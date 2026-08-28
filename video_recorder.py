# (c) Adam Davis - adamdavis.co.uk
import os
import shutil
import subprocess
import threading
import time

import numpy as np

from log import log

_REC_DIR = os.path.join(os.path.dirname(__file__), "debug", "recordings")


def _find_ffmpeg() -> str | None:
    for p in (shutil.which("ffmpeg"),
              "/opt/homebrew/bin/ffmpeg",
              "/usr/local/bin/ffmpeg"):
        if p and os.path.isfile(p):
            return p
    return None


_FFMPEG = _find_ffmpeg()


class VideoRecorder:
    """Pipes raw canvas frames to an ffmpeg subprocess.

    Thread safety
        record() is called from the capture thread ~60 times a second.
        start()/stop() are called from whichever thread the operator used:
        the GUI thread (hotkey V), the MIDI thread (pedal) or the mode-control
        thread (POST /admin/mode).  Without a lock, stop() setting _proc = None
        between record()'s None-check and its write raises AttributeError on
        the capture thread, and closing stdin mid-write corrupts the frame.

        The lock is held across the pipe write - that is the point, it is what
        stops stop() closing the pipe underneath a partial frame.  It is NOT
        held across stop()'s ffmpeg drain, which can take seconds: that would
        stall the capture thread and freeze the audience feed.  stop() takes
        ownership of the handle under the lock and does the slow part outside.
    """

    def __init__(self):
        self.active  = False
        self._proc   = None
        self._path   = ""
        self._size   = None   # (w, h) set on first frame
        self._lock   = threading.Lock()
        self._issued = set()  # paths handed out this session - see start()

    def start(self) -> str:
        os.makedirs(_REC_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        # The stamp only resolves to the second, so two recordings started
        # inside the same second would land on one filename and the second
        # would overwrite the first - silently, since ffmpeg is passed -y.
        # Easy to hit now that recording can be toggled over HTTP rather than
        # only by a foot pedal.
        #
        # Checking the filesystem is not enough on its own: ffmpeg opens the
        # file lazily on the first frame, so a recording stopped before any
        # frame arrived leaves nothing on disk to collide with. _issued
        # remembers the names handed out regardless.
        path = os.path.join(_REC_DIR, f"{stamp}.mp4")
        n = 2
        while os.path.exists(path) or path in self._issued:
            path = os.path.join(_REC_DIR, f"{stamp}_{n}.mp4")
            n += 1
        self._issued.add(path)
        with self._lock:
            self._path = path
            self._proc = None   # opened lazily on first frame so size is known
            self._size = None
            self.active = True
            path = self._path
        log.info(f"[rec] recording started → {path}")
        return path

    def record(self, canvas: np.ndarray):
        with self._lock:
            self._record_locked(canvas)

    def _record_locked(self, canvas: np.ndarray):
        if not self.active:
            return
        h, w = canvas.shape[:2]
        if self._proc is None:
            if not _FFMPEG:
                log.warning("[rec] ffmpeg not found — cannot record")
                self.active = False
                return
            self._size = (w, h)
            # use_wallclock_as_timestamps timestamps each piped raw frame at
            # arrival time, so playback matches real-world duration regardless
            # of camera fps (varies 14–60 in our setup).
            self._proc = subprocess.Popen(
                [
                    _FFMPEG, "-y",
                    "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{w}x{h}", "-pix_fmt", "bgr24",
                    "-use_wallclock_as_timestamps", "1",
                    "-i", "pipe:0",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    # Without this libx264 keeps the full chroma from bgr24 and
                    # writes yuv444p, which no browser will decode - Chrome and
                    # Safari both need 4:2:0. Every recording made before this
                    # plays in VLC and shows a black frame on a web page.
                    "-pix_fmt", "yuv420p",
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
        # Take ownership of the handle under the lock so no other thread can
        # be mid-write, then release it before draining ffmpeg.
        with self._lock:
            self.active = False
            proc, self._proc = self._proc, None
            path, self._path = self._path, ""
            self._size = None

        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=30)
                log.info(f"[rec] saved → {path}")
            except Exception as e:
                log.warning(f"[rec] ffmpeg close error: {e}")
                proc.kill()
                proc.wait()   # reap zombie after forced kill
        return path
