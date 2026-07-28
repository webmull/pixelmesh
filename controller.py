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
from network import post_json, post_json_async, post_bytes, fetch_client_count, fetch_json

# Detection decode is Python-heavy in 50ms budgeted bursts; the default
# 5ms GIL switch interval lets it convoy the display thread. Finer
# switching shortens each stall at negligible throughput cost.
sys.setswitchinterval(0.002)
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

# MJPEG stream: display loop swaps in the latest finished canvas (fresh
# object per frame, so no tearing); a worker thread encodes and POSTs it
# to the server at up to 60fps, keeping JPEG cost off the display thread.
_stream_latest    = None
_STREAM_FPS       = 60

# Temporary perf probe: per-5s display-loop breakdown + DPG item count
# (leak detector for the run-over-run sluggish render investigation).
_perf = {"n": 0, "t0": 0.0, "frame": 0.0}
def _perf_tick(frame_start):
    import time as _t
    now = _t.time()
    _perf["frame"] += now - frame_start
    _perf["n"] += 1
    if now - _perf["t0"] >= 5.0:
        if _perf["t0"] > 0 and _perf["n"]:
            n = _perf["n"]
            avg_ms = _perf["frame"] / n * 1000
            # Sentinel, not chatter: only speak when pacing degrades.
            # (24-40ms is healthy; sustained 45+ is how the save_frame
            # display-thread stall of Jul 2026 would have been caught.)
            if avg_ms > 45:
                log.warning(f"[perf] display avg={avg_ms:.0f}ms over {n} frames - "
                            f"dpg_items={len(dpg.get_all_items())} "
                            f"threads={threading.active_count()}")
        _perf["t0"] = now
        _perf["n"] = 0
        _perf["frame"] = 0.0
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
_detect_fps:   float = 0.0   # EMA fps of detection thread, read by _update_hud
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
_last_midi_hist_version: int = -1    # sidebar MIDI panel refresh guard
_last_midi_conn = None               # pedal link indicator guard

# Two-phase ISO: detection wants the lowest sensible gain so dark phases
# read near-zero (sensor noise floor matters more than nominal range —
# noisy dark phases compress amplitude and the variance gate then drops
# dim or distant phones).  The post-detection / showtime phase wants
# more gain so the camera feed reads as a bright lit crowd, not a dim
# wash.  Both auto-applied around the detection toggle; operator can
# still slide either direction via the ISO control afterwards.
_DETECTION_ISO_GAIN: int = 35
_AUDIENCE_ISO_GAIN:  int = 100

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
        _iso_hint = f"low signal {median_amp:.2f} - try ISO ~{suggested}"
    elif median_amp > 0.95 and current > 30:
        delta = max(10, int(current * 0.20))
        suggested = max(0, current - delta)
        _iso_hint = f"strong signal {median_amp:.2f} - could try ISO ~{suggested}"
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
# Auto-start debug capture with detection; it then runs for the whole
# session until toggled off manually (G / checkbox) or the app exits.
_DEBUG_AUTO_ON_DETECT = True


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
    return canvas


# ------------------------------------------------------------------ #
# Detection overlay helpers
# ------------------------------------------------------------------ #

def draw_device_overlay(canvas: np.ndarray, flipped: bool = False):
    """Draw the per-phone ID badge.  When `flipped`, the canvas has
    already been mirrored, so we invert x to land each badge over the
    correct phone and the text reads upright (cv2.putText paints
    horizontally onto the post-flip pixels)."""
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
        if flipped:
            px = PREVIEW_WIDTH - 1 - px
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


def draw_roi_overlay(canvas: np.ndarray, flipped: bool = False):
    """Dim the excluded ROI regions and draw boundary lines.  When
    `flipped`, swap left↔right inputs so the dimmed band reflects the
    real-world ROI on the mirrored canvas, and the corner label lands
    inside the displayed inner ROI rather than off-screen."""
    global _roi_label_info
    _roi_label_info = None
    if not _overlays_on():
        return
    roi_top    = detector.cfg.get("roi_top_frac",    0.0)
    roi_bottom = detector.cfg.get("roi_bottom_frac", 0.0)
    roi_left   = detector.cfg.get("roi_left_frac",   0.0)
    roi_right  = detector.cfg.get("roi_right_frac",  0.0)
    if flipped:
        roi_left, roi_right = roi_right, roi_left
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

    # Text itself is drawn crisp by _update_hud on the viewport drawlist
    # (cv2 text on the 720p canvas upscales fuzzy); export the anchor in
    # canvas coords plus the strings.  Same render iteration, same thread.
    _roi_label_info = (x1, y1, label1, label2)


_WINNER_HIGHLIGHT_SECS = 6.0


def draw_winner_highlight(canvas: np.ndarray, flipped: bool = False):
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
    if flipped:
        px = PREVIEW_WIDTH - 1 - px
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


