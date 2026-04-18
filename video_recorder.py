# (c) Adam Davis — adamdavis.co.uk
import os
import shutil
import subprocess
import time

import numpy as np

from log import log

_REC_DIR = os.path.join(os.path.dirname(__file__), "debug", "recordings")

_FFMPEG = (
    shutil.which("ffmpeg")
    or "/opt/homebrew/bin/ffmpeg"
    or "/usr/local/bin/ffmpeg"
)


class VideoRecorder:
    def __init__(self):
        self.active  = False
        self._proc   = None
        self._path   = ""
        self._size   = None   # (w, h) set on first frame

    def start(self) -> str:
        os.makedirs(_REC_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self._path = os.path.join(_REC_DIR, f"{stamp}.mp4")
        self._proc = None   # opened lazily on first frame so size is known
        self._size = None
        self.active = True
        log.info(f"[rec] recording started → {self._path}")
        return self._path

    def record(self, canvas: np.ndarray):
        if not self.active:
            return
        h, w = canvas.shape[:2]
        if self._proc is None:
            if not os.path.isfile(_FFMPEG):
                log.warning("[rec] ffmpeg not found — cannot record")
                self.active = False
                return
            self._size = (w, h)
            self._proc = subprocess.Popen(
                [
                    _FFMPEG, "-y",
                    "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{w}x{h}", "-pix_fmt", "bgr24", "-r", "30",
                    "-i", "pipe:0",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-movflags", "+faststart",
                    self._path,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(f"[rec] ffmpeg pipe opened ({w}×{h})")
        if canvas.shape[:2] != (self._size[1], self._size[0]):
            return   # size changed mid-recording — skip frame
        try:
            self._proc.stdin.write(canvas.tobytes())
        except BrokenPipeError:
            log.warning("[rec] ffmpeg pipe broken")
            self._proc = None
            self.active = False

    def stop(self) -> str:
        self.active = False
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=30)
                log.info(f"[rec] saved → {self._path}")
            except Exception as e:
                log.warning(f"[rec] ffmpeg close error: {e}")
                self._proc.kill()
            self._proc = None
        path, self._path = self._path, ""
        return path
