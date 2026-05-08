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
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is not None:
        cv2.imshow(window_name, img)
        cv2.waitKey(1)


# --------------------------------------------------------------------------- #
# Generation loop
# --------------------------------------------------------------------------- #
def run_generation_loop(client, workflow, webcam, transcriber, nodes_cfg, display_size, shutdown_event):
    client_id   = str(uuid.uuid4())
    node_id     = str(nodes_cfg.get("prompt_node_id", "139"))
    PREVIEW_WIN = "ComfyUI Output"

    # Create preview window before loop starts (resizable -- drag corner to any size)
    init_preview_window(PREVIEW_WIN, display_size)

    state = {
        "last_prompt":     None,
        "current_pid":     None,
        "gen_count":       0,
        "last_queue_time": 0.0,
    }

    def get_voice_text():
        return transcriber.get_latest_text() or "a beautiful scene"

    def grab_and_upload():
        """Capture a webcam frame and upload it to ComfyUI. Returns True on success."""
        png = webcam.grab_png_bytes()
        if png is None:
            print(f"{C.WARN}[WARN] Webcam grab failed -- skipping frame upload{C.RESET}")
            return False
        try:
            client.upload_bytes(png, WebcamGrabber.UPLOAD_NAME)
            return True
        except Exception as exc:
            print(f"{C.WARN}[WARN] Webcam upload failed: {exc}{C.RESET}")
            return False

    def queue(prompt_text, front=False):
        """Upload webcam frame, patch workflow, inject prompt, POST to ComfyUI."""
        grab_and_upload()

        # Swap WebcamCapture -> LoadImage, then inject voice text
        try:
            wf  = ComfyUIClient.replace_webcam_node(workflow, WebcamGrabber.UPLOAD_NAME)
            wf  = ComfyUIClient.inject_prompt(wf, prompt_text, node_id)
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

        # Keep the cv2 preview window alive
        cv2.waitKey(1)

        # Voice-change check (debounced 300 ms)
        latest  = get_voice_text()
        elapsed = time.time() - state["last_queue_time"]
        if latest != state["last_prompt"] and elapsed > 0.3:
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
                if img_bytes:
                    show_image(PREVIEW_WIN, img_bytes)

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

    comfy_cfg    = cfg["comfyui"]
    whisper_cfg  = cfg["whisper"]
    audio_cfg    = cfg["audio"]
    nodes_cfg    = cfg["nodes"]
    webcam_cfg   = cfg.get("webcam", {})
    display_size = cfg.get("display", {}).get("window_size", 768)

    workflow_path = comfy_cfg.get("workflow_json", "workflows/optimized_flux2_klein_512_v01.json")
    if not Path(workflow_path).exists():
        print(f"{C.ERR}[ERROR] Workflow JSON not found: {workflow_path}{C.RESET}")
        sys.exit(1)

    # Pre-flight: verify ComfyUI is reachable
    if not preflight_check(comfy_cfg["host"]):
        sys.exit(1)

    workflow = load_workflow(workflow_path)
    client   = ComfyUIClient(comfy_cfg["host"])

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
        transcribe_interval = audio_cfg.get("transcribe_interval", 1.0),
        silence_threshold   = audio_cfg.get("silence_threshold", 0.01),
    )
    transcriber.start()
    print(f"{C.INFO}[INFO] Whisper listening. Speak to drive the prompt.{C.RESET}\n")

    # Run generation loop (blocks until Ctrl+C)
    run_generation_loop(client, workflow, webcam, transcriber, nodes_cfg, display_size, _shutdown_event)

    # Shutdown
    webcam.release()
    print(f"\n{C.INFO}[INFO] Stopping Whisper...{C.RESET}")
    transcriber.stop()
    transcriber.join(timeout=3.0)
    print(f"{C.INFO}[INFO] Done.{C.RESET}")


if __name__ == "__main__":
    main()
