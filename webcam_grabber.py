"""OpenCV webcam capture -- grabs a fresh frame for each ComfyUI generation."""
import time
import cv2


class WebcamGrabber:
    # Filename used for every upload -- ComfyUI overwrites the same slot each time
    UPLOAD_NAME = "comfy_live_webcam.png"

    def __init__(self, device_index=0, width=512, height=512, retries=5, retry_delay=2.0):
        # On Windows, MSMF holds an exclusive lock that can linger after a crash.
        # DSHOW is tried first because it tends to succeed even when MSMF still
        # thinks it owns the device.  We retry the full list a few times to give
        # Windows a moment to release the handle.
        backends = [
            (cv2.CAP_DSHOW, "DSHOW"),
            (cv2.CAP_MSMF,  "MSMF"),
            (cv2.CAP_ANY,   "AUTO"),
        ]
        self._cap = None
        for attempt in range(1, retries + 1):
            for backend, name in backends:
                cap = cv2.VideoCapture(device_index, backend)
                if cap.isOpened():
                    ret, _ = cap.read()
                    if ret:
                        self._cap = cap
                        print("[CAM] Opened device {} via {} (attempt {})".format(
                            device_index, name, attempt))
                        break
                cap.release()
            if self._cap is not None:
                break
            if attempt < retries:
                print("[CAM] Device {} busy -- retrying in {:.0f}s ({}/{})...".format(
                    device_index, retry_delay, attempt, retries))
                time.sleep(retry_delay)

        if self._cap is None:
            raise RuntimeError(
                "Cannot open webcam device {} with any backend after {} attempts.\n"
                "  - Kill any stale python.exe processes:  Get-Process python* | Stop-Process -Force\n"
                "  - Make sure no other app (browser, Teams, OBS) has the camera open.\n"
                "  - Try a different index in config.yaml -> webcam.device_index".format(
                    device_index, retries)
            )

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH,  width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.width  = width
        self.height = height
        print("[CAM] Resolution set to {}x{}".format(width, height))

    def grab_png_bytes(self):
        """Flush the capture buffer, read a fresh frame, return PNG bytes. None on failure."""
        self._cap.grab()
        self._cap.grab()
        ret, frame = self._cap.read()
        if not ret or frame is None:
            return None
        frame = cv2.resize(frame, (self.width, self.height))
        ok, buf = cv2.imencode(".png", frame)
        return bytes(buf) if ok else None

    def release(self):
        self._cap.release()