# HUD pills (fps + detected/connected counter) and the ROI label render
# on a front viewport drawlist instead of the 720p cv2 canvas: canvas text
# upscales fuzzy, drawlist text rasterises from the 32px Verdana atlas at
# half size (retina-crisp, same trick as the widget font).  A drawlist
# also captures no mouse input, so ripple clicks pass straight through.
_HUD_M    = 12     # margin from the viewport's bottom-right corner
_HUD_PAD  = 8      # pill inner padding
_HUD_TXT  = 16     # drawn text size (half the 32px atlas)
_hud_font = None   # set by setup_ui; get_text_size needs the font handle
_roi_label_info = None   # (canvas_x, canvas_y, line1, line2) or None
_hud_last = None


def _update_hud(fps: float):
    """Reposition/retext the crisp HUD, bottom-right of the viewport.
    Called every frame from the render loop; no-ops until values move."""
    global _hud_last
    if _hud_font is None or not dpg.does_item_exist("hud_fps_text"):
        return
    with state.lock:
        detecting  = state.detecting
        sidebar_on = state.sidebar_visible
    det_str   = f" / {int(_detect_fps + 0.5)}" if detecting else ""
    fps_label = f"{int(fps + 0.5)}{det_str} fps"
    n_det, n_conn = len(_detected_ids), len(_valid_blink_ids)
    found_label = f"{n_det} / {n_conn} found"
    vw = dpg.get_viewport_client_width()
    vh = dpg.get_viewport_client_height()
    roi_info = _roi_label_info
    key = (fps_label, detecting, found_label, vw, vh, roi_info, sidebar_on)
    if key == _hud_last:
        return
    _hud_last = key

    ph = _HUD_TXT + 2 * _HUD_PAD           # pill height
    y1, y2 = vh - _HUD_M - ph, vh - _HUD_M
    ty     = y1 + _HUD_PAD - 1
    dot_w  = 16                            # dot diameter + gap before text

    fw  = dpg.get_text_size(fps_label, font=_hud_font)[0] * 0.5
    fx2 = vw - _HUD_M
    fx1 = fx2 - (fw + 2 * _HUD_PAD + dot_w)
    dpg.configure_item("hud_pill_bg", pmin=(fx1, y1), pmax=(fx2, y2))
    dpg.configure_item("hud_dot", center=(fx1 + _HUD_PAD + 4, (y1 + y2) // 2),
                       fill=(60, 210, 90, 255) if detecting else (95, 95, 95, 255))
    dpg.configure_item("hud_fps_text", pos=(fx1 + _HUD_PAD + dot_w, ty),
                       text=fps_label)

    # detected / connected counter — pill to the left of the fps pill
    # while detecting.  Green when caught up, amber while still chasing.
    dpg.configure_item("hud_found_bg",   show=detecting)
    dpg.configure_item("hud_found_text", show=detecting)
    if detecting:
        cw  = dpg.get_text_size(found_label, font=_hud_font)[0] * 0.5
        cx2 = fx1 - 10
        cx1 = cx2 - (cw + 2 * _HUD_PAD)
        col = (60, 210, 90, 255) if n_conn and n_det >= n_conn \
              else (255, 170, 40, 255)
        dpg.configure_item("hud_found_bg", pmin=(cx1, y1), pmax=(cx2, y2),
                           color=col)
        dpg.configure_item("hud_found_text", pos=(cx1 + _HUD_PAD, ty),
                           text=found_label)

    # ROI label: canvas-space anchor from draw_roi_overlay mapped into the
    # preview image's on-screen rect.
    show_roi = roi_info is not None and dpg.does_item_exist("preview_image")
    if show_roi:
        cx, cy, l1, l2 = roi_info
        ix, iy = dpg.get_item_rect_min("preview_image")
        iw, ih = dpg.get_item_rect_size("preview_image")
        if iw < 2:
            show_roi = False
        else:
            sx = ix + cx * iw / PREVIEW_WIDTH + 14
            sy = iy + cy * ih / PREVIEW_HEIGHT + 14
            if sidebar_on:               # keep it out from under the sidebar
                sx = max(sx, _SIDEBAR_WIDTH + 12)
            w1 = dpg.get_text_size(l1, font=_hud_font)[0] * 0.5
            w2 = dpg.get_text_size(l2, font=_hud_font)[0] * 0.5
            bw = max(w1, w2) + 2 * _HUD_PAD
            bh = 2 * _HUD_TXT + 4 + 2 * _HUD_PAD
            dpg.configure_item("roi_bg", pmin=(sx, sy), pmax=(sx + bw, sy + bh))
            dpg.configure_item("roi_line1", text=l1,
                               pos=(sx + _HUD_PAD, sy + _HUD_PAD - 1))
            dpg.configure_item("roi_line2", text=l2,
                               pos=(sx + _HUD_PAD, sy + _HUD_PAD + _HUD_TXT + 3))
    for _t in ("roi_bg", "roi_line1", "roi_line2"):
        dpg.configure_item(_t, show=show_roi)


# ------------------------------------------------------------------ #
# UI helpers
# ------------------------------------------------------------------ #

def set_status(text: str):
    with state.lock:
        state.status_text = text


def safe_set(tag: str, value):
    ui_queue.put((tag, value))


_last_ui_snapshot = None

def update_ui_from_state():
    global _last_ui_snapshot
    with state.lock:
        status    = state.status_text
        clients   = state.client_count
        detecting = state.detecting
        effect    = state.current_effect

    rec_active = vid_rec.active
    rec_path   = getattr(vid_rec, "_path", "") if rec_active else ""

    # Every value pushed below changes only on a user action or a background
    # event (detection / elgato) — never per-frame.  Skip the whole push (and its
    # ~28 queue items + label-colour reconfigures) when nothing an operator can
    # see has changed, dropping steady-state UI work to zero.  Event-driven puts
    # (effect re-fire, sync rows, elgato callback) go through ui_queue elsewhere
    # and are unaffected.
    snapshot = (
        status, clients, detecting, effect, len(_detected_ids),
        rec_active, rec_path, _iso_hint,
        state.syncing, state.show_overlays, state.show_device_overlay,
        state.overlay_show_render, dbg_cap.active, state.flip_projection,
        elgato.connected, elgato.ae_on, elgato.iso_gain,
    )
    # MIDI history has its own version guard, outside the main snapshot
    # dedup, so pedal events appear immediately without joining it.
    global _last_midi_conn
    if midi.midi.connected != _last_midi_conn:
        _last_midi_conn = midi.midi.connected
        safe_set("midi_conn_text",
                 "connected" if _last_midi_conn else "waiting for pedal")
        ui_queue.put(("_midi_conn_color", _last_midi_conn))

    global _last_midi_hist_version
    if midi.midi.history_version != _last_midi_hist_version:
        _last_midi_hist_version = midi.midi.history_version
        hist = midi.midi.history()
        safe_set("midi_last_text", hist[0] if hist else "no commands yet")
        safe_set("midi_history_text", "\n".join(hist[1:]))

    if snapshot == _last_ui_snapshot:
        return
    _last_ui_snapshot = snapshot

    # state.current_effect now carries "ripple" while armed (set by
    # _set_ripple_armed), so the highlight follows naturally.
    ui_queue.put(("_active_effect", effect))

    safe_set("rec_status_text", "[REC]" if rec_active else "")
    ui_queue.put(("_rec_status_show", rec_active))
    safe_set("rec_filename_text", _os.path.basename(rec_path) if rec_path else "")
    ui_queue.put(("_rec_filename_show", bool(rec_path)))

    safe_set("iso_hint_text",    _iso_hint)
    ui_queue.put(("_iso_hint_show", bool(_iso_hint)))
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
    """Drop ISO to the detection baseline (sensor noise floor matters
    most for clean blink contrast) — but never RAISE it.  If the
    operator already has the slider below _DETECTION_ISO_GAIN we leave
    it; lower ISO is at least as good for blink amplitude as the
    baseline, and bumping back up would throw away a deliberate venue
    tune."""
    if not elgato.connected:
        return
    if elgato.iso_gain > _DETECTION_ISO_GAIN:
        elgato.set_iso(_DETECTION_ISO_GAIN)


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

    # Zero-client runs are allowed (camera/ROI/overlay testing without
    # phones): the empty valid-set already rejects every decode, and the
    # auto-stop guard requires a non-empty set so it cannot fire early.
    if val:
        global _detection_start_time, _detected_ids, _render_order
        global _report_saved_path, _iso_hint
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
        # for post-show diagnostics. Once started it stays on for the whole
        # session (per-frame JPEGs only accumulate while detecting) until
        # toggled off manually or the app exits.
        if _DEBUG_AUTO_ON_DETECT and not dbg_cap.active:
            try:
                dbg_cap.start_run()
            except Exception as e:
                log.info(f"[debug] auto-start failed: {e}")
        _apply_detection_iso()
        post_json_async("/admin/detect", {"detecting": True})
        _open_timing_log()
        set_status("Detection ON")
    else:
        _log_detection_summary()
        _save_report()
        post_json_async("/admin/detect", {"detecting": False})
        _auto_enable_sync()
        _apply_audience_iso()
        set_status("Detection OFF")


def _stream_worker():
    """Encode and POST the latest display canvas at up to _STREAM_FPS.
    Runs on its own thread so JPEG cost never touches the display loop;
    always takes the newest frame, dropping any it was too slow for."""
    last = None
    while True:
        t0 = time.time()
        # Full 60fps normally; half rate while detecting so the encode
        # thread isn't competing for the interpreter during decode bursts.
        with state.lock:
            detecting = state.detecting
        interval = (2.0 if detecting else 1.0) / _STREAM_FPS
        frame = _stream_latest
        if frame is not None and frame is not last:
            last = frame
            try:
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    post_bytes("/admin/feed_frame", buf.tobytes())
            except Exception:
                pass
        sleep_for = interval - (time.time() - t0)
        if sleep_for > 0:
            time.sleep(sleep_for)


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


# Sized to the widest fixed-width content inside ~17px of window chrome
# (2x8 padding + 1): the 310px effect preview image (indent 0) and the
# 308px fx button rows.  The old 314px bar silently clipped both against
# its border; over the camera render the clipping shows, so the bar has
# to genuinely fit its contents.
_SIDEBAR_WIDTH = 330


def toggle_sidebar():
    # The sidebar is a floating overlay window, so hide/show is outright:
    # the preview underneath never moves or reflows.
    with state.lock:
        state.sidebar_visible = not state.sidebar_visible
        vis = state.sidebar_visible
    dpg.configure_item("sidebar_panel", show=vis)


_sidebar_fit_h = 0


def _fit_sidebar_height(*_args):
    """Keep the sidebar overlay as tall as the window.  Called every frame
    from the render loop: macOS settles the real window size a few frames
    after show_viewport (menu bar clamp, maximise), so a one-shot fit after
    startup lands short.  Reconfigures only when the height changes."""
    global _sidebar_fit_h
    vh = dpg.get_viewport_client_height()
    if vh > 0 and vh != _sidebar_fit_h and dpg.does_item_exist("sidebar_panel"):
        _sidebar_fit_h = vh
        dpg.configure_item("sidebar_panel", height=vh)


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
    # Cancel the other armed mode first so the spotlight's 15 Hz broadcast
    # doesn't keep stomping over the ripple a phone has just received (or
    # vice versa).  The mutual-cancel guards against re-entry via the early
    # `armed == _xxx_armed` exit at the top of each setter.
    if armed:
        _set_spotlight_armed(False)
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
        # Pop the Spotlight modal so the Radius slider is right there to
        # tune on the fly — without this you'd have to hunt for the ...
        # button before you can even see what the radius is set to.
        try:
            effects._open_modal("spotlight")
        except Exception:
            pass
    else:
        _last_spotlight_canvas_px = None
        post_json_async("/admin/effect/stop", {})
        # Tuck the modal away again so it doesn't camp on top of the
        # preview after the operator disarms.
        try:
            if dpg.does_item_exist("fx_modal_spotlight"):
                dpg.configure_item("fx_modal_spotlight", show=False)
        except Exception:
            pass


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
        radius_u = float(dpg.get_value("fx_spotlight_spatial_freq") or 0.09)
    except Exception:
        radius_u = 0.09
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


def draw_click_ripples(canvas: np.ndarray, flipped: bool = False):
    """Render in-flight click ripples on top of the canvas as concentric
    half-arches that open toward the nearest detected phone.  When
    `flipped`, the stored cx (room-frame canvas pixel) is mirrored to
    the display frame, and theta is reflected around the y-axis so the
    arc still points at the nearest phone in the mirrored view."""
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
        draw_cx    = (PREVIEW_WIDTH - 1 - cx) if flipped else cx
        draw_theta = (180 - theta) if (flipped and theta is not None) else theta
        # Draw three rings spaced in time so the effect feels like water.
        for ring in range(3):
            ring_prog = prog - ring * 0.18
            if ring_prog <= 0 or ring_prog >= 1:
                continue
            radius = int(ring_prog * _CLICK_RIPPLE_MAX_RADIUS)
            alpha  = (1 - ring_prog) ** 1.5
            color = tuple(int(c * alpha) for c in _RIPPLE_BGR)
            thickness = max(1, int(3 * alpha))
            if draw_theta is None:
                cv2.circle(canvas, (draw_cx, cy), radius, color, thickness, cv2.LINE_AA)
            else:
                # 180° arc facing theta (image y is down, so atan2 already
                # matches OpenCV's clockwise-from-+x convention).
                cv2.ellipse(canvas, (draw_cx, cy), (radius, radius), 0,
                            draw_theta - 90, draw_theta + 90,
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
    if not _detected_ids:
        return   # nothing to report

    # Snapshot state on the caller's (render) thread so the report reflects the
    # moment of the stop/reset, then do the blocking fetch_json + file write +
    # open() on a worker thread — fetch_json can stall up to 0.5s on a slow
    # server and must never block the render loop.  The snapshot also means a
    # reset_server() that clears _detected_ids right after this call can't empty
    # the report mid-generation.
    snap = dict(
        detected_ids      = set(_detected_ids),
        detection_timings = dict(_detection_timings),
        detection_start   = _detection_start_time,
    )

    def _work():
        global _report_saved_path
        try:
            stats = fetch_json("/admin/show_stats") or {}
            path  = report.generate(
                detected_ids      = snap["detected_ids"],
                detection_timings = snap["detection_timings"],
                detection_start   = snap["detection_start"],
                like_count        = stats.get("like_count", 0),
                total_connected   = stats.get("total_connected", len(snap["detected_ids"])),
            )
            _report_saved_path = path
            if auto_open:
                import subprocess
                subprocess.Popen(["open", path])
            set_status(f"Report saved: {_os.path.basename(path)}")
            log.info(f"[report] saved → {path}")
        except Exception as e:
            log.warning(f"[report] failed: {e}")

    threading.Thread(target=_work, daemon=True).start()


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
            set_status("Plug in the Elgato Facecam - waiting...")
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


def _heading(label: str, indent: int = None):
    """Sidebar section heading: bold when Verdana Bold is available, and
    no separator line underneath."""
    t = dpg.add_text(label, color=(160, 160, 160),
                     indent=_PAD if indent is None else indent)
    if dpg.does_item_exist("heading_font"):
        dpg.bind_item_font(t, "heading_font")
    return t


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
    game.init(state, set_status, post_json, fetch_json, _render_order, ui_queue=ui_queue)
    dpg.create_context()

    # Real font instead of the 13px bitmap default: Verdana was designed
    # for screen legibility at small sizes. Loaded at 2x and drawn at
    # 0.5 global scale so glyphs rasterise retina-crisp at an effective
    # 16px (up from 13).
    _FONT_PATH = "/System/Library/Fonts/Supplemental/Verdana.ttf"
    _ui_font = _tab_font = None
    if _os.path.exists(_FONT_PATH):
        _BOLD_PATH = "/System/Library/Fonts/Supplemental/Verdana Bold.ttf"
        with dpg.font_registry():
            _ui_font  = dpg.add_font(_FONT_PATH, 32)
            _tab_font = dpg.add_font(_FONT_PATH, 52)   # tab labels: effective 26px
            if _os.path.exists(_BOLD_PATH):
                # Sidebar section headings: same family and size, bold face
                dpg.add_font(_BOLD_PATH, 32, tag="heading_font")
        dpg.bind_font(_ui_font)
        dpg.set_global_font_scale(0.5)
    global _hud_font
    _hud_font = _ui_font

    # Global dark theme: pure-black chrome matching the brand (#020204).
    # Interactive fills stay a step lighter so controls keep affordance.
    with dpg.theme() as _global_black:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg,        (2, 2, 4, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg,         (2, 2, 4, 255))
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg,         (8, 8, 12, 255))
            dpg.add_theme_color(dpg.mvThemeCol_MenuBarBg,       (2, 2, 4, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBg,         (2, 2, 4, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive,   (8, 8, 12, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg,         (18, 18, 24, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered,  (30, 30, 38, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive,   (40, 40, 50, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Button,          (22, 22, 28, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,   (38, 38, 48, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,    (50, 50, 62, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Header,          (24, 24, 30, 255))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered,   (36, 36, 45, 255))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive,    (46, 46, 58, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Tab,             (10, 10, 14, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TabHovered,      (48, 42, 22, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TabActive,       (82, 66, 22, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Border,          (255, 255, 255, 24))
            dpg.add_theme_color(dpg.mvThemeCol_Separator,       (255, 255, 255, 24))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg,     (2, 2, 4, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab,   (40, 40, 50, 255))
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark,       (255, 200, 50, 255))
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrab,      (120, 120, 135, 255))
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive,(255, 200, 50, 255))
    dpg.bind_theme(_global_black)

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

    # Sidebar overlay: the window itself uses no_background; child windows
    # inside it must also go clear or they'd paint opaque slabs over the
    # camera render.
    with dpg.theme(tag="sidebar_theme"):
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (0, 0, 0, 0))

    with dpg.texture_registry(show=False):
        blank = np.zeros(PREVIEW_HEIGHT * PREVIEW_WIDTH * 4, dtype=np.float32)
        # Wordmark for the sidebar header (white-on-transparent raster of
        # the site's pixelmesh_text.svg; DPG cannot render SVG directly)
        _wm = cv2.imread(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                       "public", "pixelmesh_text.png"),
                         cv2.IMREAD_UNCHANGED)
        if _wm is not None:
            _wm = cv2.cvtColor(_wm, cv2.COLOR_BGRA2RGBA).astype(np.float32) / 255.0
            dpg.add_static_texture(_wm.shape[1], _wm.shape[0],
                                   _wm.flatten().tolist(), tag="wordmark_texture")

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

        # ---- Preview: always fills the full window; sidebar floats above ----
        with dpg.child_window(tag="preview_panel", border=False,
                              width=-1, height=-1,
                              no_scrollbar=True, no_scroll_with_mouse=True):
            dpg.add_image("camera_texture", tag="preview_image",
                          width=1, height=1)
    dpg.bind_item_theme("preview_panel", "preview_panel_theme")

    # ---- Sidebar: transparent overlay window on top of the preview.
    # Tab shows/hides it outright (toggle_sidebar); the preview never
    # reflows because it no longer shares a layout row with the sidebar.
    # no_scrollbar matters for width: a visible scrollbar steals 14px of
    # content region and clips the fixed-width rows (fx buttons, MIDI
    # text).  Wheel scrolling still works if content ever overflows.
    with dpg.window(tag="sidebar_panel", pos=(0, 0),
                    width=_SIDEBAR_WIDTH, height=800,
                    no_title_bar=True, no_resize=True, no_move=True,
                    no_collapse=True, no_background=True,
                    no_scrollbar=True):


        if dpg.does_item_exist("wordmark_texture"):
            dpg.add_spacer(height=4)
            # 546x107 source at ~0.36 scale fits the 314px sidebar
            dpg.add_image("wordmark_texture", width=196, height=38,
                          indent=(_SIDEBAR_WIDTH - 196) // 2 - 4)
            dpg.add_spacer(height=2)
        else:
            dpg.add_text("pixelmesh", color=(255, 200, 50), indent=_PAD)
        dpg.add_separator()
        dpg.add_spacer(height=2)

        # Tabs region auto-sizes to its content so the MIDI panel
        # sits directly beneath the active tab instead of being
        # pinned to the window bottom with dead space above it.
        with dpg.child_window(auto_resize_y=True, border=False):
            with dpg.tab_bar(tag="main_tabs"):

                # ---- SCENE tab (default) ----
                with dpg.tab(label="SCENE"):
                    with dpg.group(tag="scene_body"):
                        dpg.add_spacer(height=4)
                        _heading("CAMERA HUB")
                        _chk("Auto Exposure", "chk_ae", _toggle_ae, enabled=False)
                        _chk("Flip Projection  [F]", "chk_flip_projection",
                             toggle_flip_projection)
                        dpg.add_text("ISO Gain", color=(180, 180, 180), indent=_PAD)
                        dpg.add_slider_int(label="##iso", tag="sld_iso",
                                           default_value=elgato._DEFAULT_GAIN,
                                           min_value=0, max_value=160,
                                           callback=_set_iso,
                                           indent=_PAD, width=-(_PAD + 1),
                                           enabled=False)
                        dpg.add_text("", tag="iso_hint_text",
                                     color=(220, 180, 80), indent=_PAD,
                                     wrap=300, show=False)

                        dpg.add_spacer(height=8)
                        _heading("FRAME ROI")
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
                        _heading("CAPTURE")
                        _chk("Record Video  [V]", "chk_recording", lambda: toggle_recording())
                        dpg.add_text("[REC]", tag="rec_status_text",
                                     color=(220, 60, 60), indent=_PAD, show=False)
                        dpg.add_text("", tag="rec_filename_text",
                                     color=(150, 150, 150), indent=_PAD, show=False,
                                     wrap=300)

                        # ---- MIDI panel ----
                        dpg.add_spacer(height=8)
                        with dpg.group(horizontal=True):
                            _heading("MIDI")
                            dpg.add_text("waiting for pedal", tag="midi_conn_text",
                                         color=(120, 120, 120))
                        dpg.add_spacer(height=2)
                        # Newest command big and bright, history dim below.
                        with dpg.child_window(height=260, border=False):
                            dpg.add_spacer(height=2)
                            dpg.add_text("no commands yet", tag="midi_last_text",
                                         color=(255, 200, 50), indent=6, wrap=290)
                            dpg.add_separator()
                            dpg.add_text("", tag="midi_history_text",
                                         color=(130, 130, 130), indent=6, wrap=290)

                # ---- RUN tab ----
                with dpg.tab(label="RUN"):
                    with dpg.group(tag="run_body"):
                        dpg.add_spacer(height=4)
                        _heading("DETECTION")
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
                    with dpg.group(tag="game_body"):
                        dpg.add_spacer(height=4)
                        game.build_sidebar_buttons(indent=_PAD, pad=_PAD)

                        dpg.add_spacer(height=8)
                        _heading("HEARTS")
                        dpg.add_button(label="Reset Like Counter",
                                       callback=heart_reset,
                                       indent=_PAD, width=-(_PAD + 1))
                        dpg.add_button(label="Enable / Disable Likes",
                                       callback=heart_toggle,
                                       indent=_PAD, width=-(_PAD + 1))
    dpg.bind_item_theme("sidebar_panel", "sidebar_theme")

    # ---- Per-effect settings modals (hidden until ... is clicked) ----
    effects.build_window()

    # ---- Bug game leaderboard window ----
    game.build_window()

    # ---- Crisp HUD layer (fps/found pills bottom-right + ROI label) ----
    # Front viewport drawlist: renders above every window, captures no
    # mouse input.  _update_hud positions everything each frame.
    with dpg.viewport_drawlist(front=True, tag="hud_draw"):
        dpg.draw_rectangle((0, 0), (1, 1), tag="hud_pill_bg",
                           fill=(16, 16, 18, 235), color=(70, 70, 70, 255),
                           rounding=6)
        dpg.draw_circle((0, 0), 5, tag="hud_dot", fill=(95, 95, 95, 255),
                        color=(0, 0, 0, 0))
        dpg.draw_text((0, 0), "", tag="hud_fps_text", size=_HUD_TXT,
                      color=(215, 215, 215, 255))
        dpg.draw_rectangle((0, 0), (1, 1), tag="hud_found_bg",
                           fill=(16, 16, 18, 235), color=(255, 170, 40, 255),
                           rounding=6, show=False)
        dpg.draw_text((0, 0), "", tag="hud_found_text", size=_HUD_TXT,
                      color=(215, 215, 215, 255), show=False)
        dpg.draw_rectangle((0, 0), (1, 1), tag="roi_bg",
                           fill=(10, 10, 12, 235), color=(255, 160, 80, 255),
                           rounding=4, show=False)
        dpg.draw_text((0, 0), "", tag="roi_line1", size=_HUD_TXT,
                      color=(255, 160, 80, 255), show=False)
        dpg.draw_text((0, 0), "", tag="roi_line2", size=_HUD_TXT,
                      color=(230, 200, 180, 255), show=False)
    if _ui_font:
        dpg.bind_item_font("hud_draw", _ui_font)

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
    _fit_sidebar_height()   # first guess now; the render loop keeps it true
    # macOS Dock icon: GLFW ignores viewport icons on Cocoa (why earlier
    # attempts never showed) - set it through AppKit instead.
    try:
        from AppKit import NSApplication, NSImage
        _icon_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                   "public", "app_icon.png")
        _icon = NSImage.alloc().initWithContentsOfFile_(_icon_path)
        if _icon:
            NSApplication.sharedApplication().setApplicationIconImage_(_icon)
    except Exception as e:
        log.info(f"[gui] dock icon not set: {e}")
    # Tab labels get the larger face; each tab's body group rebinds the
    # normal font so content is unaffected (item fonts cascade in DPG).
    if _tab_font is not None:
        dpg.bind_item_font("main_tabs", _tab_font)
        for _body in ("scene_body", "run_body", "game_body"):
            if dpg.does_item_exist(_body):
                dpg.bind_item_font(_body, _ui_font)
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

    def _midi_toggle_detection():
        # Pedal semantics: switch 1 starting detection means "fresh run" -
        # reset first (clears positions server-side and locally), settle so
        # the async /admin/reset lands before /admin/detect, then start.
        # Stomping while detecting just stops, no reset. Keyboard D keeps
        # plain toggle semantics.
        with state.lock:
            detecting = state.detecting
        if not detecting:
            reset_server()
            time.sleep(0.4)
        toggle_detection()

    def _midi_fire_effect(name):
        # Showtime stomp: kill all camera overlays first (the H toggle,
        # forced off rather than flipped) so the projected feed is clean
        # the moment effects start.
        with state.lock:
            if state.show_overlays:
                state.show_overlays = False
        ui_queue.put(("_midi_effect", name))

    midi.midi.start(
        trigger_effect = _midi_fire_effect,
        toggle_detect  = _midi_toggle_detection,
        set_iso        = lambda v: elgato.set_iso(v),
        set_recording  = _midi_set_recording,
        set_overlays   = _midi_set_overlays,
        set_sync       = _midi_set_sync,
        reset          = reset_server,
    )

    threading.Thread(target=_stream_worker, daemon=True,
                     name="stream").start()

    texture_data = frame_to_texture(no_camera_canvas())
    _dbg_counter = 0   # local to main — throttles debug save_frame calls

    try:
        while dpg.is_dearpygui_running():
            _frame_t0 = time.time()
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
                    with state.lock:
                        detecting = state.detecting
                        show_ov   = state.show_device_overlay

                    # Hand frame to detection thread (non-blocking).
                    # If it's busy the frame is dropped — display continues unblocked.
                    # Only the detector needs a stable copy (cap reuses its buffer
                    # on the next read); when not detecting we skip the ~6 MB/frame
                    # copy entirely.  (state.latest_frame was write-only dead state.)
                    if detecting:
                        raw_copy = raw.copy()
                        try:
                            _detect_queue.put_nowait((raw_copy, time.time()))
                        except Full:
                            pass

                    # Downscale first, then apply gamma/contrast on the 720p canvas
                    # in-place — 6.7× less pixel work and no ~6 MB/frame allocation
                    # churn vs. correcting the full-res frame and immediately
                    # discarding it.  Detection is fed the untouched `raw`, so this
                    # reordering is display-only and does not affect the decoder.
                    canvas = build_canvas(raw)
                    apply_gamma(canvas, dst=canvas)
                    apply_contrast(canvas, dst=canvas)

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

                    # Scene → Flip Projection: mirror the canvas BEFORE
                    # the user-facing overlays so ROI/device/winner/HUD
                    # text and click ripples all render upright on the
                    # display.  Detector debug overlays drawn earlier
                    # (frozen code) end up mirrored, which is fine —
                    # they're for offline debugging, not show-time.  The
                    # raw camera frame queued for detection is untouched,
                    # and _on_preview_click inverts x so the click → u
                    # conversion still maps to the real room position.
                    flipped = state.flip_projection
                    display_canvas = cv2.flip(canvas, 1) if flipped else canvas

                    draw_roi_overlay(display_canvas, flipped=flipped)

                    if show_ov:
                        draw_device_overlay(display_canvas, flipped=flipped)

                    try:
                        draw_winner_highlight(display_canvas, flipped=flipped)
                    except Exception as e:
                        log.info(f"[winner] draw skipped: {e}")

                    draw_click_ripples(display_canvas, flipped=flipped)

                    if detecting:
                        draw_detect_border(display_canvas)

                    fps = 1.0 / max(time.time() - frame_start, 1e-4)
                    _camera_fps = 0.9 * _camera_fps + 0.1 * fps
                    _update_hud(fps)
                    _draw_spotlight_cursor(display_canvas)

                    # Record AFTER the spotlight cursor so the post-show
                    # video matches what was actually on the projector —
                    # same frame the MJPEG write below sees.  (The fps and
                    # found pills moved to the DPG drawlist HUD, so they no
                    # longer appear in recordings or the stream.)
                    if vid_rec.active:
                        vid_rec.record(display_canvas)

                    # Debug run.mp4 must also record after the spotlight
                    # draw: it queues the frame to a writer thread, and an
                    # earlier call site let in-place draws race the writer —
                    # overlays flashed on/off frame to frame.
                    if dbg_cap.active:
                        dbg_cap.record_frame(display_canvas)

                    # MJPEG stream — hand the finished frame to the
                    # encode/POST worker (reference swap, GIL-atomic).
                    global _stream_latest
                    _stream_latest = display_canvas

                    texture_data = frame_to_texture(display_canvas)

            # Update texture
            dpg.set_value("camera_texture", texture_data)

            _fit_sidebar_height()

            # Fit preview image to available space.  preview_panel spans the
            # whole main_window (the sidebar floats above it), so its rect is
            # the full usable area.
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
            effects.tick_preview()   # main-thread preview render (self-throttled ~10fps)

            # Drain UI queue — set _ui_syncing so checkbox set_value calls
            # don't re-fire toggle callbacks in DearPyGui versions that
            # invoke callbacks on set_value.
            global _ui_syncing
            _ui_syncing = True
            try:
                while not ui_queue.empty():
                    tag, value = ui_queue.get()
                    if tag == "_game_active_btn":
                        game.set_active_btn(value)
                        continue
                    if tag == "_active_effect":
                        for n in effects.EFFECT_LABELS:
                            btn = f"fx_btn_{n}"
                            if dpg.does_item_exist(btn):
                                if n == value:
                                    dpg.bind_item_theme(btn, "fx_active_theme")
                                else:
                                    dpg.bind_item_theme(btn, None)
                        continue
                    if tag == "_midi_effect":
                        # Pedal-fired effect: same UI-thread constraint as
                        # _refire_effect (trigger_effect reads dpg values).
                        try:
                            effects.trigger_effect(value)
                        except Exception as e:
                            log.warning(f"[effect] pedal fire failed: {e}")
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
                    if tag == "_midi_conn_color":
                        dpg.configure_item("midi_conn_text",
                                           color=(80, 200, 80) if value else (120, 120, 120))
                        continue
                    if tag == "_iso_hint_show":
                        dpg.configure_item("iso_hint_text", show=value)
                        continue
                    if tag == "_roi_enabled":
                        for item in ("sld_roi_top", "sld_roi_bottom",
                                     "sld_roi_left", "sld_roi_right"):
                            dpg.enable_item(item) if value else dpg.disable_item(item)
                        continue
                    if tag == "_elgato_enabled":
                        for item in ("chk_ae", "sld_iso"):
                            if value:
                                dpg.enable_item(item)
                            else:
                                dpg.disable_item(item)
                        continue
                    try:
                        dpg.set_value(tag, value)
                    except Exception as e:
                        log.info(f"[ui] queue error tag={tag} err={e}")
            finally:
                _ui_syncing = False

            dpg.render_dearpygui_frame()
            _perf_tick(_frame_t0)

    finally:
        # Session-long debug captures are only stopped here or by the user —
        # finalise run.mp4 + summary.json before anything else can throw.
        if dbg_cap.active:
            try:
                dbg_cap.stop_run()
            except Exception as e:
                log.info(f"[shutdown] debug capture close failed: {e}")
        post_json("/admin/reset", {})
        cap = holder.get("cap")
        if cap:
            cap.release()
        # Push a blank frame so /internal/feed/v1 doesn't keep serving the
        # last camera image after shutdown.
        try:
            blank = np.zeros((PREVIEW_HEIGHT, PREVIEW_WIDTH, 3), dtype=np.uint8)
            ok, buf = cv2.imencode(".jpg", blank, [cv2.IMWRITE_JPEG_QUALITY, 60])
            if ok:
                post_bytes("/admin/feed_frame", buf.tobytes())
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

        # Cap the detector at 20fps. It only needs ~10fps for clean
        # 300ms-phase sampling but was running at whatever the camera
        # delivered (30-170fps at MaccTech), and its GIL-heavy decode
        # bursts starved the display thread: measured 240ms display
        # stalls during the decode window vs a flat 40ms otherwise.
        # 20fps keeps 6 samples per phase (2x the minimum) and returns
        # the interpreter to the UI between frames. Decode cadence and
        # warmup are wall-clock based, so detection times are unchanged.
        _since = ts - _det_last_ts
        if _since < 0.05:
            continue          # maxsize=1 queue: this drop keeps the newest
        _det_last_ts = ts

        try:
            t_frame_start = time.time()
            results, dbg_imgs = detector.process_frame(raw, ts, need_debug=dbg_cap.active,
                                                       valid_ids=_valid_blink_ids or None)
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



if __name__ == "__main__":
    main()
