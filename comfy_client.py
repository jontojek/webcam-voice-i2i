"""ComfyUI REST + WebSocket client."""
import json
import uuid
from typing import Any, Dict, Optional

import requests
import websocket as ws_lib


class ComfyUIClient:
    def __init__(self, host: str):
        self.host = host.rstrip("/")

    # ------------------------------------------------------------------ #
    # REST helpers
    # ------------------------------------------------------------------ #
    def queue_prompt(self, prompt: Dict[str, Any], client_id: str, front: bool = False) -> str:
        """Queue a prompt, return prompt_id.
        front=True jumps to the front of the queue (use after /interrupt).
        """
        payload: Dict[str, Any] = {"prompt": prompt, "client_id": client_id}
        if front:
            payload["front"] = True
        resp = requests.post(f"{self.host}/prompt", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()["prompt_id"]

    def interrupt(self) -> None:
        """Stop the currently executing generation immediately."""
        try:
            requests.post(f"{self.host}/interrupt", timeout=5)
        except Exception:
            pass

    def upload_bytes(self, image_bytes: bytes, filename: str) -> str:
        """Upload raw PNG/JPEG bytes to ComfyUI input folder (overwrites same slot each time)."""
        files = {"image": (filename, image_bytes, "image/png")}
        data  = {"overwrite": "true"}
        resp  = requests.post(f"{self.host}/upload/image", files=files, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json().get("name", filename)

    def fetch_image_bytes(self, filename: str, subfolder: str = "", img_type: str = "temp"):
        """Fetch a generated image from ComfyUI /view. Returns bytes or None."""
        params = "filename={}&subfolder={}&type={}".format(filename, subfolder, img_type)
        try:
            r = requests.get("{}/view?{}".format(self.host, params), timeout=10)
            r.raise_for_status()
            return r.content
        except Exception:
            return None

    def open_websocket(self, client_id: str) -> ws_lib.WebSocket:
        """Open and return a WebSocket connection to ComfyUI."""
        ws_url = (
            self.host
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            + "/ws?clientId=" + client_id
        )
        sock = ws_lib.WebSocket()
        sock.connect(ws_url)
        return sock

    # ------------------------------------------------------------------ #
    # Workflow manipulation
    # ------------------------------------------------------------------ #
    @staticmethod
    def inject_prompt(workflow: Dict[str, Any], prompt_text: str, prompt_node_id: str) -> Dict[str, Any]:
        """Return a deep copy of workflow with text injected into CLIPTextEncode node."""
        wf = json.loads(json.dumps(workflow))
        if prompt_node_id in wf:
            inputs = wf[prompt_node_id].get("inputs", {})
            if "text" in inputs:
                inputs["text"] = prompt_text
            else:
                for k, v in inputs.items():
                    if isinstance(v, str):
                        inputs[k] = prompt_text
                        break
        return wf

    @staticmethod
    def replace_webcam_node(workflow: Dict[str, Any], image_filename: str) -> Dict[str, Any]:
        """Return a deep copy of workflow with every WebcamCapture node replaced by LoadImage.

        Node IDs and all downstream connections are preserved -- only the class_type
        and inputs change, so the wiring to VAEEncode etc. keeps working.
        """
        wf = json.loads(json.dumps(workflow))
        for node_id, node in wf.items():
            if node.get("class_type") == "WebcamCapture":
                title = node.get("_meta", {}).get("title", "Webcam Frame")
                wf[node_id] = {
                    "class_type": "LoadImage",
                    "_meta": {"title": title},
                    "inputs": {"image": image_filename, "upload": "image"},
                }
        return wf
