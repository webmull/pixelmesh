# (c) Adam Davis - adamdavis.co.uk
#!/usr/bin/env python3
"""
pixelmesh — controller

Dear PyGui controller with:
  - Live camera preview
  - Blink detection overlay (replaces AprilTag detection)
  - Device position mapping to u-space
  - Effect triggers (sidebar buttons / MIDI only — no number-key hotkeys)
  - Client count polling

Hotkeys:
  D       Toggle detection
  S       Toggle clock sync
  H       Toggle all overlays
  O       Toggle ID overlays
  P       Toggle overlay mode (IDs / render order)
  R       Reset server
  Tab     Toggle sidebar
  G       Debug capture
  V       Record video
  Q/Esc   Quit

Dependencies:
  pip install dearpygui opencv-python numpy requests
"""

import math
import sys
import os as _os
import threading
import time
from queue import Queue, Full, Empty

# Must be launched via run.sh
if not _os.environ.get("PIXELMESH_LAUNCHED"):
    print("pixelmesh controller must be started via run.sh", file=sys.stderr)
    sys.exit(1)

import cv2
import dearpygui.dearpygui as dpg
import numpy as np

from state import AppState, PREVIEW_WIDTH, PREVIEW_HEIGHT
from camera import apply_gamma, apply_contrast
from blink_detector import BlinkDetector
from debug_capture import DebugCapture
from video_recorder import VideoRecorder
from network import post_json, post_json_async, fetch_client_count, fetch_json
from log import log
import effects
import game
import elgato
import midi
import report

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #

WINDOW_TITLE       = "pixelmesh"
CAM_WIDTH          = 1920
CAM_HEIGHT         = 1080
TARGET_FPS         = 60
CLIENT_FETCH_SECS  = 2.0
FONT               = cv2.FONT_HERSHEY_SIMPLEX

state    = AppState()
detector = BlinkDetector()
detector.cfg["history_seconds"] = 15.0   # decode needs 13.2s; 30s default wastes memory/trim cost
detector.cfg["recent_n"]        = 18     # smaller std window (1.2s @ 15fps) — still covers 6 blink cycles
dbg_cap  = DebugCapture()

# Throttle debug saves: one frame every N camera frames
DEBUG_SAVE_EVERY = 6

# MJPEG stream: latest canvas frame written atomically for server.py to serve
_STREAM_PATH      = "/tmp/pixelmesh_stream.jpg"
_STREAM_INTERVAL  = 1.0 / 30          # 30fps
_last_stream_ts   = 0.0
ui_queue: Queue = Queue()
# Set to True while draining the UI queue so checkbox set_value calls
# don't re-fire toggle callbacks (some DearPyGui versions fire callbacks
# on set_value, which causes detection/debug to toggle unexpectedly).
_ui_syncing = False

# Detection runs on a dedicated background thread so the camera loop is never
# blocked.  maxsize=1 means old frames are dropped if the detector is busy —
# the display thread always runs at full camera speed regardless of detection load.
_detect_queue  = Queue(maxsize=1)
_last_dbg_imgs = None   # DebugImages; written by detection thread, read by main
_detect_fps:   float = 0.0   # EMA fps of detection thread, read by draw_hud
_camera_fps:   float = 0.0   # EMA fps of camera frame delivery, read by exposure monitor

MIN_RELIABLE_FPS    = 8.0   # below this, assume exposure has crept up in auto mode
_EXP_MONITOR_SECS   = 5.0   # how often the exposure monitor checks fps
_EXP_RELOCK_COOLDOWN = 15.0 # minimum seconds between consecutive re-lock attempts

# Detection timing
_detection_start_time: float = 0.0
_detected_ids: set = set()                      # blink_ids seen this detection session
_render_order: dict[int, int] = {}              # blink_id → left-to-right rank (1=leftmost)
_detection_timings: dict[int, tuple] = {}       # blink_id → (elapsed_s, confidence)
_report_saved_path: str | None = None            # set after first save this session; cleared on detect-start
_valid_blink_ids: set[int] = set()  # blink_ids assigned to connected clients (empty = not fetched yet)
_timing_log_paths: list[str] = []   # may be 1 or 2 paths (master + run)
_timing_log_handles: list = []      # open file handles paired with _timing_log_paths

# Per-phone blink amplitude (max-min brightness over the grid point's history),
# captured at decode time.  Used at detection end to suggest an ISO adjustment.
_detection_amplitudes: list[float] = []
_iso_hint: str = ""                  # surfaced under the ISO slider; cleared on detect-start

# Two-phase ISO: detection wants the low-gain default so blink contrast reads
# cleanly; the post-detection / showtime phase wants more gain so the camera
# feed showing the room reads as a bright lit crowd rather than a dim wash.
# Both auto-applied around the detection toggle; operator can still override
# via the sidebar slider afterwards.
_AUDIENCE_ISO_GAIN: int = 100

# Click-to-ripple state.  Toggled by clicking the Ripple button in the sidebar
# (which highlights when armed); the button no longer fires the audience effect
# itself.  While armed, left-clicks on the camera preview send a single half-arch
# light-blue ripple from the click's u,v and draw a matching water animation on
# the controller canvas.  Firing any other sidebar effect disarms ripple.
_ripple_armed: bool = False

# Spotlight-follow state.  Toggled by the Spotlight sidebar button; while
# armed, every render frame draws a glowing circle on the operator's cursor
# (visible in both the controller preview and the MJPEG projection), and a
# throttled broadcast streams the cursor's u,v + radius to the audience
# phones so phones inside the radius brighten and the rest stay dark.
_spotlight_armed: bool = False
_last_spotlight_broadcast_ts: float = 0.0
_SPOTLIGHT_BROADCAST_HZ = 15.0
_last_spotlight_canvas_px: tuple[int, int] | None = None
_last_spotlight_canvas_r:  int                  = 60
# List of (canvas_x, canvas_y, t_started, theta_deg) for in-flight click ripples.
# theta_deg points toward the nearest detected phone (None if no detections yet);
# the local animation draws a half-arch opening in that direction.
_click_ripples: list[tuple[int, int, float, float | None]] = []
_CLICK_RIPPLE_LIFETIME = 1.2     # seconds — local water animation duration
_CLICK_RIPPLE_MAX_RADIUS = 160   # pixels in canvas-space
# Light blue (BGR) for both local water rings and the audience ripple.
_RIPPLE_BGR = (255, 210, 140)

# Guards compound mutations of the detection-session globals
# (_detected_ids / _render_order / _detection_timings / _detection_start_time /
# _report_saved_path) so a reset can't be observed mid-rebind by the detection
# thread.  Held only for the few microseconds of the reset itself.
_det_lock = threading.Lock()

_CALIBRATION_LOG_DIR = _os.path.join(_os.path.dirname(__file__), "debug", "calibration_logs")

def _close_timing_log():
    global _timing_log_handles, _timing_log_paths
    for f in _timing_log_handles:
        try:
            f.close()
        except Exception:
            pass
    _timing_log_handles = []
    _timing_log_paths   = []


