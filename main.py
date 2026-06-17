"""Realtime webcam voice-to-image loop.

Flow
----
1. WebcamGrabber    -- OpenCV grabs a fresh frame before every generation
2. AudioTranscriber -- Whisper listens to mic and updates latest_text every ~1 s
3. Generation loop  -- WebSocket-driven:
     * Grab webcam frame -> upload to ComfyUI
     * Inject voice prompt text into CLIPTextEncode node
     * Queue workflow (WebcamCapture node swapped to LoadImage at runtime)
     * On completion: show output image in a cv2 preview window, queue next frame
     * On voice change (debounced 0.3s): interrupt + requeue immediately
"""

import json
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
import requests
import yaml
import websocket

from audio_capture import AudioTranscriber
from comfy_client import ComfyUIClient
from webcam_grabber import WebcamGrabber

# Enable ANSI escape codes on Windows 10+
os.system("")

# Fixed upload slot for the previous output frame -- ComfyUI overwrites this same
# file each cycle (mirrors WebcamGrabber.UPLOAD_NAME), and the workflow's
# "Previous Output Frame" LoadImage node (id 152) always reads this filename.
PREVOUT_UPLOAD_NAME = "comfy_live_prevout.png"

# --------------------------------------------------------------------------- #
# ANSI color constants
# --------------------------------------------------------------------------- #
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    # Prompt text -- bright cyan, bold
    PROMPT  = "\033[1;96m"
    # Labels / brackets
    LABEL   = "\033[1;33m"
    # Technical detail lines (pid, node)
    DETAIL  = "\033[2;37m"
    # Info / status lines
    INFO    = "\033[0;36m"
    # Warnings
    WARN    = "\033[0;33m"
    # Errors
    ERR     = "\033[1;31m"
    # Separator
    SEP     = "\033[0;90m"


# --------------------------------------------------------------------------- #
# Graceful shutdown
# --------------------------------------------------------------------------- #
_shutdown_event = threading.Event()

def _signal_handler(signum, _frame):
    print(f"\n{C.INFO}[INFO] Signal {signum} -- shutting down...{C.RESET}")
    _shutdown_event.set()

signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_workflow(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def preflight_check(host: str) -> bool:
    print(f"{C.INFO}[CHECK] Reaching ComfyUI at {host} ...{C.RESET}")
    try:
        r = requests.get(f"{host}/system_stats", timeout=5)
        r.raise_for_status()
        pv = r.json().get("system", {}).get("python_version", "?")
        print(f"{C.INFO}[CHECK] OK -- ComfyUI online  (Python {pv}){C.RESET}")
        return True
    except requests.exceptions.ConnectionError:
        print(f"{C.ERR}[ERROR] Connection refused at {host}{C.RESET}")
        print(f"{C.ERR}        Is ComfyUI running? Check config.yaml -> comfyui.host{C.RESET}")
        return False
    except Exception as exc:
        print(f"{C.ERR}[ERROR] ComfyUI pre-flight failed: {exc}{C.RESET}")
        return False

def init_preview_window(window_name: str, size: int) -> None:
    """Create a resizable preview window at the given initial size."""
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, size, size)

def show_image(window_name: str, img_bytes: bytes) -> None:
    """Decode raw PNG/JPEG bytes and update the named cv2 window."""
    img = decode_image(img_bytes)
    if img is not None:
        show_array(window_name, img)

def decode_image(img_bytes: bytes):
    """Decode raw PNG/JPEG bytes to a BGR np array, or None on failure."""
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)

def show_array(window_name: str, img) -> None:
    """Push an already-decoded BGR array to the named cv2 window."""
    cv2.imshow(window_name, img)
    cv2.waitKey(1)

