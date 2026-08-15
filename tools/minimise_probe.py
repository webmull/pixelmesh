# (c) Adam Davis - adamdavis.co.uk
"""Does a camera capture thread keep running while the window is minimised?

This settles the premise behind moving capture off the DearPyGui render loop.
Today capture lives inside `while dpg.is_dearpygui_running(): ... render_
dearpygui_frame()`, and macOS stops driving that loop when the window is
minimised — so frames stop, _stream_latest stops changing, and the feed
freezes. Moving capture to its own thread only fixes that if a background
thread genuinely keeps running while the app is hidden. App Nap could still
throttle it.

Nothing here touches pixelmesh. It opens the camera the same way controller.py
does, reads frames on a background thread, and reports the rate once a second
from the main thread — the same producer/consumer split the refactor would use.

    python3 tools/minimise_probe.py          # camera 0
    python3 tools/minimise_probe.py 1        # camera 1

Stop pixelmesh first — two processes cannot hold the same camera.

HOW TO READ IT
    Let it settle for ~10s, then minimise the window (Cmd-M) and leave it for
    two minutes. Bring it back and look at the log.

    fps holds steady while minimised  -> a capture thread survives. The
                                         refactor will work. Go ahead.
    fps drops to 0 while minimised    -> App Nap is throttling the whole
                                         process. Threading will NOT fix it on
                                         its own; the answer is App Nap
                                         (LSAppNapIsDisabled / NSAppSleep-
                                         Disabled), which is far less work.

The probe prints a verdict when you stop it with Ctrl-C.
"""
import sys
import threading
import time

import cv2

CAM_WIDTH, CAM_HEIGHT, TARGET_FPS = 1280, 720, 60

_frames = 0
_lock = threading.Lock()
_stop = threading.Event()


def capture_worker(cap):
    """The producer half of the proposed refactor, in miniature."""
    global _frames
    while not _stop.is_set():
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue
        with _lock:
            _frames += 1


def main():
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    cap = cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        print(f"could not open camera {idx} — is pixelmesh still running?")
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)

    t = threading.Thread(target=capture_worker, args=(cap,),
                         daemon=True, name="probe-capture")
    t.start()

    print(f"camera {idx} open. Minimise this window (Cmd-M) for two minutes, "
          f"then bring it back.\nCtrl-C to stop.\n")

    global _frames
    samples = []
    try:
        while True:
            time.sleep(1.0)
            with _lock:
                n, _frames = _frames, 0
            samples.append(n)
            bar = "#" * min(n, 60)
            print(f"{time.strftime('%H:%M:%S')}  {n:3d} fps  {bar}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        t.join(timeout=2.0)
        # Releasing the device is the whole point of a clean shutdown — a
        # thread left holding the camera is exactly the failure mode that
        # would bite the real refactor at the NEXT launch, not this one.
        cap.release()
        print("\ncamera released.")

    if samples:
        worst = min(samples)
        best = max(samples)
        print(f"\n{len(samples)} samples, low {worst} fps, high {best} fps")
        if worst == 0:
            print("VERDICT: frames stopped at some point. If that was while "
                  "minimised, threading alone will NOT fix the feed — the "
                  "process is being throttled (App Nap). Chase that instead.")
        else:
            print("VERDICT: frames never stopped. A capture thread survives "
                  "minimising, so moving capture off the render loop will fix "
                  "the feed. Safe to do the refactor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