def _open_timing_log():
    global _timing_log_paths, _timing_log_handles
    _close_timing_log()
    # Cross-reference the active debug-capture run so the video and the
    # per-blink-id timing log can always be paired up later.
    debug_ref = ""
    if dbg_cap.active and dbg_cap.run_dir:
        debug_ref = f"debug_run     {_os.path.basename(dbg_cap.run_dir)}\n"
    header = (f"detection started {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"{debug_ref}"
              f"{'blink_id':>10}  {'time_to_detect':>16}  {'confidence':>12}\n")
    paths = []
    # Always write to the master calibration_logs folder
    _os.makedirs(_CALIBRATION_LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    paths.append(_os.path.join(_CALIBRATION_LOG_DIR, f"{stamp}.log"))
    # Also duplicate into the active debug run folder if one is in progress
    if dbg_cap.active and dbg_cap.run_dir:
        paths.append(_os.path.join(dbg_cap.run_dir, "calibration.log"))
    handles = []
    for p in paths:
        f = open(p, "w", buffering=1)   # line-buffered for live tailing
        f.write(header)
        handles.append(f)
    _timing_log_paths   = paths
    _timing_log_handles = handles

def _log_timing(line: str):
    for f in _timing_log_handles:
        try:
            f.write(line + "\n")
        except Exception:
            pass


def _stop_auto_debug_capture():
    """Stop the debug capture only if WE auto-started it (so manual G
    captures aren't killed by detection toggling off)."""
    global _debug_auto_started
    if _debug_auto_started and dbg_cap.active:
        try:
            dbg_cap.stop_run()
        except Exception as e:
            log.info(f"[debug] auto-stop failed: {e}")
    _debug_auto_started = False


def _compute_iso_hint():
    """After detection ends, look at the median blink amplitude across all
    decoded phones and suggest an ISO change if it's outside the comfort
    band.  Sets _iso_hint for the sidebar; never moves the slider itself —
    the user can always overrule.

    Target amplitude band 0.6–0.9: above 0.55 keeps recent_std well above
    the gate's 0.05 floor; below 0.95 leaves headroom for noise without
    clipping.  Outside that band we suggest a ±20-25% ISO nudge — the
    actual gain→amplitude curve is roughly linear in this regime, but the
    suggestion is deliberately coarse because mixed-lighting rooms can
    push individual phones away from the median.
    """
    global _iso_hint
    if len(_detection_amplitudes) < 3:
        return
    if not elgato.connected:
        return
    import statistics
    median_amp = statistics.median(_detection_amplitudes)
    current = elgato.iso_gain
    if median_amp < 0.55 and current < 160:
        delta = max(10, int(current * 0.25))
        suggested = min(160, current + delta)
        _iso_hint = f"low signal {median_amp:.2f} — try ISO ~{suggested}"
    elif median_amp > 0.95 and current > 30:
        delta = max(10, int(current * 0.20))
        suggested = max(0, current - delta)
        _iso_hint = f"strong signal {median_amp:.2f} — could try ISO ~{suggested}"
    else:
        _iso_hint = f"signal OK ({median_amp:.2f}, n={len(_detection_amplitudes)})"
    log.info(f"[iso] {_iso_hint}")


def _log_detection_summary():
    """One-line end-of-detection summary listing connected vs detected vs
    missed blink_ids — written to both the controller log and the active
    calibration log so post-show analysis can see what the detector failed
    to find without needing to cross-reference /admin/blink_map snapshots."""
    if not _detection_start_time:
        return
    connected = set(_valid_blink_ids)
    detected  = set(_detected_ids)
    missed    = sorted(connected - detected)
    summary = (f"[detect] end  connected={len(connected)}  "
               f"detected={len(detected)}  "
               f"missed={missed}  "
               f"elapsed={time.time() - _detection_start_time:.1f}s")
    log.info(summary)
    for f in _timing_log_handles:
        try:
            f.write(summary + "\n")
        except Exception as e:
            log.info(f"[detect] summary write failed: {e}")
    _close_timing_log()
    _compute_iso_hint()


vid_rec = VideoRecorder()

# Auto-start debug capture whenever detection runs, so we always have the
# heatmap available when investigating "why didn't this phone get detected".
# Tracked separately from the manual G toggle: only the auto-started runs
# are auto-stopped on detection-off.
_DEBUG_AUTO_ON_DETECT = True
_debug_auto_started   = False


# ------------------------------------------------------------------ #
# Camera helpers
# ------------------------------------------------------------------ #

# Names that indicate virtual / software / continuity cameras to exclude
_VIRTUAL_CAM_NAMES = (
    "iphone", "ipad", "continuity", "virtual", "facetime",
    "obs", "snap camera", "mmhmm", "camo", "reincubate", "ndisourcevirtualcam",
)

def _avfoundation_device_names() -> dict[int, str]:
    """Use ffmpeg to list AVFoundation video devices → {index: name}."""
    import subprocess, re
    try:
        import shutil
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        result = subprocess.run(
            [ffmpeg, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stderr  # ffmpeg lists devices on stderr
        devices = {}
        for line in output.splitlines():
            if "audio devices" in line.lower():
                break  # stop before audio section — indices overlap with video
            m = re.search(r"\[(\d+)\]\s+(.+)", line)
            if m:
                devices[int(m.group(1))] = m.group(2).strip()
        return devices
    except Exception:
        return {}


def find_cameras(max_idx: int = 8) -> list[int]:
    names  = _avfoundation_device_names()
    no_names = not names   # ffmpeg failed — we have no name info
    found  = []
    for i in range(max_idx):
        name = names.get(i, "").lower()
        if any(v in name for v in _VIRTUAL_CAM_NAMES):
            log.info(f"[camera] skipping virtual camera [{i}] {names.get(i)}")
            continue
        # When ffmpeg can't list devices, skip index 0 — on macOS it's always
        # the built-in FaceTime/iSight camera which we never want as the default.
        if no_names and i == 0:
            log.info("[camera] skipping index 0 (no name info, assumed built-in)")
            continue
        cap = cv2.VideoCapture(i, cv2.CAP_AVFOUNDATION)
        if cap.isOpened():
            label = names.get(i, f"Camera {i}")
            log.info(f"[camera] found [{i}] {label}")
            found.append(i)
            cap.release()
    return found


def _avf_lock_exposure(camera_name: str) -> bool:
    """
    Lock exposure on a named AVFoundation camera via pyobjc.

    OpenCV's CAP_PROP_AUTO_EXPOSURE is unreliable on some cameras (e.g. Elgato
    Facecam 4K ignores it entirely).  This calls AVFoundation directly to set
    AVCaptureExposureModeLocked, preventing the camera from extending exposure
    in dark conditions and dropping fps to 2-4.

    Returns True if the lock was applied, False if unavailable or unsupported.
    """
    try:
        from AVFoundation import (AVCaptureDevice,
                                   AVCaptureExposureModeLocked)
        devices = AVCaptureDevice.devicesWithMediaType_("vide")
        for device in devices:
            if camera_name.lower() not in device.localizedName().lower():
                continue
            if not device.isExposureModeSupported_(AVCaptureExposureModeLocked):
                log.info(f"[camera] {device.localizedName()}: locked exposure not supported")
                return False
            err = None
            if device.lockForConfiguration_(err):
                device.setExposureMode_(AVCaptureExposureModeLocked)
                device.unlockForConfiguration()
                dur = device.exposureDuration()
                fps_eq = (dur.timescale / dur.value) if dur.value else 0
                log.info(
                    f"[camera] AVF exposure locked on {device.localizedName()} "
                    f"(duration={dur.value}/{dur.timescale}"
                    + (f" ≈ {fps_eq:.0f}fps" if fps_eq else "")
                    + ")"
                )
                return True
        log.info(f"[camera] AVF lock: no device matching '{camera_name}'")
    except Exception as e:
        log.info(f"[camera] AVF lock unavailable: {e}")
    return False


def _avf_relock_exposure(camera_name: str) -> bool:
    """
    Adapt-then-lock: briefly re-enable ContinuousAutoExposure so the camera
    adjusts to changed lighting conditions, then lock again at the new value.

    Called by the exposure monitor when camera fps drops below MIN_RELIABLE_FPS.
    In auto mode the camera extends exposure time in dark conditions, dropping
    fps to 2-4.  This function lets it pick a new exposure that suits the current
    room brightness, then freezes it to guarantee fast fps going forward.

    Returns True if the re-lock succeeded.
    """
    try:
        from AVFoundation import (AVCaptureDevice,
                                   AVCaptureExposureModeLocked,
                                   AVCaptureExposureModeContinuousAutoExposure)
        devices = AVCaptureDevice.devicesWithMediaType_("vide")
        for device in devices:
            if camera_name.lower() not in device.localizedName().lower():
                continue
            err = None
            # Step 1: re-enable ContinuousAE so camera adapts to new lighting
            if not device.lockForConfiguration_(err):
                log.info(f"[camera] relock: could not lock {device.localizedName()} for config")
                return False
            device.setExposureMode_(AVCaptureExposureModeContinuousAutoExposure)
            device.unlockForConfiguration()
            log.info(f"[camera] relock: ContinuousAE enabled on {device.localizedName()} — adapting...")
            time.sleep(1.5)   # let camera settle on a new exposure
            # Step 2: lock at whatever exposure the camera has now chosen
            if not device.lockForConfiguration_(err):
                return False
            device.setExposureMode_(AVCaptureExposureModeLocked)
            device.unlockForConfiguration()
            dur = device.exposureDuration()
            fps_eq = (dur.timescale / dur.value) if dur.value else 0
            log.info(
                f"[camera] relock: exposure re-locked on {device.localizedName()} "
                f"(duration={dur.value}/{dur.timescale}"
                + (f" ≈ {fps_eq:.0f}fps" if fps_eq else "")
                + ")"
            )
            return True
        log.info(f"[camera] relock: no device matching '{camera_name}'")
    except Exception as e:
        log.info(f"[camera] relock unavailable: {e}")
    return False


def _exposure_monitor_worker():
    """
    Background thread: watches camera fps.  If it drops below MIN_RELIABLE_FPS
    (indicating auto-exposure extended shutter for changed room lighting), triggers
    an adapt-and-relock cycle so fps recovers without a manual camera restart.
    """
    log.info(f"[exposure_monitor] started (check every {_EXP_MONITOR_SECS}s, "
             f"threshold={MIN_RELIABLE_FPS}fps, cooldown={_EXP_RELOCK_COOLDOWN}s)")
    time.sleep(6.0)   # startup grace — let camera stabilise first
    _last_relock    = 0.0
    _no_frames_logged = False   # suppress repeated "no frames" lines
    _last_ok_log    = 0.0       # throttle "fps OK" to once per minute
    while True:
        with state.lock:
            if not state.running:
                break
        time.sleep(_EXP_MONITOR_SECS)
        fps = _camera_fps
        if fps <= 0:
            if not _no_frames_logged:
                log.info("[exposure_monitor] no frames yet — waiting")
                _no_frames_logged = True
            continue
        if fps < MIN_RELIABLE_FPS:
            now = time.time()
            if now - _last_relock < _EXP_RELOCK_COOLDOWN:
                log.info(f"[exposure_monitor] fps={fps:.1f} still low but cooldown active "
                         f"({_EXP_RELOCK_COOLDOWN - (now - _last_relock):.0f}s remaining)")
                continue
            log.info(f"[exposure_monitor] fps={fps:.1f} < {MIN_RELIABLE_FPS} — triggering re-lock")
            _avf_relock_exposure("elgato")
            _last_relock = now
        else:
            now = time.time()
            if now - _last_ok_log >= 60.0:
                log.info(f"[exposure_monitor] fps={fps:.1f} OK")
                _last_ok_log = now


def open_camera(idx: int) -> cv2.VideoCapture | None:
    cap = cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)
    # Lock exposure so the camera doesn't slow to 2-4 fps in dark rooms.
    # On AVFoundation: 0 = locked (manual), non-zero = continuous auto.
    # With auto-exposure, the camera extends exposure time in dark conditions —
    # this drops fps from ~14 to 2-3 fps even with self-lit phone screens in
    # view.  Locked exposure keeps fps high regardless of ambient light.
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0)   # 0 = locked on AVFoundation

    # Request short exposure to ensure fast fps.
    # -6 ≈ 1/64 s on supported cameras.  Elgato Facecam 4K ignores this via
    # OpenCV (returns 0.0), but it's harmless and works on other cameras.
    cap.set(cv2.CAP_PROP_EXPOSURE, -6)

    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    actual_ae  = cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
    actual_exp = cap.get(cv2.CAP_PROP_EXPOSURE)
    log.info(f"[camera] idx={idx} actual_fps={actual_fps:.1f} ae={actual_ae} exposure={actual_exp}")

    # Belt-and-suspenders: directly lock via AVFoundation for cameras (like the
    # Elgato Facecam 4K) that ignore OpenCV's CAP_PROP_AUTO_EXPOSURE.
    # We try common Elgato name fragments; harmless if camera not found.
    _avf_lock_exposure("elgato")

    return cap


# ------------------------------------------------------------------ #
# Texture conversion
# ------------------------------------------------------------------ #

# Pre-allocated buffers reused every frame — avoids allocating 14 MB/frame
# which was the main cause of GC pauses and FPS jitter.
_tex_u8:  np.ndarray | None = None   # uint8 RGBA staging buffer
_tex_f32: np.ndarray | None = None   # float32 RGBA output buffer

def frame_to_texture(bgr: np.ndarray) -> np.ndarray:
    global _tex_u8, _tex_f32
    h, w = bgr.shape[:2]
    if _tex_f32 is None or _tex_f32.shape != (h, w, 4):
        _tex_u8  = np.zeros((h, w, 4), dtype=np.uint8)
        _tex_f32 = np.empty((h, w, 4), dtype=np.float32)
    cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA, dst=_tex_u8)
    np.multiply(_tex_u8, 1.0 / 255.0, out=_tex_f32)
    return _tex_f32.ravel()


# ------------------------------------------------------------------ #
# Canvas builder
# ------------------------------------------------------------------ #

