"""OpenCV webcam capture -- grabs a fresh frame for each ComfyUI generation."""
import cv2


class WebcamGrabber:
    # Filename used for every upload -- ComfyUI overwrites the same slot each time
    UPLOAD_NAME = "comfy_live_webcam.png"

    def __init__(self, device_index=0, width=512, height=512):
        # Try backends in order: MSMF (best on Win10/11), then auto, then DSHOW
        backends = [
            (cv2.CAP_MSMF,  "MSMF"),
            (cv2.CAP_ANY,   "AUTO"),
            (cv2.CAP_DSHOW, "DSHOW"),
        ]
        self._cap = None
        for backend, name in backends:
            cap = cv2.VideoCapture(device_index, backend)
            if cap.isOpened():
                ret, _ = cap.read()
                if ret:
                    self._cap = cap
                    print("[CAM] Opened device {} via {}".format(device_index, name))
                    break
            cap.release()

        if self._cap is None:
            raise RuntimeError(
                "Cannot open webcam device {} with any backend.\n"
                "  - Make sure no other app (browser, Teams, OBS) has the camera open.\n"
                "  - Try a different index in config.yaml -> webcam.device_index".format(device_index)
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