def optical_flow_midframe(prev_bgr, curr_bgr):
    """Motion-compensated interpolation between two frames (classic optical-flow
    retiming, e.g. Nuke's Kronos/OFlow) -- CPU-only, no extra model dependency.
    Returns a single frame roughly halfway between prev and curr, or None if the
    frames aren't usable (e.g. mismatched size on the very first pair).
    """
    if prev_bgr is None or curr_bgr is None:
        return None
    if prev_bgr.shape != curr_bgr.shape:
        return None
    try:
        prev_gray = cv2.cvtColor(prev_bgr, cv2.COLOR_BGR2GRAY)
        curr_gray = cv2.cvtColor(curr_bgr, cv2.COLOR_BGR2GRAY)
        flow = cv2.calcOpticalFlowFarneback(
            prev_gray, curr_gray, None,
            pyr_scale=0.5, levels=2, winsize=15,
            iterations=2, poly_n=5, poly_sigma=1.1, flags=0,
        )
        h, w = prev_gray.shape
        grid_x, grid_y = np.meshgrid(np.arange(w), np.arange(h))
        # Warp prev forward half a step, curr backward half a step, blend the two.
        fwd_x = (grid_x + 0.5 * flow[..., 0]).astype(np.float32)
        fwd_y = (grid_y + 0.5 * flow[..., 1]).astype(np.float32)
        bwd_x = (grid_x - 0.5 * flow[..., 0]).astype(np.float32)
        bwd_y = (grid_y - 0.5 * flow[..., 1]).astype(np.float32)
        warped_prev = cv2.remap(prev_bgr, fwd_x, fwd_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        warped_curr = cv2.remap(curr_bgr, bwd_x, bwd_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        return cv2.addWeighted(warped_prev, 0.5, warped_curr, 0.5, 0)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Generation loop
# --------------------------------------------------------------------------- #
def run_generation_loop(client, workflow, webcam, transcriber, nodes_cfg, display_size,
                         shutdown_event, prompt_template="{phrase}", interpolate_enabled=True,
                         feedback_step=0.02):
    client_id   = str(uuid.uuid4())
    node_id     = str(nodes_cfg.get("prompt_node_id", "139"))
    PREVIEW_WIN = "ComfyUI Output"

    # Create preview window before loop starts (resizable -- drag corner to any size)
    init_preview_window(PREVIEW_WIN, display_size)

    # Temporal feedback's LatentBlend node, if the workflow has one. Reading the
    # blend_factor that's already baked into `workflow` (set in main() from
    # config.yaml's feedback.strength) lets the keyboard control below start
    # from whatever the config said, instead of guessing.
    feedback_node_id  = str(nodes_cfg.get("feedback_node_id", "154"))
    feedback_available = feedback_node_id in workflow

    state = {
        "last_prompt":       None,
        "current_pid":       None,
        "gen_count":         0,
        "last_queue_time":   0.0,
        # Last completed-utterance sequence number we've already committed.
        # AudioTranscriber.get_update() returns a monotonic seq that increments
        # once per finished phrase (pause-terminated), so we no longer need to
        # wait for identical text across polls -- a new seq IS a new phrase.
        "last_committed_seq": 0,
        # Temporal-feedback / interpolation bookkeeping
        "last_output_bytes":  None,   # raw bytes of the last decoded output (fed back as node 152)
        "last_output_arr":    None,   # decoded BGR array of the last decoded output (for optical flow)
        "feedback_strength":  (
            float(workflow[feedback_node_id]["inputs"].get("blend_factor", 0.0))
            if feedback_available else 0.0
        ),
    }

    if feedback_available:
        print(
            f"{C.INFO}[INFO] Feedback strength: {state['feedback_strength']:.3f}  "
            f"(press [ for more memory/ghosting, ] for more live/responsive, "
            f"1 = fully live, 0 = max ghosting){C.RESET}"
        )

    def get_voice_text():
        return transcriber.get_latest_text() or "The Easter Bunny"

    def queue(prompt_text, front=False):
        """Upload webcam frame + previous-output frame, patch workflow, inject prompt, POST to ComfyUI."""
        png = webcam.grab_png_bytes()
        if png is None:
            print(f"{C.WARN}[WARN] Webcam grab failed -- skipping frame upload{C.RESET}")
            return False
        try:
            client.upload_bytes(png, WebcamGrabber.UPLOAD_NAME)
            # Feed the previous decoded output back in for the temporal-feedback blend.
            # On the very first frame there is no previous output yet, so fall back to
            # the fresh webcam frame -- blending a frame with itself is a no-op.
            client.upload_bytes(state["last_output_bytes"] or png, PREVOUT_UPLOAD_NAME)
        except Exception as exc:
            print(f"{C.WARN}[WARN] Frame upload failed: {exc}{C.RESET}")
            return False

        # Swap WebcamCapture -> LoadImage, then inject voice text
        try:
            wf  = ComfyUIClient.replace_webcam_node(workflow, WebcamGrabber.UPLOAD_NAME)
            wf  = ComfyUIClient.inject_prompt(wf, prompt_template.format(phrase=prompt_text), node_id)
            pid = client.queue_prompt(wf, client_id, front=front)
        except Exception as exc:
            print(f"\n{C.ERR}[ERROR] queue_prompt failed: {exc}{C.RESET}")
            return False

        state["current_pid"]     = pid
        state["last_queue_time"] = time.time()
        n = state["gen_count"]

        if prompt_text != state["last_prompt"]:
            # --- prominent prompt banner ---
            sep = f"{C.SEP}{'─' * 60}{C.RESET}"
            print(f"\n{sep}")
            print(f"  {C.LABEL}PROMPT #{n}{C.RESET}  {C.PROMPT}{prompt_text}{C.RESET}")
            print(f"{sep}")
            print(f"  {C.DETAIL}pid={pid[:8]}  node={node_id}  front={front}{C.RESET}")
            state["last_prompt"] = prompt_text
        else:
            print(f"  {C.DETAIL}[{n}] pid={pid[:8]}{C.RESET}", end="\r", flush=True)

        state["gen_count"] += 1
        return True

    # -- WebSocket connect --------------------------------------------------
    print(f"{C.INFO}[INFO] Connecting to ComfyUI WebSocket...{C.RESET}")
    try:
        sock = client.open_websocket(client_id)
        sock.settimeout(0.25)
        print(f"{C.INFO}[INFO] WebSocket connected (id={client_id[:8]}...){C.RESET}")
    except Exception as exc:
        print(f"{C.ERR}[ERROR] WebSocket connect failed: {exc}{C.RESET}")
        return

    print(f"{C.INFO}[INFO] Starting generation loop. Speak to update the prompt. Ctrl+C to quit.{C.RESET}")
    print(f"{C.SEP}{'─' * 60}{C.RESET}")

    # -- First generation ---------------------------------------------------
    if not queue(get_voice_text()):
        print(f"{C.ERR}[ERROR] Could not queue first prompt.{C.RESET}")
        sock.close()
        return

    # -- Main loop ----------------------------------------------------------
    while not shutdown_event.is_set():

        # Keep the cv2 preview window alive, and read keyboard input from it.
        # The preview window must have focus for keypresses to register --
        # click it once if [ / ] don't seem to do anything.
        #
        # blend_factor (node 154) is the weight on the CURRENT webcam frame's
        # latent, not on memory -- ComfyUI's LatentBlend computes
        # samples1*blend_factor + samples2*(1-blend_factor), and samples1 here
        # is the live frame (146) while samples2 is the previous output (153).
        # So: higher = more live/responsive, lower = more memory/ghosting.
        # [ moves toward more ghosting, ] moves toward more live, 1 goes fully
        # live (memory off), 0 goes to maximum ghosting (ignores the camera).
        key = cv2.waitKey(1) & 0xFF
        if feedback_available and key != 255:
            new_strength = None
            if key in (ord('['), ord('-'), ord('_')):
                new_strength = max(0.0, round(state["feedback_strength"] - feedback_step, 3))
            elif key in (ord(']'), ord('='), ord('+')):
                new_strength = min(1.0, round(state["feedback_strength"] + feedback_step, 3))
            elif key == ord('0'):
                new_strength = 0.0
            elif key == ord('1'):
                new_strength = 1.0
            if new_strength is not None and new_strength != state["feedback_strength"]:
                old_strength = state["feedback_strength"]
                state["feedback_strength"] = new_strength
                workflow[feedback_node_id]["inputs"]["blend_factor"] = new_strength
                label = "more live/responsive" if new_strength > old_strength else "more memory/ghosting"
                print(f"{C.INFO}[INFO] Feedback strength -> {new_strength:.3f}  ({label}){C.RESET}")

        # Commit on utterance completion, not text stability:
        # AudioTranscriber segments speech by real pauses and only updates its
        # seq/text once a phrase is fully finished, so the moment seq advances
        # we already know it's a complete, new phrase -- no extra wait needed.
        seq, latest = transcriber.get_update()
        if seq != state["last_committed_seq"]:
            state["last_committed_seq"] = seq
            if latest and latest != state["last_prompt"]:
                client.interrupt()
                queue(latest, front=True)
                continue

        # Wait for a WebSocket frame
        try:
            raw = sock.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as exc:
            print(f"\n{C.WARN}[WARN] WebSocket dropped: {exc}  Reconnecting...{C.RESET}")
            time.sleep(1.0)
            try:
                sock = client.open_websocket(client_id)
                sock.settimeout(0.25)
                print(f"{C.INFO}[INFO] WebSocket reconnected.{C.RESET}")
            except Exception as exc2:
                print(f"{C.ERR}[ERROR] Reconnect failed: {exc2}{C.RESET}")
                time.sleep(2.0)
            continue

        # ComfyUI sends binary preview frames (latent/JPEG previews) -- show them
        if isinstance(raw, bytes):
            if len(raw) > 8:
                # Header: 4 bytes event-type + 4 bytes format, then image data
                preview_bytes = raw[8:]
                show_image(PREVIEW_WIN, preview_bytes)
            continue

        try:
            msg = json.loads(raw)
        except Exception:
            continue

        mtype = msg.get("type", "")
        data  = msg.get("data", {})
        pid   = data.get("prompt_id")

        # ---- Show final output image when a node completes with images ----
        if mtype == "executed":
            images = data.get("output", {}).get("images", [])
            for img_info in images:
                img_bytes = client.fetch_image_bytes(
                    filename  = img_info["filename"],
                    subfolder = img_info.get("subfolder", ""),
                    img_type  = img_info.get("type", "temp"),
                )
                if not img_bytes:
                    continue
                arr = decode_image(img_bytes)
                if arr is None:
                    continue
                # Optical-flow interpolated half-step between the last real output
                # and this one, shown first to smooth the perceived frame rate --
                # purely a display trick, doesn't change what gets fed back as
                # node 152's temporal-feedback source (that always uses the real
                # decoded frame, never an interpolated one).
                if interpolate_enabled and state["last_output_arr"] is not None:
                    mid = optical_flow_midframe(state["last_output_arr"], arr)
                    if mid is not None:
                        show_array(PREVIEW_WIN, mid)
                show_array(PREVIEW_WIN, arr)
                state["last_output_arr"]   = arr
                state["last_output_bytes"] = img_bytes

        # ---- ComfyUI execution error -- log it and keep going -------------
        if mtype == "execution_error":
            err  = data.get("exception_message", "unknown error")
            node = data.get("node_id", "?")
            print(f"\n{C.ERR}[COMFYUI ERROR] node={node}: {err}{C.RESET}")
            queue(get_voice_text())
            continue

        # ---- Completion signal -- queue next frame immediately ------------
        is_done = (
            (mtype == "executing"          and data.get("node") is None and pid == state["current_pid"])
            or
            (mtype == "execution_complete" and pid == state["current_pid"])
        )
        if is_done:
            queue(get_voice_text())

    sock.close()
    cv2.destroyAllWindows()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()

    comfy_cfg     = cfg["comfyui"]
    whisper_cfg   = cfg["whisper"]
    audio_cfg     = cfg["audio"]
    nodes_cfg     = cfg["nodes"]
    webcam_cfg    = cfg.get("webcam", {})
    display_cfg   = cfg.get("display", {})
    display_size  = display_cfg.get("window_size", 768)
    interpolate_enabled = display_cfg.get("interpolate", True)
    feedback_cfg  = cfg.get("feedback", {})
    prompting_cfg = cfg.get("prompting", {})
    prompt_template = prompting_cfg.get("template", "{phrase}")

    workflow_path = comfy_cfg.get("workflow_json", "workflows/optimized_flux2_klein_512_v01.json")
    if not Path(workflow_path).exists():
        print(f"{C.ERR}[ERROR] Workflow JSON not found: {workflow_path}{C.RESET}")
        sys.exit(1)

    # Pre-flight: verify ComfyUI is reachable
    if not preflight_check(comfy_cfg["host"]):
        sys.exit(1)

    workflow = load_workflow(workflow_path)
    client   = ComfyUIClient(comfy_cfg["host"])

    # Bake the temporal-feedback strength into the LatentBlend node once at startup.
    # node 154's blend_factor is the weight on the CURRENT/LIVE webcam frame latent --
    # 1.0 = pure live frame (no memory), lower = more persistence/ghosting from the
    # previous output, 0.0 = ignores the live frame entirely (max ghosting).
    feedback_node_id = str(nodes_cfg.get("feedback_node_id", "154"))
    if feedback_cfg.get("enabled", True) and feedback_node_id in workflow:
        strength = float(feedback_cfg.get("strength", 0.15))
        workflow[feedback_node_id]["inputs"]["blend_factor"] = strength
        print(f"{C.INFO}[INFO] Temporal feedback enabled (strength={strength}){C.RESET}")
    elif feedback_node_id in workflow:
        # Disabled -- blend_factor=1.0 means pure live frame, i.e. no feedback at all.
        workflow[feedback_node_id]["inputs"]["blend_factor"] = 1.0
        print(f"{C.INFO}[INFO] Temporal feedback disabled{C.RESET}")

    # Open webcam
    try:
        webcam = WebcamGrabber(
            device_index = webcam_cfg.get("device_index", 0),
            width        = webcam_cfg.get("width",  512),
            height       = webcam_cfg.get("height", 512),
        )
    except RuntimeError as exc:
        print(f"{C.ERR}[ERROR] {exc}{C.RESET}")
        sys.exit(1)

    # Start Whisper
    transcriber = AudioTranscriber(
        model_size          = whisper_cfg["model"],
        device              = whisper_cfg.get("device", "auto"),
        compute_type        = whisper_cfg.get("compute_type", "int8"),
        sample_rate         = audio_cfg.get("sample_rate", 16000),
        chunk_duration_ms   = audio_cfg.get("chunk_duration_ms", 500),
        channels            = audio_cfg.get("channels", 1),
        buffer_seconds      = audio_cfg.get("buffer_seconds", 3),
        transcribe_interval = audio_cfg.get("transcribe_interval", 1.5),
        silence_threshold   = audio_cfg.get("silence_threshold", 0.01),
        min_silence_ms      = audio_cfg.get("min_silence_ms", 600),
        min_speech_ms       = audio_cfg.get("min_speech_ms", 150),
        max_utterance_ms    = audio_cfg.get("max_utterance_ms", 8000),
    )
    transcriber.start()
    print(f"{C.INFO}[INFO] Whisper listening. Speak to drive the prompt.{C.RESET}\n")

    # Run generation loop (blocks until Ctrl+C)
    run_generation_loop(
        client, workflow, webcam, transcriber, nodes_cfg, display_size, _shutdown_event,
        prompt_template=prompt_template, interpolate_enabled=interpolate_enabled,
        feedback_step=float(feedback_cfg.get("step", 0.02)),
    )

    # Shutdown
    webcam.release()
    print(f"\n{C.INFO}[INFO] Stopping Whisper...{C.RESET}")
    transcriber.stop()
    transcriber.join(timeout=3.0)
    print(f"{C.INFO}[INFO] Done.{C.RESET}")


if __name__ == "__main__":
    main()