def build_canvas(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    scale = max(PREVIEW_WIDTH / w, PREVIEW_HEIGHT / h)
    nw, nh = int(w * scale), int(h * scale)
    resized = cv2.resize(frame, (nw, nh))

    cx = max((nw - PREVIEW_WIDTH) // 2, 0)
    cy = max((nh - PREVIEW_HEIGHT) // 2, 0)
    canvas = resized[cy:cy + PREVIEW_HEIGHT, cx:cx + PREVIEW_WIDTH].copy()

    with state.lock:
        state.cam_offset      = (0, 0)
        state.cam_frame_size  = (PREVIEW_WIDTH, PREVIEW_HEIGHT)
        state.last_render_scale = scale
        state.last_crop_x     = cx
        state.last_crop_y     = cy

    return canvas


def no_camera_canvas() -> np.ndarray:
    canvas = np.zeros((PREVIEW_HEIGHT, PREVIEW_WIDTH, 3), dtype=np.uint8)
    for x in range(0, PREVIEW_WIDTH, 80):
        cv2.line(canvas, (x, 0), (x, PREVIEW_HEIGHT), (30, 30, 30), 1)
    for y in range(0, PREVIEW_HEIGHT, 80):
        cv2.line(canvas, (0, y), (PREVIEW_WIDTH, y), (30, 30, 30), 1)
    msg = "No camera detected"
    (tw, _), _ = cv2.getTextSize(msg, FONT, 0.9, 2)
    cv2.putText(canvas, msg,
                (PREVIEW_WIDTH // 2 - tw // 2, PREVIEW_HEIGHT // 2),
                FONT, 0.9, (160, 160, 160), 2, cv2.LINE_AA)
    return canvas


# ------------------------------------------------------------------ #
# Detection overlay helpers
# ------------------------------------------------------------------ #

def draw_device_overlay(canvas: np.ndarray):
    if not _overlays_on():
        return
    with state.lock:
        positions    = state.calibrated_positions.copy()
        crop_x       = state.last_crop_x
        crop_y       = getattr(state, "last_crop_y", 0)
        show_render  = state.overlay_show_render

    # Derive render-order labels directly from current positions so the
    # overlay never shows '?' just because _render_order is mid-rebuild.
    render_map = {}
    if show_render:
        visible = [b for b in positions
                   if not _valid_blink_ids or b in _valid_blink_ids]
        for rank, bid in enumerate(
            sorted(visible, key=lambda b: positions[b]["u"]), 1
        ):
            render_map[bid] = rank

    for blink_id_str, pos in positions.items():
        blink_id = int(blink_id_str)
        if _valid_blink_ids and blink_id not in _valid_blink_ids:
            continue
        u, v = pos["u"], pos["v"]
        px = int(u * (PREVIEW_WIDTH  + 2 * crop_x) - crop_x)
        py = int(v * (PREVIEW_HEIGHT + 2 * crop_y) - crop_y)
        label = str(render_map[blink_id]) if show_render else str(blink_id + 1)
        font_scale = 0.55
        (tw, th), _ = cv2.getTextSize(label, FONT, font_scale, 1)
        pad = 5
        x1, y1 = px - tw // 2 - pad, py - th // 2 - pad - 1
        x2, y2 = px + tw // 2 + pad, py + th // 2 + pad + 1
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (20, 20, 20), -1)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 220, 80), 2)
        cv2.putText(canvas, label, (px - tw // 2, py + th // 2),
                    FONT, font_scale, (255, 255, 255), 1, cv2.LINE_AA)


def _overlays_on() -> bool:
    with state.lock:
        return state.show_overlays


def draw_roi_overlay(canvas: np.ndarray):
    """Dim the excluded ROI regions and draw boundary lines."""
    if not _overlays_on():
        return
    roi_top    = detector.cfg.get("roi_top_frac",    0.0)
    roi_bottom = detector.cfg.get("roi_bottom_frac", 0.0)
    roi_left   = detector.cfg.get("roi_left_frac",   0.0)
    roi_right  = detector.cfg.get("roi_right_frac",  0.0)
    if roi_top == 0.0 and roi_bottom == 0.0 and roi_left == 0.0 and roi_right == 0.0:
        return
    with state.lock:
        scale  = state.last_render_scale
        crop_x = state.last_crop_x
        crop_y = getattr(state, "last_crop_y", 0)
    h, w = canvas.shape[:2]
    y1 = max(0, min(h - 1, int(roi_top    * CAM_HEIGHT * scale) - crop_y))
    y2 = max(0, min(h - 1, h - int(roi_bottom * CAM_HEIGHT * scale) + crop_y))
    x1 = max(0, min(w - 1, int(roi_left   * CAM_WIDTH  * scale) - crop_x))
    x2 = max(0, min(w - 1, w - int(roi_right  * CAM_WIDTH  * scale) + crop_x))
    color = (80, 160, 255)
    if y1 > 0:
        canvas[:y1, :] //= 3
        cv2.line(canvas, (0, y1), (w - 1, y1), color, 1)
    if y2 < h - 1:
        canvas[y2:, :] //= 3
        cv2.line(canvas, (0, y2), (w - 1, y2), color, 1)
    if x1 > 0:
        canvas[:, :x1] //= 3
        cv2.line(canvas, (x1, 0), (x1, h - 1), color, 1)
    if x2 < w - 1:
        canvas[:, x2:] //= 3
        cv2.line(canvas, (x2, 0), (x2, h - 1), color, 1)
    parts = []
    if roi_top    > 0: parts.append(f"top {int(roi_top * 100)}%")
    if roi_bottom > 0: parts.append(f"bot {int(roi_bottom * 100)}%")
    if roi_left   > 0: parts.append(f"left {int(roi_left * 100)}%")
    if roi_right  > 0: parts.append(f"right {int(roi_right * 100)}%")
    label1 = "ROI  " + "  ".join(parts)

    inner_frac = max(0.0, 1.0 - roi_top - roi_bottom) * \
                 max(0.0, 1.0 - roi_left - roi_right)
    saved_pct = (1.0 - inner_frac) * 100
    saved_px  = int((1.0 - inner_frac) * CAM_WIDTH * CAM_HEIGHT)
    if saved_px >= 1_000_000:
        saved_str = f"{saved_px / 1_000_000:.1f}M px"
    elif saved_px >= 1_000:
        saved_str = f"{saved_px / 1_000:.0f}K px"
    else:
        saved_str = f"{saved_px} px"
    label2 = f"saved {saved_str}  ({saved_pct:.0f}%)"

    (tw1, th1), _ = cv2.getTextSize(label1, FONT, 0.4, 1)
    (tw2, th2), _ = cv2.getTextSize(label2, FONT, 0.4, 1)
    tw = max(tw1, tw2)
    line_gap = 6
    pad_x, pad_y = 10, 7
    tx = x1 + 18
    ty1 = y1 + 18 + th1
    ty2 = ty1 + line_gap + th2
    bg_x0 = tx - pad_x
    bg_y0 = ty1 - th1 - pad_y
    bg_x1 = tx + tw + pad_x
    bg_y1 = ty2 + pad_y
    cv2.rectangle(canvas, (bg_x0, bg_y0), (bg_x1, bg_y1), (8, 8, 10), -1)
    cv2.rectangle(canvas, (bg_x0, bg_y0), (bg_x1, bg_y1), color, 1)
    cv2.putText(canvas, label1, (tx, ty1),
                FONT, 0.4, color, 1, cv2.LINE_AA)
    cv2.putText(canvas, label2, (tx, ty2),
                FONT, 0.4, (180, 200, 230), 1, cv2.LINE_AA)


_WINNER_HIGHLIGHT_SECS = 6.0


def draw_winner_highlight(canvas: np.ndarray):
    """Pulsing gold ring + 'WINNER #N' label at the bug-game winner's
    position, for ~6s after the round ends."""
    bid, at = game.get_last_winner()
    if bid is None:
        return
    age = time.time() - at
    if age > _WINNER_HIGHLIGHT_SECS:
        return
    with state.lock:
        positions = state.calibrated_positions.copy()
        crop_x    = state.last_crop_x
        crop_y    = getattr(state, "last_crop_y", 0)
    pos = positions.get(bid)
    if pos is None:
        return
    px = int(pos["u"] * (PREVIEW_WIDTH  + 2 * crop_x) - crop_x)
    py = int(pos["v"] * (PREVIEW_HEIGHT + 2 * crop_y) - crop_y)
    pulse  = 0.5 + 0.5 * math.sin(time.time() * 6.0)
    base_r = 28
    r      = int(base_r + 12 * pulse)
    gold   = (50, 200, 250)   # BGR — warm gold
    glow   = (80, 220, 255)
    cv2.circle(canvas, (px, py), r + 8, glow, 3, cv2.LINE_AA)
    cv2.circle(canvas, (px, py), r,     gold, 4, cv2.LINE_AA)
    label = f"WINNER  #{bid + 1}"
    font_scale = 0.8
    thickness  = 2
    (tw, th), _ = cv2.getTextSize(label, FONT, font_scale, thickness)
    lx = px - tw // 2
    ly = py - r - 18
    pad = 7
    cv2.rectangle(canvas, (lx - pad, ly - th - pad),
                  (lx + tw + pad, ly + pad), (8, 8, 12), -1)
    cv2.rectangle(canvas, (lx - pad, ly - th - pad),
                  (lx + tw + pad, ly + pad), gold, 1)
    cv2.putText(canvas, label, (lx, ly),
                FONT, font_scale, gold, thickness, cv2.LINE_AA)


def draw_detect_border(canvas: np.ndarray):
    """Thick green inset border drawn on the canvas while detecting."""
    h, w = canvas.shape[:2]
    thickness = 8
    color = (40, 220, 90)
    half = thickness // 2
    cv2.rectangle(canvas, (half, half), (w - 1 - half, h - 1 - half),
                  color, thickness, cv2.LINE_AA)


def draw_hud(canvas: np.ndarray, fps: float):
    with state.lock:
        detecting = state.detecting

    dot_color  = (40, 210, 80) if detecting else (70, 70, 70)
    det_str    = f" / {int(_detect_fps + 0.5)}" if detecting else ""
    label      = f"{int(fps + 0.5)}{det_str} fps"

    PAD = 6
    font_scale, thickness = 0.5, 1
    (tw, th), _ = cv2.getTextSize(label, FONT, font_scale, thickness)

    x, y  = 8, 8
    bx1   = x + PAD * 2 + 14 + tw
    by1   = y + PAD * 2 + th
    mid_y = (y + by1) // 2

    cv2.rectangle(canvas, (x, y), (bx1, by1), (18, 18, 18), -1)
    cv2.rectangle(canvas, (x, y), (bx1, by1), (55, 55, 55), 1)
    cv2.circle(canvas, (x + PAD + 5, mid_y), 4, dot_color, -1)
    cv2.putText(canvas, label, (x + PAD + 14, y + PAD + th),
                FONT, font_scale, (210, 210, 210), thickness, cv2.LINE_AA)

    # detected / connected counter — pill to the right of the fps pill
    # while detecting, so the operator can see how many phones are
    # outstanding without waiting for the post-run calibration log.
    if detecting:
        n_det  = len(_detected_ids)
        n_conn = len(_valid_blink_ids)
        count_label = f"{n_det} / {n_conn} found"
        (cw, _), _ = cv2.getTextSize(count_label, FONT, font_scale, thickness)
        cx = bx1 + 16
        cx2 = cx + PAD * 2 + cw
        # Colour the box edge green when caught up, amber while still chasing.
        edge = (40, 210, 80) if n_conn and n_det >= n_conn else (0, 165, 255)
        cv2.rectangle(canvas, (cx, y), (cx2, by1), (18, 18, 18), -1)
        cv2.rectangle(canvas, (cx, y), (cx2, by1), edge, 1)
        cv2.putText(canvas, count_label, (cx + PAD, y + PAD + th),
                    FONT, font_scale, (210, 210, 210), thickness, cv2.LINE_AA)

    # Bottom-right: show friendly debug run name when debug capture is active
    if dbg_cap.active and dbg_cap.run_dir:
        run_name = _os.path.basename(dbg_cap.run_dir)
        h, w = canvas.shape[:2]
        (nw, nh), _ = cv2.getTextSize(run_name, FONT, font_scale, thickness)
        nx = w - nw - PAD * 2 - 2
        ny = h - PAD * 2 - 2
        cv2.putText(canvas, run_name, (nx, ny),
                    FONT, font_scale, (60, 180, 255), thickness, cv2.LINE_AA)

# ------------------------------------------------------------------ #
# UI helpers
# ------------------------------------------------------------------ #

def set_status(text: str):
    with state.lock:
        state.status_text = text


def safe_set(tag: str, value):
    ui_queue.put((tag, value))


def update_ui_from_state():
    with state.lock:
        status    = state.status_text
        clients   = state.client_count
        detecting = state.detecting
        effect    = state.current_effect

    dbg_label = f"Debug: REC ({dbg_cap.frame_idx} frames)" if dbg_cap.active else ""

    safe_set("status_text",    status)
    safe_set("clients_text",   f"Clients: {clients}")
    safe_set("detect_text",    f"Clients detected: {len(_detected_ids)}")
    # state.current_effect now carries "ripple" while armed (set by
    # _set_ripple_armed), so the highlight follows naturally.
    ui_queue.put(("_active_effect", effect))

    safe_set("rec_status_text", "[REC]" if vid_rec.active else "")
    ui_queue.put(("_rec_status_show", vid_rec.active))
    rec_path = getattr(vid_rec, "_path", "") if vid_rec.active else ""
    safe_set("rec_filename_text", _os.path.basename(rec_path) if rec_path else "")
    ui_queue.put(("_rec_filename_show", bool(rec_path)))

    safe_set("iso_hint_text",    _iso_hint)
    _safe_set_chk("chk_detection",    detecting)
    _safe_set_chk("chk_sync",         state.syncing)
    _safe_set_chk("chk_overlays_all", state.show_overlays)
    _safe_set_chk("chk_overlays",     state.show_device_overlay)
    _safe_set_chk("chk_overlay_pos",  state.overlay_show_render)
    _safe_set_chk("chk_debug",        dbg_cap.active)
    _safe_set_chk("chk_recording",    vid_rec.active)
    _safe_set_chk("chk_flip_projection", state.flip_projection)
    ui_queue.put(("_roi_enabled", not detecting))

    _push_elgato_state()


# ------------------------------------------------------------------ #
# Elgato callbacks
# ------------------------------------------------------------------ #

def _push_elgato_state():
    connected = elgato.connected
    safe_set("elgato_status", "[ON]" if connected else "[OFF]")
    ui_queue.put(("elgato_color", connected))
    _safe_set_chk("chk_ae", elgato.ae_on)
    safe_set("sld_iso", elgato.iso_gain)
    ui_queue.put(("_elgato_enabled", connected))


def _elgato_state_changed():
    """Called by elgato watchdog (background thread) when state changes."""
    _push_elgato_state()


def _toggle_ae():
    if _ui_syncing:
        return
    elgato.set_ae(dpg.get_value("chk_ae"))


def _set_iso(sender, value):
    if _ui_syncing:
        return
    elgato.set_iso(int(value))


def toggle_flip_projection():
    with state.lock:
        state.flip_projection = not state.flip_projection
        on = state.flip_projection
    set_status(f"Projection flip {'ON' if on else 'OFF'}")


def _apply_detection_iso():
    """Drop ISO back to the low-gain default for clean blink detection."""
    if elgato.connected:
        elgato.set_iso(elgato._DEFAULT_GAIN)


def _apply_audience_iso():
    """Bump ISO so the live camera feed of the crowd reads brightly during
    the show."""
    if elgato.connected:
        elgato.set_iso(_AUDIENCE_ISO_GAIN)


# ------------------------------------------------------------------ #
# Actions
# ------------------------------------------------------------------ #

def _no_camera() -> bool:
    """Return True and set status if no camera is active."""
    with state.lock:
        active = state.camera_active
    if not active:
        set_status("No camera")
        return True
    return False


def toggle_sync():
    if _no_camera():
        return
    with state.lock:
        state.syncing = not state.syncing
        val = state.syncing
    post_json_async("/admin/sync", {"sync": val})


def _auto_enable_sync():
    """Turn clock sync ON when detection ends, so the moment phones are
    located they're also rendering aligned animations.  No-op if sync was
    already on."""
    with state.lock:
        already_on = state.syncing
        state.syncing = True
    if not already_on:
        post_json_async("/admin/sync", {"sync": True})
        log.info("[sync] auto-enabled after detection")


def toggle_detection():
    log.info(f"[toggle_detection] called  ui_syncing={_ui_syncing}")
    if _ui_syncing:
        log.info("[toggle_detection] blocked by _ui_syncing")
        return
    if _no_camera():
        log.info("[toggle_detection] blocked by _no_camera")
        return
    with state.lock:
        state.detecting = not state.detecting
        val = state.detecting

    if val and not _valid_blink_ids:
        with state.lock:
            state.detecting = False
        set_status("No clients connected")
        return

    if val:
        global _detection_start_time, _detected_ids, _render_order
        global _debug_auto_started, _report_saved_path, _iso_hint
        with _det_lock:
            _detection_start_time = time.time()
            _detected_ids = set()
            _render_order.clear()
            _report_saved_path = None
            _detection_amplitudes.clear()
            _iso_hint = ""
        with state.lock:
            state.overlay_show_render = False
        # Reset detector internal state (_ever_active, history, diff accum) so
        # accumulated points from previous runs don't slow process_frame.
        # Already-found positions are preserved in state.calibrated_positions and
        # on the server — the detector state does not need to carry over.
        ever_active_before = len(detector._ever_active)
        detector.reset()
        log.info(f"[detect] detector reset on run start (cleared {ever_active_before} _ever_active points)")
        # Auto-start debug capture so the heatmap + frames are always there
        # for post-show diagnostics. Skipped if a manual G capture is already
        # running (we don't want to interfere with intentional ones).
        if _DEBUG_AUTO_ON_DETECT and not dbg_cap.active:
            try:
                dbg_cap.start_run()
                _debug_auto_started = True
            except Exception as e:
                log.info(f"[debug] auto-start failed: {e}")
        _apply_detection_iso()
        post_json_async("/admin/detect", {"detecting": True})
        _open_timing_log()
        set_status("Detection ON")
    else:
        _log_detection_summary()
        _save_report()
        _stop_auto_debug_capture()
        post_json_async("/admin/detect", {"detecting": False})
        _auto_enable_sync()
        _apply_audience_iso()
        set_status("Detection OFF")


def toggle_debug():
    """Start or stop a debug capture run (hotkey G)."""
    if _ui_syncing:
        return
    if _no_camera():
        return
    if dbg_cap.active:
        dbg_cap.stop_run()
        set_status("Debug capture stopped")
    else:
        run_dir = dbg_cap.start_run()
        set_status(f"Debug: {run_dir}")


def toggle_recording():
    """Start or stop a plain video recording (hotkey V). Independent of debug capture."""
    if vid_rec.active:
        path = vid_rec.stop()
        set_status(f"Recording saved: {_os.path.basename(path)}")
    else:
        if _no_camera():
            return
        path = vid_rec.start()
        set_status(f"Recording: {_os.path.basename(path)}")


_SIDEBAR_WIDTH = 314


def toggle_sidebar():
    with state.lock:
        state.sidebar_visible = not state.sidebar_visible
        vis = state.sidebar_visible
    # Width must collapse alongside show=False so the horizontal group
    # actually reflows; otherwise the sidebar's slot stays reserved.
    dpg.configure_item("sidebar_panel",
                       show=vis,
                       width=_SIDEBAR_WIDTH if vis else 0)


def toggle_device_overlay():
    if _no_camera():
        return
    with state.lock:
        state.show_device_overlay = not state.show_device_overlay

def toggle_overlay_mode():
    with state.lock:
        state.overlay_show_render = not state.overlay_show_render
    mode = "render order" if state.overlay_show_render else "IDs"
    set_status(f"Overlay: {mode}")


def toggle_all_overlays():
    with state.lock:
        state.show_overlays = not state.show_overlays
        val = state.show_overlays
    set_status(f"Overlays {'ON' if val else 'OFF'}")


def _set_ripple_armed(armed: bool):
    """Internal: toggle armed state.  Drives state.current_effect to
    "ripple" while armed so both the sidebar button highlight AND the
    effect preview pane reflect ripple immediately (previously the
    preview kept showing the last fired effect — wave/pulse/etc — until
    a click actually triggered the audience message).

    On arm we also synchronously clear the audience to black so the
    leftover colour from the previous effect doesn't sit on the phones
    while the operator lines up a click.  Sync (not async) because the
    next thing the user does is click the preview, which fires
    trigger_ripple_at → /admin/effect/fire — async posts here would race
    that fire through the network thread pool and the trailing stop
    could clear a just-fired ripple.  Same bug class as the old
    switch-effects race; sync makes the ordering deterministic."""
    global _ripple_armed
    if armed == _ripple_armed:
        return
    _ripple_armed = armed
    with state.lock:
        if armed:
            state.current_effect = "ripple"
        elif state.current_effect == "ripple":
            state.current_effect = None
    if armed:
        post_json("/admin/effect/stop", {}, timeout=0.3)
    else:
        _click_ripples.clear()


def toggle_ripple_arm():
    """Click on the sidebar 'Ripple' button: arm/disarm click-to-ripple mode.
    Does NOT fire the audience effect — that only happens on a preview click."""
    _set_ripple_armed(not _ripple_armed)
    set_status(f"Ripple {'armed' if _ripple_armed else 'disarmed'}")


def _set_spotlight_armed(armed: bool):
    global _spotlight_armed, _last_spotlight_canvas_px
    if armed == _spotlight_armed:
        return
    _spotlight_armed = armed
    with state.lock:
        if armed:
            state.current_effect = "spotlight"
        elif state.current_effect == "spotlight":
            state.current_effect = None
    if armed:
        # Arming swaps any current effect for spotlight; the per-frame
        # broadcast that follows takes over from there.  Clear synchronously
        # so the ripple-style switch race doesn't bite (network thread pool
        # could otherwise reorder a stale stop after the first broadcast).
        _set_ripple_armed(False)
        post_json("/admin/effect/stop", {}, timeout=0.3)
    else:
        _last_spotlight_canvas_px = None
        post_json_async("/admin/effect/stop", {})


def toggle_spotlight_arm():
    """Sidebar 'Spotlight' button: arm/disarm the cursor-follow spotlight."""
    _set_spotlight_armed(not _spotlight_armed)
    set_status(f"Spotlight {'armed' if _spotlight_armed else 'disarmed'}")


def _spotlight_cursor_canvas_xy() -> tuple[int, int] | None:
    """If the operator's mouse is over the camera preview, return the
    cursor position mapped into display_canvas pixel coordinates.  Returns
    None when the cursor is off the preview (so the spotlight pauses
    instead of stuttering at the last in-bounds position)."""
    if not dpg.does_item_exist("preview_image"):
        return None
    if not dpg.is_item_hovered("preview_image"):
        return None
    mouse    = dpg.get_mouse_pos(local=False)
    img_min  = dpg.get_item_rect_min("preview_image")
    img_size = dpg.get_item_rect_size("preview_image")
    if img_size[0] <= 0 or img_size[1] <= 0:
        return None
    disp_x = mouse[0] - img_min[0]
    disp_y = mouse[1] - img_min[1]
    if disp_x < 0 or disp_y < 0 or disp_x >= img_size[0] or disp_y >= img_size[1]:
        return None
    px = int(disp_x * PREVIEW_WIDTH  / img_size[0])
    py = int(disp_y * PREVIEW_HEIGHT / img_size[1])
    return (px, py)


def _broadcast_spotlight_if_due(canvas_px: int, canvas_py: int):
    """Convert cursor canvas pixel → room u,v and throttle-broadcast.
    canvas_px/py are in the display_canvas (post-flip) frame; we unflip
    so phones receive their actual room position."""
    global _last_spotlight_broadcast_ts
    now = time.time()
    if now - _last_spotlight_broadcast_ts < (1.0 / _SPOTLIGHT_BROADCAST_HZ):
        return
    _last_spotlight_broadcast_ts = now
    room_x = (PREVIEW_WIDTH - canvas_px) if state.flip_projection else canvas_px
    with state.lock:
        crop_x = state.last_crop_x
        crop_y = state.last_crop_y
    u = (room_x + crop_x) / max(1, (PREVIEW_WIDTH  + 2 * crop_x))
    v = (canvas_py + crop_y) / max(1, (PREVIEW_HEIGHT + 2 * crop_y))
    u = max(0.0, min(1.0, u))
    v = max(0.0, min(1.0, v))
    effects.trigger_spotlight_at(u, v)


def _draw_spotlight_cursor(display_canvas):
    """Paint a glowing disc + ring at the spotlight cursor so the audience
    can see where the operator is pointing on the projection.  No-op when
    spotlight isn't armed or the cursor is off the preview."""
    global _last_spotlight_canvas_px, _last_spotlight_canvas_r
    if not _spotlight_armed:
        return
    cursor = _spotlight_cursor_canvas_xy()
    if cursor is None:
        return
    px, py = cursor
    _last_spotlight_canvas_px = cursor

    # Radius shown on canvas matches the audience effect's actual reach so
    # the operator's circle previews what the phones will light.
    try:
        radius_u = float(dpg.get_value("fx_spotlight_spatial_freq") or 0.18)
    except Exception:
        radius_u = 0.18
    circle_r = max(20, int(radius_u * PREVIEW_WIDTH * 0.5))
    _last_spotlight_canvas_r = circle_r

    # Soft filled disc — alpha-blended over the canvas so the audience
    # sees a gentle glow rather than an opaque blob.
    overlay = display_canvas.copy()
    cv2.circle(overlay, (px, py), circle_r, (200, 220, 255), -1, cv2.LINE_AA)
    cv2.addWeighted(overlay, 0.20, display_canvas, 0.80, 0, display_canvas)
    # Sharp ring + cursor dot so the operator can pinpoint where they are.
    cv2.circle(display_canvas, (px, py), circle_r, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.circle(display_canvas, (px, py), 6, (60, 200, 255), -1, cv2.LINE_AA)
    cv2.circle(display_canvas, (px, py), 6, (255, 255, 255), 1, cv2.LINE_AA)

    _broadcast_spotlight_if_due(px, py)


def stop_current_effect():
    """Tell the audience to clear whatever effect they're rendering and
    reset the controller's tracked current_effect so the sidebar highlight
    drops."""
    post_json_async("/admin/effect/stop", {})
    with state.lock:
        state.current_effect = None
    set_status("Effect stopped")


def fire_effect_and_disarm_ripple(name: str):
    """Sidebar effect-button callback for everything except Ripple itself.
    Click semantics: same effect as the active one → stop; different one
    → just fire the new (each phone's WebSocket delivers the new effect
    message after the old in-order, which already replaces all the per-
    effect params on the audience).  Ripple arm-state is always cleared
    so its highlight doesn't linger.

    A previous version POSTed /admin/effect/stop before /admin/effect/fire
    on a switch, but those two async calls raced through the network
    executor's worker pool — when fire landed first you'd get the effect
    set then immediately cleared by the trailing stop, so users had to
    click twice to switch effects."""
    _set_ripple_armed(False)
    _set_spotlight_armed(False)
    with state.lock:
        current = state.current_effect
    if current == name:
        stop_current_effect()
        return
    effects.trigger_effect(name)


def _on_preview_click(sender, app_data):
    """Map a left-click on the preview image to a u,v in the room, fire the
    audience ripple from there, and spawn a local water animation at the
    click point.  No-op when ripple is disarmed or the click was outside
    the preview widget."""
    if not _ripple_armed:
        return
    if not dpg.does_item_exist("preview_image"):
        return
    if not dpg.is_item_hovered("preview_image"):
        return
    mouse = dpg.get_mouse_pos(local=False)
    img_min  = dpg.get_item_rect_min("preview_image")
    img_size = dpg.get_item_rect_size("preview_image")
    if img_size[0] <= 0 or img_size[1] <= 0:
        return
    disp_x = mouse[0] - img_min[0]
    disp_y = mouse[1] - img_min[1]
    if disp_x < 0 or disp_y < 0 or disp_x >= img_size[0] or disp_y >= img_size[1]:
        return
    # Map display pixels → canvas pixels (preview is aspect-fitted).  When
    # the projection is flipped, invert x so the canvas-space coords land
    # on the actual phone in the room — clicks then ripple at the spot the
    # operator pointed at, and draw_click_ripples renders on the unflipped
    # canvas which gets mirrored back on display.
    cx = int(disp_x * PREVIEW_WIDTH  / img_size[0])
    cy = int(disp_y * PREVIEW_HEIGHT / img_size[1])
    if state.flip_projection:
        cx = PREVIEW_WIDTH - cx

    # Look up the nearest detected phone (in canvas-pixel space) so the local
    # half-arch can open toward it.  No detections yet → leave theta None and
    # the draw falls back to a full ring.
    with state.lock:
        positions = state.calibrated_positions.copy()
        crop_x = state.last_crop_x
        crop_y = state.last_crop_y
    # Map canvas → u,v in the original camera frame (inverse of
    # draw_device_overlay's u→px transform).
    u = (cx + crop_x) / max(1, (PREVIEW_WIDTH  + 2 * crop_x))
    v = (cy + crop_y) / max(1, (PREVIEW_HEIGHT + 2 * crop_y))
    u = max(0.0, min(1.0, u))
    v = max(0.0, min(1.0, v))

    # Find the nearest detected phone (in u,v space).  Used for three things:
    # the local half-arch animation opens toward it, the wave_angle sent to
    # the audience tells the ripple which direction to go super bright, and
    # the click-to-nearest distance scales the wave's travel speed (close
    # click → slow intimate wave; distant click → fast energetic wave).
    theta_deg: float | None = None
    wave_angle_deg: float | None = None
    nearest_dist: float | None = None
    if positions:
        nearest = None
        best_d2 = None
        for p in positions.values():
            d2 = (p["u"] - u) ** 2 + (p["v"] - v) ** 2
            if best_d2 is None or d2 < best_d2:
                best_d2 = d2
                nearest = p
        if nearest is not None and best_d2 and best_d2 > 1e-6:
            nearest_dist = math.sqrt(best_d2)
            wave_angle_deg = math.degrees(math.atan2(nearest["v"] - v,
                                                     nearest["u"] - u))
            # Local canvas-pixel angle for the half-arch drawing
            px = int(nearest["u"] * (PREVIEW_WIDTH  + 2 * crop_x) - crop_x)
            py = int(nearest["v"] * (PREVIEW_HEIGHT + 2 * crop_y) - crop_y)
            theta_deg = math.degrees(math.atan2(py - cy, px - cx))

    # Speed multiplier from cursor-to-nearest-phone distance.
    # 0.0 (click right on a phone)  → 0.45× (slow, dramatic)
    # 0.3 (typical near-cluster)    → 0.90×
    # 0.7 (between clusters)        → 1.50×
    # 1.0+ (corner click, far away) → 1.95×+ (whip-fast)
    # No phones detected yet → use the slider value unchanged.
    if nearest_dist is None:
        speed_mult = 1.0
    else:
        speed_mult = 0.45 + nearest_dist * 1.5

    _click_ripples.append((cx, cy, time.time(), theta_deg))
    effects.trigger_ripple_at(u, v, wave_angle_deg, speed_mult)


def draw_click_ripples(canvas: np.ndarray):
    """Render in-flight click ripples on top of the canvas as concentric
    half-arches that open toward the nearest detected phone — cosmetic
    feedback for the operator; not visible to the audience.  Falls back
    to full rings when no phone has been located yet (no direction to
    aim at)."""
    if not _click_ripples:
        return
    now = time.time()
    alive: list[tuple[int, int, float, float | None]] = []
    for cx, cy, t0, theta in _click_ripples:
        age = now - t0
        if age >= _CLICK_RIPPLE_LIFETIME:
            continue
        alive.append((cx, cy, t0, theta))
        prog = age / _CLICK_RIPPLE_LIFETIME
        # Draw three rings spaced in time so the effect feels like water.
        for ring in range(3):
            ring_prog = prog - ring * 0.18
            if ring_prog <= 0 or ring_prog >= 1:
                continue
            radius = int(ring_prog * _CLICK_RIPPLE_MAX_RADIUS)
            alpha  = (1 - ring_prog) ** 1.5
            color = tuple(int(c * alpha) for c in _RIPPLE_BGR)
            thickness = max(1, int(3 * alpha))
            if theta is None:
                cv2.circle(canvas, (cx, cy), radius, color, thickness, cv2.LINE_AA)
            else:
                # 180° arc facing theta (image y is down, so atan2 already
                # matches OpenCV's clockwise-from-+x convention).
                cv2.ellipse(canvas, (cx, cy), (radius, radius), 0,
                            theta - 90, theta + 90,
                            color, thickness, cv2.LINE_AA)
    _click_ripples[:] = alive


def _set_roi():
    if _ui_syncing:
        return
    with state.lock:
        if state.detecting:
            return   # guard: ROI rebuild races with draw_overlay during detection
    top    = dpg.get_value("sld_roi_top")    / 100.0
    bottom = dpg.get_value("sld_roi_bottom") / 100.0
    left   = dpg.get_value("sld_roi_left")   / 100.0
    right  = dpg.get_value("sld_roi_right")  / 100.0
    detector.cfg["roi_top_frac"]    = top
    detector.cfg["roi_bottom_frac"] = bottom
    detector.cfg["roi_left_frac"]   = left
    detector.cfg["roi_right_frac"]  = right
    # Auto-enable overlays when any ROI is active so the dimmed exclusion
    # region and boundary line are visible while tuning.
    if (top + bottom + left + right) > 0:
        with state.lock:
            state.show_overlays = True


def _save_report(auto_open: bool = False):
    """Generate and save a post-show report. Safe to call with no data.
    auto_open=True opens the file in the default text editor — only the
    explicit reset path passes True; detection-stop calls leave the file
    on disk silently so a report doesn't pop up mid-show.

    Idempotent within a session: a show typically ends via auto-stop (silent
    save) followed by the user hitting Reset (auto_open save), which would
    otherwise write two identical files. Second+ calls re-open the original
    instead of re-writing."""
    global _detection_timings, _report_saved_path
    if _report_saved_path is not None:
        if auto_open:
            import subprocess
            subprocess.Popen(["open", _report_saved_path])
        return
    if not _detected_ids and not game.game_order:
        return   # nothing to report
    try:
        stats = fetch_json("/admin/show_stats") or {}
        path  = report.generate(
            detected_ids      = set(_detected_ids),
            detection_timings = dict(_detection_timings),
            detection_start   = _detection_start_time,
            like_count        = stats.get("like_count", 0),
            total_connected   = stats.get("total_connected", len(_detected_ids)),
            game_results      = dict(game.game_results),
            game_order        = list(game.game_order),
        )
        _report_saved_path = path
        if auto_open:
            import subprocess
            subprocess.Popen(["open", path])
        set_status(f"Report saved → {_os.path.basename(path)}")
        log.info(f"[report] saved → {path}")
    except Exception as e:
        log.warning(f"[report] failed: {e}")


def reset_server():
    global _detected_ids, _detection_start_time, _render_order, _detection_timings, _iso_hint
    # Save the run report to disk but don't auto-open it on reset — popping
    # the file viewer mid-show is jarring.  Detection-end still opens.
    _save_report(auto_open=False)
    post_json_async("/admin/reset", {})
    detector.reset()
    with _det_lock:
        _detected_ids = set()
        _render_order.clear()
        _detection_timings = {}
        _detection_start_time = 0.0
        _detection_amplitudes.clear()
        _iso_hint = ""
    with state.lock:
        state.detecting = False
        state.syncing = False
        state.current_effect = None
        state.calibrated_positions.clear()
        state.last_detections = []
        state.last_detection_count = 0
    game.set_game_btn_highlight(False)
    game.clear_winner_highlight()
    set_status("Reset")


def heart_reset():
    post_json_async("/admin/heart/reset", {})
    set_status("Likes reset")


def heart_toggle():
    post_json_async("/admin/heart/toggle", {})
    set_status("Likes toggled")


trigger_effect = effects.trigger_effect


def _is_elgato_label(label: str) -> bool:
    s = (label or "").lower()
    return "facecam" in s or "elgato" in s


def _scan_and_pick_elgato(holder, retrying=False):
    """Refresh the camera list and open the Elgato if it's available.
    Returns True if an Elgato is now open, False otherwise."""
    names  = _avfoundation_device_names()
    cams   = find_cameras(8)
    labels = [names.get(i, f"Camera {i}") for i in cams] or ["No cameras found"]
    lmap   = {names.get(i, f"Camera {i}"): i for i in cams}

    with state.lock:
        state.cameras               = cams
        state.camera_listbox_items  = labels
        state.camera_label_to_index = lmap
        current_idx                 = state.selected_camera_idx

    # If the currently-open camera is already the Elgato, leave it alone.
    current_label = next(
        (l for l, i in lmap.items() if i == current_idx),
        "",
    )
    if holder and holder.get("cap") and _is_elgato_label(current_label):
        return True

    # Find an Elgato in the new list and open it.
    elgato_label = next((l for l in labels if _is_elgato_label(l)), None)
    if elgato_label is None:
        if not retrying:
            log.info(f"[camera] no Elgato found in {labels} — waiting for one to appear")
            set_status("Plug in the Elgato Facecam — waiting…")
        return False

    idx = lmap.get(elgato_label)
    if idx is None or holder is None:
        return False
    cap = open_camera(idx)
    if not cap:
        log.info(f"[camera] failed to open Elgato at index {idx}")
        return False
    old = holder.get("cap")
    if old:
        old.release()
    holder["cap"] = cap
    with state.lock:
        state.selected_camera_idx = idx
        state.status_text         = elgato_label
    log.info(f"[camera] auto-opened {elgato_label}")
    return True


def camera_scan_worker(holder=None):
    """Initial Elgato scan + background re-scan loop.  Refuses to silently
    fall back to a non-Elgato camera (audience phones need consistent
    exposure, and Camera Hub watchdog only works on the Elgato).  If the
    Elgato isn't present yet, the loop keeps re-scanning every 5 seconds
    so plugging it in later picks it up automatically."""
    if _scan_and_pick_elgato(holder, retrying=False):
        return
    while True:
        try:
            with state.lock:
                if not state.running:
                    return
            time.sleep(5.0)
            if _scan_and_pick_elgato(holder, retrying=True):
                set_status("Elgato connected")
                return
        except Exception as e:
            log.info(f"[camera] rescan error: {e}")
            time.sleep(5.0)



# ------------------------------------------------------------------ #
# Key handler
# ------------------------------------------------------------------ #

def on_key_press(key, holder):
    if key in (dpg.mvKey_Q, dpg.mvKey_Escape):
        with state.lock:
            state.running = False

    elif key == dpg.mvKey_D:
        toggle_detection()

    elif key == dpg.mvKey_S:
        toggle_sync()

    elif key == dpg.mvKey_R:
        reset_server()

    elif key == dpg.mvKey_Tab:
        toggle_sidebar()

    elif key == dpg.mvKey_G:
        toggle_debug()

    elif key == dpg.mvKey_V:
        toggle_recording()

    elif key == dpg.mvKey_H:
        toggle_all_overlays()

    elif key == dpg.mvKey_O:
        toggle_device_overlay()

    elif key == dpg.mvKey_P:
        toggle_overlay_mode()

    elif key == dpg.mvKey_F:
        toggle_flip_projection()




# ------------------------------------------------------------------ #
# UI setup
# ------------------------------------------------------------------ #

_PAD        = 8     # left/right padding for sidebar content
_CHK_INDENT = 284   # checkbox x position
_KEY_INDENT = 252   # hotkey label x position (flush left of checkbox)

_CHK_LABEL_ON  = (240, 175, 60)    # active-orange — same family as fx_active_theme
_CHK_LABEL_OFF = (210, 210, 210)


def _chk(label: str, tag: str, callback, enabled: bool = True):
    """Checkbox row: label left, hotkey right-aligned before checkbox.
    The label gets a tag (lbl_{tag}) so update_ui_from_state can switch
    its colour to the active-orange theme when the checkbox is on, giving
    every checkbox the same 'selected' highlight as the Ripple button."""
    import re as _re
    m = _re.search(r'\s*(\[[^\]]+\])\s*$', label)
    base   = label[:m.start()] if m else label
    hotkey = m.group(1)        if m else ""
    with dpg.group(horizontal=True):
        dpg.add_text(base, indent=_PAD, tag=f"lbl_{tag}",
                     color=_CHK_LABEL_OFF)
        if hotkey:
            dpg.add_text(hotkey, indent=_KEY_INDENT, color=(120, 120, 120))
        dpg.add_checkbox(label=f"##{tag}", tag=tag,
                         callback=callback, indent=_CHK_INDENT,
                         enabled=enabled)


def _safe_set_chk(tag: str, value: bool):
    """Set a checkbox value AND push the matching label-colour update so
    the row visibly highlights when on."""
    safe_set(tag, value)
    ui_queue.put((f"_lbl_color_{tag}", bool(value)))


def setup_ui(holder: dict):
    effects.init(state, set_status, ui_queue=ui_queue)
    game.init(state, set_status, post_json, fetch_json, _render_order)
    dpg.create_context()

    # Theme for the currently active effect button
    with dpg.theme(tag="fx_active_theme"):
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button,        (180, 120, 20, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (210, 150, 40, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,  (220, 160, 50, 255))

    # Theme for the game button while a game is active
    with dpg.theme(tag="game_active_theme"):
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button,        (20, 140, 60, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (30, 170, 75, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,  (40, 190, 85, 255))

    # Zero padding on the preview panel so get_item_rect_size == usable pixel area
    with dpg.theme(tag="preview_panel_theme"):
        with dpg.theme_component(dpg.mvWindowAppItem):
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 0)

    with dpg.texture_registry(show=False):
        blank = np.zeros(PREVIEW_HEIGHT * PREVIEW_WIDTH * 4, dtype=np.float32)
        dpg.add_dynamic_texture(PREVIEW_WIDTH, PREVIEW_HEIGHT, blank,
                                tag="camera_texture")
        effects.register_preview_texture()

    with dpg.handler_registry():
        dpg.add_key_press_handler(
            callback=lambda s, a: on_key_press(a, holder)
        )
        dpg.add_mouse_click_handler(
            button=dpg.mvMouseButton_Left,
            callback=_on_preview_click,
        )

    with dpg.window(tag="main_window", label=WINDOW_TITLE,
                    no_resize=True, no_move=True, no_collapse=True,
                    width=-1, height=-1):

        with dpg.group(horizontal=True, horizontal_spacing=0):

            # ---- Sidebar ----
            with dpg.child_window(width=_SIDEBAR_WIDTH, height=-1, border=True,
                                  tag="sidebar_panel"):

                dpg.add_text("pixelmesh", color=(255, 200, 50), indent=_PAD)
                dpg.add_separator()
                dpg.add_spacer(height=2)

                with dpg.tab_bar():

                    # ---- SCENE tab (default) ----
                    with dpg.tab(label="SCENE"):
                        dpg.add_spacer(height=4)
                        dpg.add_text("CAMERA HUB", color=(160, 160, 160), indent=_PAD)
                        dpg.add_separator()
                        with dpg.group(horizontal=True):
                            dpg.add_text("Status", indent=_PAD)
                            dpg.add_text("[OFF]", tag="elgato_status",
                                         color=(120, 120, 120))
                        _chk("Auto Exposure", "chk_ae", _toggle_ae, enabled=False)
                        dpg.add_text("ISO Gain", color=(180, 180, 180), indent=_PAD)
                        dpg.add_slider_int(label="##iso", tag="sld_iso",
                                           default_value=elgato._DEFAULT_GAIN,
                                           min_value=0, max_value=160,
                                           callback=_set_iso,
                                           indent=_PAD, width=-(_PAD + 1),
                                           enabled=False)
                        dpg.add_text("", tag="iso_hint_text",
                                     color=(220, 180, 80), indent=_PAD,
                                     wrap=300)

                        dpg.add_spacer(height=8)
                        dpg.add_text("PROJECTION", color=(160, 160, 160), indent=_PAD)
                        dpg.add_separator()
                        _chk("Flip Projection  [F]", "chk_flip_projection",
                             toggle_flip_projection)

                        dpg.add_spacer(height=8)
                        dpg.add_text("FRAME ROI", color=(160, 160, 160), indent=_PAD)
                        dpg.add_separator()
                        with dpg.table(header_row=False, indent=_PAD,
                                       width=-(_PAD + 1), pad_outerX=True):
                            dpg.add_table_column()
                            dpg.add_table_column()
                            with dpg.table_row():
                                dpg.add_text("Top %",    color=(180, 180, 180))
                                dpg.add_text("Bottom %", color=(180, 180, 180))
                            with dpg.table_row():
                                dpg.add_slider_int(label="##roi_top",    tag="sld_roi_top",
                                                   default_value=0, min_value=0, max_value=60,
                                                   callback=_set_roi, width=-1)
                                dpg.add_slider_int(label="##roi_bottom", tag="sld_roi_bottom",
                                                   default_value=0, min_value=0, max_value=60,
                                                   callback=_set_roi, width=-1)
                            with dpg.table_row():
                                dpg.add_text("Left %",  color=(180, 180, 180))
                                dpg.add_text("Right %", color=(180, 180, 180))
                            with dpg.table_row():
                                dpg.add_slider_int(label="##roi_left",  tag="sld_roi_left",
                                                   default_value=0, min_value=0, max_value=60,
                                                   callback=_set_roi, width=-1)
                                dpg.add_slider_int(label="##roi_right", tag="sld_roi_right",
                                                   default_value=0, min_value=0, max_value=60,
                                                   callback=_set_roi, width=-1)

                        dpg.add_spacer(height=8)
                        dpg.add_text("CAPTURE", color=(160, 160, 160), indent=_PAD)
                        dpg.add_separator()
                        _chk("Record Video  [V]", "chk_recording", lambda: toggle_recording())
                        dpg.add_text("[REC]", tag="rec_status_text",
                                     color=(220, 60, 60), indent=_PAD, show=False)
                        dpg.add_text("", tag="rec_filename_text",
                                     color=(150, 150, 150), indent=_PAD, show=False,
                                     wrap=300)

                        dpg.add_spacer(height=8)
                        dpg.add_text("INFORMATION", color=(160, 160, 160), indent=_PAD)
                        dpg.add_separator()
                        dpg.add_text("", tag="status_text",  indent=_PAD)
                        dpg.add_text("", tag="clients_text", indent=_PAD)
                        dpg.add_text("", tag="detect_text",  indent=_PAD)

                        dpg.add_spacer(height=8)
                        dpg.add_text("SYNC STATS", color=(160, 160, 160), indent=_PAD)
                        dpg.add_separator()
                        dpg.add_text("", tag="sync_status_line",
                                     color=(160, 160, 160), indent=_PAD)
                        dpg.add_text("  #     RTT     Off    Smp",
                                     color=(180, 180, 180), indent=_PAD)
                        with dpg.child_window(tag="sync_stats_panel",
                                              height=200, width=-(_PAD + 1),
                                              indent=_PAD, border=False):
                            dpg.add_text("No sync data - enable Clock Sync.",
                                         tag="sync_no_data", color=(120, 120, 120))
                            for i in range(32):
                                dpg.add_text("", tag=f"sync_row_{i}", show=False)

                    # ---- RUN tab ----
                    with dpg.tab(label="RUN"):
                        dpg.add_spacer(height=4)
                        dpg.add_text("DETECTION", color=(160, 160, 160), indent=_PAD)
                        dpg.add_separator()
                        _chk("Detection  [D]",     "chk_detection",    lambda: toggle_detection())
                        _chk("Clock Sync  [S]",    "chk_sync",         lambda: toggle_sync())
                        _chk("Overlays  [H]",      "chk_overlays_all", lambda: toggle_all_overlays())
                        _chk("ID Overlays  [O]",   "chk_overlays",     lambda: toggle_device_overlay())
                        _chk("Render Order  [P]",  "chk_overlay_pos",  lambda: toggle_overlay_mode())
                        _chk("Debug Capture  [G]", "chk_debug",        lambda: toggle_debug())
                        dpg.add_spacer(height=4)
                        dpg.add_button(label="Reset Server  [R]", callback=reset_server,
                                       indent=_PAD, width=-(_PAD + 1))

                        dpg.add_spacer(height=8)
                        effects.build_preview_widget(indent=_PAD)
                        dpg.add_spacer(height=4)
                        for _ename, _elabel in effects.EFFECT_LABELS.items():
                            if _ename == "ripple":
                                _btn_cb = lambda s, a, u: toggle_ripple_arm()
                            elif _ename == "spotlight":
                                _btn_cb = lambda s, a, u: toggle_spotlight_arm()
                            else:
                                _btn_cb = lambda s, a, u: fire_effect_and_disarm_ripple(u)
                            with dpg.group(horizontal=True, indent=_PAD):
                                dpg.add_button(
                                    label=_elabel,
                                    tag=f"fx_btn_{_ename}",
                                    callback=_btn_cb,
                                    user_data=_ename,
                                    width=262,
                                )
                                dpg.add_button(
                                    label="...",
                                    callback=lambda s, a, u: effects._open_modal(u),
                                    user_data=_ename,
                                    width=30,
                                )

                    # ---- GAME tab ----
                    with dpg.tab(label="GAME"):
                        dpg.add_spacer(height=4)
                        game.build_sidebar_buttons(indent=_PAD, pad=_PAD)

                        dpg.add_spacer(height=8)
                        dpg.add_text("HEARTS", color=(160, 160, 160), indent=_PAD)
                        dpg.add_separator()
                        dpg.add_button(label="Reset Like Counter",
                                       callback=heart_reset,
                                       indent=_PAD, width=-(_PAD + 1))
                        dpg.add_button(label="Enable / Disable Likes",
                                       callback=heart_toggle,
                                       indent=_PAD, width=-(_PAD + 1))

            # ---- Preview panel ----
            with dpg.child_window(tag="preview_panel", border=False,
                                  width=-1, height=-1,
                                  no_scrollbar=True, no_scroll_with_mouse=True):
                dpg.add_image("camera_texture", tag="preview_image",
                              width=1, height=1)
        dpg.bind_item_theme("preview_panel", "preview_panel_theme")

    # ---- Per-effect settings modals (hidden until ... is clicked) ----
    effects.build_window()

    # ---- Bug game leaderboard window ----
    game.build_window()

    # Open maximised — read screen size via AppKit (macOS), fall back to 1660×780.
    try:
        from AppKit import NSScreen
        r = NSScreen.mainScreen().frame()
        _sw, _sh = int(r.size.width), int(r.size.height)
    except Exception:
        _sw, _sh = 1660, 780
    dpg.create_viewport(title=WINDOW_TITLE, width=_sw, height=_sh, x_pos=0, y_pos=0)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main_window", True)
    with dpg.theme() as _main_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 0)
    dpg.bind_item_theme("main_window", _main_theme)


def on_camera_selected(label: str, holder: dict):
    with state.lock:
        idx = state.camera_label_to_index.get(label)
    if idx is None:
        return
    cap = open_camera(idx)
    if cap is None:
        set_status(f"Could not open {label}")
        return
    old = holder.get("cap")
    if old:
        old.release()
    holder["cap"] = cap
    with state.lock:
        state.selected_camera_idx = idx
        state.status_text = label


# ------------------------------------------------------------------ #
# Main loop
# ------------------------------------------------------------------ #

def main():
    global _camera_fps
    holder = {"cap": None}

    setup_ui(holder)

    threading.Thread(target=lambda: poll_clients(), daemon=True).start()
    threading.Thread(target=poll_sync_stats, daemon=True).start()
    threading.Thread(target=camera_scan_worker, args=(holder,), daemon=True).start()
    threading.Thread(target=_detection_worker, daemon=True).start()
    threading.Thread(target=_exposure_monitor_worker, daemon=True).start()
    effects.start_preview_thread()
    elgato.on_state_change = _elgato_state_changed
    elgato.start()
    def _midi_set_recording(on: bool):
        if on and not vid_rec.active:
            if not _no_camera():
                path = vid_rec.start()
                set_status(f"Recording: {_os.path.basename(path)}")
        elif not on and vid_rec.active:
            path = vid_rec.stop()
            set_status(f"Recording saved: {_os.path.basename(path)}")

    def _midi_set_overlays(on: bool):
        with state.lock:
            state.show_device_overlay = on

    def _midi_set_sync(on: bool):
        with state.lock:
            state.syncing = on
        post_json_async("/admin/sync", {"sync": on})

    midi.midi.start(
        trigger_effect = None,                       # wired up later
        toggle_detect  = toggle_detection,
        set_iso        = lambda v: elgato.set_iso(v),
        set_recording  = _midi_set_recording,
        set_overlays   = _midi_set_overlays,
        set_sync       = _midi_set_sync,
        reset          = reset_server,
    )

    texture_data = frame_to_texture(no_camera_canvas())
    _dbg_counter = 0   # local to main — throttles debug save_frame calls

    try:
        while dpg.is_dearpygui_running():
            with state.lock:
                if not state.running:
                    break

            frame_start = time.time()

            cap = holder.get("cap")
            with state.lock:
                was_active      = state.camera_active
                state.camera_active = cap is not None
                was_detecting   = state.detecting

            # Camera just disappeared — stop detection cleanly
            if was_active and cap is None and was_detecting:
                global _detected_ids, _detection_start_time
                _log_detection_summary()
                _save_report()
                _stop_auto_debug_capture()
                _detected_ids = set()
                _detection_start_time = 0.0
                detector.reset()
                with state.lock:
                    state.detecting = False
                    state.calibrated_positions.clear()
                    state.last_detections = []
                    state.last_detection_count = 0
                post_json_async("/admin/detect", {"detecting": False})
                _apply_audience_iso()
                set_status("Camera lost - detection stopped")

            if cap is None:
                canvas = no_camera_canvas()
                texture_data = frame_to_texture(canvas)

            else:
                ok, raw = cap.read()
                if ok:
                    # One copy shared by state.latest_frame and the detection queue —
                    # avoids a second 6 MB allocation when both need the same frame.
                    raw_copy = raw.copy()
                    with state.lock:
                        state.latest_frame = raw_copy
                        detecting = state.detecting
                        show_ov   = state.show_device_overlay

                    # Hand frame to detection thread (non-blocking).
                    # If it's busy the frame is dropped — display continues unblocked.
                    if detecting:
                        try:
                            _detect_queue.put_nowait((raw_copy, time.time()))
                        except Full:
                            pass

                    frame  = apply_gamma(raw)
                    frame  = apply_contrast(frame)
                    canvas = build_canvas(frame)

                    with state.lock:
                        _scale  = state.last_render_scale
                        _crop_x = state.last_crop_x
                        _crop_y = getattr(state, "last_crop_y", 0)

                    if detecting and _overlays_on():
                        # draw_overlay reads detector's cached state (_last_stds,
                        # _decoded_pts) written by the detection thread.  NumPy
                        # reference swaps are atomic under CPython's GIL so no
                        # explicit lock is needed — at worst we see one frame stale.
                        # Race after detector.reset() (e.g. ROI change + detect-on)
                        # can leave stale integer indices pointing past the rebuilt
                        # _points array; swallow that one-frame IndexError instead
                        # of crashing the controller. Recovers on the next frame.
                        try:
                            detector.draw_overlay(canvas, scale=_scale,
                                                  crop_x=_crop_x, crop_y=_crop_y,
                                                  show_ids=not show_ov,
                                                  valid_ids=_valid_blink_ids or None)
                        except IndexError as e:
                            log.info(f"[overlay] skipped one frame after detector reset: {e}")

                        if dbg_cap.active:
                            _dbg_counter += 1
                            if _dbg_counter % DEBUG_SAVE_EVERY == 0:
                                di = _last_dbg_imgs
                                if di is not None and di.gray is not None:
                                    with state.lock:
                                        results_snap = list(state.last_detections)
                                    # Only pass active/decoded points — passing all 25,920
                                    # causes save_frame to iterate 600K+ Python objects
                                    # per call, stalling the main thread for 50-100ms.
                                    gate = detector.cfg["min_recent_std"]
                                    active_blobs = [
                                        pt for pt in detector.get_blobs()
                                        if pt.decoded_id is not None
                                        or pt.recent_std >= gate * 0.5
                                    ]
                                    dbg_cap.save_frame(
                                        raw=raw,
                                        gray=di.gray,
                                        thresh=di.contrast if di.contrast is not None
                                               else np.zeros_like(di.gray),
                                        overlay=canvas.copy(),
                                        blobs=active_blobs,
                                        detections=results_snap,
                                    )

                    draw_roi_overlay(canvas)

                    if show_ov:
                        draw_device_overlay(canvas)

                    try:
                        draw_winner_highlight(canvas)
                    except Exception as e:
                        log.info(f"[winner] draw skipped: {e}")

                    draw_click_ripples(canvas)

                    if detecting:
                        draw_detect_border(canvas)

                    if vid_rec.active:
                        vid_rec.record(canvas)

                    if dbg_cap.active:
                        dbg_cap.record_frame(canvas)

                    # Scene → Flip Projection: mirror the whole canvas
                    # BEFORE drawing the HUD so HUD text stays readable
                    # (mirrored detection overlays and ripples are
                    # correct — those need to land on the visible phone
                    # positions in the flipped projection).  The raw
                    # camera frame queued for detection is untouched, and
                    # _on_preview_click inverts x to recover the real
                    # room position when computing u.
                    display_canvas = (
                        cv2.flip(canvas, 1) if state.flip_projection else canvas
                    )

                    fps = 1.0 / max(time.time() - frame_start, 1e-4)
                    _camera_fps = 0.9 * _camera_fps + 0.1 * fps
                    draw_hud(display_canvas, fps)
                    _draw_spotlight_cursor(display_canvas)

                    # MJPEG stream — write JPEG atomically so server.py
                    # never reads a partial file.  Capped at 30 fps.
                    global _last_stream_ts
                    _now = time.time()
                    if _now - _last_stream_ts >= _STREAM_INTERVAL:
                        _last_stream_ts = _now
                        _tmp = _STREAM_PATH + ".new.jpg"
                        cv2.imwrite(_tmp, display_canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])
                        _os.replace(_tmp, _STREAM_PATH)

                    texture_data = frame_to_texture(display_canvas)

            # Update texture
            dpg.set_value("camera_texture", texture_data)

            # Fit preview image to available space.
            # main_window always fills the full window — subtract sidebar to get
            # the true available width without relying on viewport client dims.
            try:
                pw, ph = dpg.get_item_rect_size("preview_panel")
                ph_img = max(1, ph)
                if pw > 1 and ph_img > 1:
                    aspect = PREVIEW_WIDTH / PREVIEW_HEIGHT
                    if pw / ph_img > aspect:
                        iw, ih = int(ph_img * aspect), ph_img
                    else:
                        iw, ih = pw, int(pw / aspect)
                    dpg.configure_item("preview_image", width=iw, height=ih)
            except Exception:
                pass

            update_ui_from_state()

            # Drain UI queue — set _ui_syncing so checkbox set_value calls
            # don't re-fire toggle callbacks in DearPyGui versions that
            # invoke callbacks on set_value.
            global _ui_syncing
            _ui_syncing = True
            try:
                while not ui_queue.empty():
                    tag, value = ui_queue.get()
                    if tag == "_active_effect":
                        for n in effects.EFFECT_LABELS:
                            btn = f"fx_btn_{n}"
                            if dpg.does_item_exist(btn):
                                if n == value:
                                    dpg.bind_item_theme(btn, "fx_active_theme")
                                else:
                                    dpg.bind_item_theme(btn, None)
                        continue
                    if tag == "_refire_effect":
                        # Debounced re-fire from effects._on_settings_changed
                        # — must run on main thread because trigger_effect
                        # reads slider values via dpg.get_value, which isn't
                        # thread-safe and previously deadlocked DPG under
                        # rapid colour-picker drags.
                        with state.lock:
                            eff = state.current_effect
                        if eff:
                            try:
                                effects.trigger_effect(eff)
                            except Exception as e:
                                log.warning(f"[effect] refire failed: {e}")
                        continue
                    if tag.startswith("_lbl_color_"):
                        chk_tag = tag[len("_lbl_color_"):]
                        lbl_tag = f"lbl_{chk_tag}"
                        if dpg.does_item_exist(lbl_tag):
                            dpg.configure_item(
                                lbl_tag,
                                color=_CHK_LABEL_ON if value else _CHK_LABEL_OFF,
                            )
                        continue
                    if tag == "_rec_status_show":
                        dpg.configure_item("rec_status_text", show=value)
                        continue
                    if tag == "_rec_filename_show":
                        dpg.configure_item("rec_filename_text", show=value)
                        continue
                    if tag == "_roi_enabled":
                        for item in ("sld_roi_top", "sld_roi_bottom",
                                     "sld_roi_left", "sld_roi_right"):
                            dpg.enable_item(item) if value else dpg.disable_item(item)
                        continue
                    if tag == "elgato_color":
                        col = (80, 200, 80) if value else (120, 120, 120)
                        dpg.configure_item("elgato_status", color=col)
                        continue
                    if tag == "elgato_indent":
                        dpg.configure_item("elgato_status", indent=value)
                        continue
                    if tag == "_elgato_enabled":
                        for item in ("chk_ae", "sld_iso"):
                            if value:
                                dpg.enable_item(item)
                            else:
                                dpg.disable_item(item)
                        continue
                    if tag == "_sync_stats_rows":
                        rows = value
                        ts = time.strftime("%H:%M:%S")
                        dpg.set_value("sync_status_line",
                                      f"{len(rows)} dev  ({ts})")
                        dpg.configure_item("sync_no_data", show=(len(rows) == 0))
                        for i in range(32):
                            if i < len(rows):
                                r = rows[i]
                                rtt  = f"{r['rtt_ms']:.0f}"    if r["rtt_ms"]    is not None else "-"
                                off  = f"{r['offset_ms']:+.0f}" if r["offset_ms"] is not None else "-"
                                bid_str = str(r['blink_id'])
                                line = (f"{bid_str:>3}  "
                                        f"{rtt:>5}  "
                                        f"{off:>5}  "
                                        f"{r['samples']:>4}")
                                dpg.set_value(f"sync_row_{i}", line)
                                dpg.configure_item(f"sync_row_{i}", show=True)
                            else:
                                dpg.set_value(f"sync_row_{i}", "")
                                dpg.configure_item(f"sync_row_{i}", show=False)
                        continue
                    try:
                        dpg.set_value(tag, value)
                    except Exception as e:
                        log.info(f"[ui] queue error tag={tag} err={e}")
            finally:
                _ui_syncing = False

            dpg.render_dearpygui_frame()

    finally:
        post_json("/admin/reset", {})
        cap = holder.get("cap")
        if cap:
            cap.release()
        # Overwrite the MJPEG cache with a blank frame so /internal/feed/v1
        # doesn't keep serving the last camera image after shutdown.
        try:
            blank = np.zeros((PREVIEW_HEIGHT, PREVIEW_WIDTH, 3), dtype=np.uint8)
            _tmp = _STREAM_PATH + ".new.jpg"
            cv2.imwrite(_tmp, blank, [cv2.IMWRITE_JPEG_QUALITY, 60])
            _os.replace(_tmp, _STREAM_PATH)
        except Exception as e:
            log.info(f"[shutdown] could not blank stream frame: {e}")
        dpg.destroy_context()


def _detection_worker():
    """
    Background thread: pulls frames from _detect_queue, runs the blink detector,
    and updates shared state.  The main thread never blocks on process_frame.

    NumPy releases the GIL during heavy operations (gather, partition, std) so
    this thread runs genuinely in parallel with the display thread on multi-core
    hardware — no multiprocessing overhead needed.
    """
    global _last_dbg_imgs, _detect_fps
    _det_last_ts = 0.0
    while True:
        with state.lock:
            if not state.running:
                break
        try:
            raw, ts = _detect_queue.get(timeout=0.05)
        except Empty:
            continue

        try:
            t_frame_start = time.time()
            results, dbg_imgs = detector.process_frame(raw, ts, need_debug=dbg_cap.active)
            elapsed = time.time() - t_frame_start
            _detect_fps = 0.9 * _detect_fps + 0.1 * (1.0 / max(elapsed, 1e-4))
            _last_dbg_imgs = dbg_imgs   # atomic reference swap — main thread reads safely

            with state.lock:
                state.last_detections      = results
                state.last_detection_count = len(results)

            if results:
                h_raw, w_raw = raw.shape[:2]
                positions    = {}
                for det in results:
                    # Dismiss decodes for IDs with no connected client.
                    if det.blink_id not in _valid_blink_ids:
                        # Phone may have connected since the last poll — refresh immediately
                        # before discarding the decode result.
                        _refresh_valid_blink_ids()
                    if det.blink_id not in _valid_blink_ids:
                        log.warning(f"[detect] rejected blink_id={det.blink_id} conf={det.confidence:.2f} valid={sorted(_valid_blink_ids)}")
                        detector.clear_id(det.blink_id)
                        with state.lock:
                            state.calibrated_positions.pop(det.blink_id, None)
                        continue
                    # Freeze position at first detection — centroid drifts as
                    # noise points accumulate the same decoded ID over time.
                    with _det_lock:
                        if det.blink_id in _detected_ids:
                            continue
                        u = det.cx_px / w_raw
                        v = det.cy_px / h_raw
                        positions[str(det.blink_id)] = {
                            "u": round(u, 4),
                            "v": round(v, 4),
                            "confidence": round(det.confidence, 3),
                        }
                        with state.lock:
                            state.calibrated_positions[det.blink_id] = {"u": u, "v": v}
                        _detected_ids.add(det.blink_id)
                        # Recompute render order (left-to-right by u) after each new detection.
                        with state.lock:
                            all_pos = dict(state.calibrated_positions)
                        _render_order.clear()
                        for rank, bid in enumerate(
                            sorted(all_pos, key=lambda b: all_pos[b]["u"]), 1
                        ):
                            _render_order[bid] = rank
                        elapsed = time.time() - _detection_start_time
                        _detection_timings[det.blink_id] = (elapsed, det.confidence)
                        # Capture this phone's blink amplitude from the matching
                        # grid point's history.  Used at detection end to suggest
                        # an ISO change if the median across phones is too low.
                        try:
                            for pt in detector.get_blobs():
                                if pt.decoded_id == det.blink_id and pt.history:
                                    vals = [b for _, b in pt.history]
                                    _detection_amplitudes.append(max(vals) - min(vals))
                                    break
                        except Exception:
                            pass
                    _log_timing(
                        f"{det.blink_id:>10}  {elapsed:>14.2f}s  "
                        f"{det.confidence:>12.3f}"
                    )

                # Post positions synchronously BEFORE signalling detection stop.
                # Previously: toggle_detection() fired inside the loop (spawning an
                # async /admin/detect thread), then positions were posted async after
                # the loop.  The detect-stop request would often arrive at the server
                # before the positions request, so the server sent detection_ended to
                # the last-detected phone (not in positions yet) instead of
                # update_position.  With the 0.3 s async timeout that position POST
                # could also silently fail, leaving the phone blinking indefinitely
                # while the controller showed it as located (local state was already set).
                if positions:
                    post_json("/admin/positions", {"positions": positions}, timeout=1.0)

                if _valid_blink_ids and _detected_ids >= _valid_blink_ids:
                    log.info("[detect] all clients found — auto-stopping detection")
                    # Don't go through toggle_detection here: its _ui_syncing
                    # guard exists to stop UI events from re-triggering the
                    # checkbox callback, but it also silently swallows this
                    # programmatic stop if it happens to land mid-UI-drain.
                    with state.lock:
                        was_on = state.detecting
                        state.detecting = False
                    if was_on:
                        _log_detection_summary()
                        _save_report()
                        _stop_auto_debug_capture()
                        post_json_async("/admin/detect", {"detecting": False})
                        _auto_enable_sync()
                        _apply_audience_iso()
                        set_status("Detection OFF")
        finally:
            _detect_queue.task_done()


def _refresh_valid_blink_ids():
    """Fetch the current blink map and update _valid_blink_ids immediately."""
    global _valid_blink_ids
    data = fetch_json("/admin/blink_map")
    if data is None:
        return
    bmap = data.get("map", {})
    new_ids = {int(bid) for bid in bmap}
    if 511 in new_ids:
        log.warning(f"[poll] blink_id 511 is assigned to a connected client: {bmap}")
    if new_ids != _valid_blink_ids:
        _valid_blink_ids = new_ids
        with state.lock:
            stale = [bid for bid in state.calibrated_positions if bid not in new_ids]
            for bid in stale:
                del state.calibrated_positions[bid]


def poll_clients():
    while state.running:
        fetch_client_count(state)
        _refresh_valid_blink_ids()
        time.sleep(1.0)


def poll_sync_stats():
    """Background thread: fetch /admin/sync_stats every 2s and refresh the debug table."""
    while state.running:
        time.sleep(2.0)
        data = fetch_json("/admin/sync_stats")
        if data is None:
            continue
        rows = data.get("stats", [])
        # Rebuild table rows in DearPyGui (must run on main thread via ui_queue)
        ui_queue.put(("_sync_stats_rows", rows))


if __name__ == "__main__":
    main()
