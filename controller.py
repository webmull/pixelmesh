# (c) Adam Davis - adamdavis.co.uk
#!/usr/bin/env python3
"""
pixelmesh — controller

Dear PyGui controller with:
  - Live camera preview
  - Blink detection overlay (replaces AprilTag detection)
  - Device position mapping to u-space
  - Effect triggers
  - Client count polling

Hotkeys:
  D       Toggle detection
  S       Toggle clock sync
  1-7     Trigger effects
  R       Reset server
  Tab     Toggle sidebar
  G       Debug capture
  V       Record video
  O       ID overlays
  Q/Esc   Quit

Dependencies:
  pip install dearpygui opencv-python numpy requests
"""

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
from camera import apply_gamma, apply_contrast, apply_sharpen
from blink_detector import BlinkDetector
from debug_capture import DebugCapture
from video_recorder import VideoRecorder
from network import post_json, post_json_async, fetch_client_count, fetch_json
from log import log
import effects
import elgato

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
_detected_ids: set = set()   # blink_ids seen this detection session
_valid_blink_ids: set[int] = set()  # blink_ids assigned to connected clients (empty = not fetched yet)
_timing_log_paths: list[str] = []   # may be 1 or 2 paths (master + run)

_CALIBRATION_LOG_DIR = _os.path.join(_os.path.dirname(__file__), "debug", "calibration_logs")

def _open_timing_log():
    global _timing_log_paths
    header = (f"detection started {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"{'blink_id':>10}  {'time_to_detect':>16}  {'confidence':>12}\n")
    paths = []
    # Always write to the master calibration_logs folder
    _os.makedirs(_CALIBRATION_LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    paths.append(_os.path.join(_CALIBRATION_LOG_DIR, f"{stamp}.log"))
    # Also duplicate into the active debug run folder if one is in progress
    if dbg_cap.active and dbg_cap.run_dir:
        paths.append(_os.path.join(dbg_cap.run_dir, "calibration.log"))
    for p in paths:
        with open(p, "w") as f:
            f.write(header)
    _timing_log_paths = paths

def _log_timing(line: str):
    for p in _timing_log_paths:
        with open(p, "a") as f:
            f.write(line + "\n")


vid_rec = VideoRecorder()


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
    with state.lock:
        positions = state.calibrated_positions.copy()
        crop_x = state.last_crop_x
        crop_y = getattr(state, "last_crop_y", 0)

    for blink_id, pos in positions.items():
        if _valid_blink_ids and blink_id not in _valid_blink_ids:
            continue
        u, v = pos["u"], pos["v"]
        px = int(u * (PREVIEW_WIDTH  + 2 * crop_x) - crop_x)
        py = int(v * (PREVIEW_HEIGHT + 2 * crop_y) - crop_y)
        label = str(blink_id)
        (tw, th), _ = cv2.getTextSize(label, FONT, 0.55, 1)
        pad = 5
        x1, y1 = px - tw // 2 - pad, py - th // 2 - pad - 1
        x2, y2 = px + tw // 2 + pad, py + th // 2 + pad + 1
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (20, 20, 20), -1)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 220, 80), 2)
        cv2.putText(canvas, label, (px - tw // 2, py + th // 2),
                    FONT, 0.55, (255, 255, 255), 1, cv2.LINE_AA)


def draw_hud(canvas: np.ndarray, fps: float):
    with state.lock:
        detecting = state.detecting

    dot_color  = (40, 210, 80) if detecting else (70, 70, 70)
    state_str  = "DET" if detecting else "OFF"
    disp_str   = f"{int(fps + 0.5)} fps"
    det_str    = f"det {int(_detect_fps + 0.5)} fps" if detecting else ""
    label      = f"{disp_str}  {det_str}  {state_str}".strip() if det_str else f"{disp_str}  {state_str}"

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

    # Weak signal indicator — amber dot just right of the fps pill
    if detector.signal_range < 0.5:
        cv2.circle(canvas, (bx1 + PAD + 5, mid_y), 4, (0, 165, 255), -1)

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
    ui_queue.put(("_active_effect", effect))

    safe_set("rec_status_text", "[REC]" if vid_rec.active else "")
    ui_queue.put(("_rec_status_show", vid_rec.active))

    safe_set("chk_detection", detecting)
    safe_set("chk_sync",     state.syncing)
    safe_set("chk_overlays", state.show_device_overlay)
    safe_set("chk_debug",    dbg_cap.active)
    safe_set("chk_recording", vid_rec.active)

    _push_elgato_state()


# ------------------------------------------------------------------ #
# Elgato callbacks
# ------------------------------------------------------------------ #

def _push_elgato_state():
    connected = elgato.connected
    safe_set("elgato_status", "[ON]" if connected else "[OFF]")
    ui_queue.put(("elgato_indent", (_CHK_INDENT - 8) if connected else (_CHK_INDENT - 15)))
    ui_queue.put(("elgato_color", connected))
    safe_set("chk_ae",  elgato.ae_on)
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


def toggle_detection():
    if _ui_syncing:
        return
    if _no_camera():
        return
    with state.lock:
        state.detecting = not state.detecting
        val = state.detecting

    if val:
        global _detection_start_time, _detected_ids
        _detection_start_time = time.time()
        _detected_ids = set()
        detector.reset()
        with state.lock:
            state.calibrated_positions.clear()
        post_json_async("/admin/detect", {"detecting": True})
        _open_timing_log()
        set_status("Detection ON")
    else:
        with state.lock:
            state.calibrated_positions.clear()
            state.show_device_overlay = False
        post_json_async("/admin/detect", {"detecting": False})
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


def toggle_sidebar():
    with state.lock:
        state.sidebar_visible = not state.sidebar_visible
        vis = state.sidebar_visible
    dpg.configure_item("sidebar_panel", show=vis)


def toggle_device_overlay():
    if _no_camera():
        return
    with state.lock:
        state.show_device_overlay = not state.show_device_overlay


def reset_server():
    global _detected_ids, _detection_start_time
    post_json_async("/admin/reset", {})
    detector.reset()
    _detected_ids = set()
    _detection_start_time = 0.0
    with state.lock:
        state.detecting = False
        state.syncing = False
        state.calibrated_positions.clear()
        state.last_detections = []
        state.last_detection_count = 0
    set_status("Reset")


def heart_reset():
    post_json_async("/admin/heart/reset", {})
    set_status("Likes reset")


def heart_toggle():
    post_json_async("/admin/heart/toggle", {})
    set_status("Likes toggled")


trigger_effect = effects.trigger_effect
def camera_scan_worker(holder=None):
    names  = _avfoundation_device_names()
    cams   = find_cameras(8)
    labels = [names.get(i, f"Camera {i}") for i in cams] or ["No cameras found"]
    lmap   = {names.get(i, f"Camera {i}"): i for i in cams}

    with state.lock:
        state.cameras                = cams
        state.camera_listbox_items   = labels
        state.camera_label_to_index  = lmap

    safe_set("camera_selector_items", labels)
    log.info(f"[camera] scan complete: {labels}")

    # Auto-open Facecam 4K if present, otherwise first camera found
    if holder is not None and cams:
        preferred = next(
            (l for l in labels if "facecam" in l.lower() or "elgato" in l.lower()),
            labels[0],
        )
        idx = lmap.get(preferred)
        if idx is not None:
            cap = open_camera(idx)
            if cap:
                old = holder.get("cap")
                if old:
                    old.release()
                holder["cap"] = cap
                with state.lock:
                    state.selected_camera_idx = idx
                log.info(f"[camera] auto-opened {preferred}")



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

    elif key == dpg.mvKey_O:
        toggle_device_overlay()

    elif key == dpg.mvKey_1:
        trigger_effect("wave")

    elif key == dpg.mvKey_2:
        trigger_effect("gradient")

    elif key == dpg.mvKey_3:
        trigger_effect("binary_wave")

    elif key == dpg.mvKey_4:
        trigger_effect("pulse")

    elif key == dpg.mvKey_5:
        trigger_effect("rainbow")

    elif key == dpg.mvKey_6:
        trigger_effect("colour_flood")

    elif key == dpg.mvKey_7:
        trigger_effect("aurora")



# ------------------------------------------------------------------ #
# UI setup
# ------------------------------------------------------------------ #

_PAD        = 8     # left/right padding for sidebar content
_CHK_INDENT = 284   # checkbox x position
_KEY_INDENT = 252   # hotkey label x position (flush left of checkbox)

def _chk(label: str, tag: str, callback, enabled: bool = True):
    """Checkbox row: label left, hotkey right-aligned before checkbox."""
    import re as _re
    m = _re.search(r'\s*(\[[^\]]+\])\s*$', label)
    base   = label[:m.start()] if m else label
    hotkey = m.group(1)        if m else ""
    with dpg.group(horizontal=True):
        dpg.add_text(base, indent=_PAD)
        if hotkey:
            dpg.add_text(hotkey, indent=_KEY_INDENT, color=(120, 120, 120))
        dpg.add_checkbox(label=f"##{tag}", tag=tag,
                         callback=callback, indent=_CHK_INDENT,
                         enabled=enabled)


def setup_ui(holder: dict):
    effects.init(state, set_status)
    dpg.create_context()

    # Theme for the currently active effect button
    with dpg.theme(tag="fx_active_theme"):
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button,        (180, 120, 20, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (210, 150, 40, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,  (220, 160, 50, 255))

    with dpg.texture_registry(show=False):
        blank = np.zeros(PREVIEW_HEIGHT * PREVIEW_WIDTH * 4, dtype=np.float32)
        dpg.add_dynamic_texture(PREVIEW_WIDTH, PREVIEW_HEIGHT, blank,
                                tag="camera_texture")

    with dpg.handler_registry():
        dpg.add_key_press_handler(
            callback=lambda s, a: on_key_press(a, holder)
        )

    with dpg.window(tag="main_window", label=WINDOW_TITLE,
                    no_resize=True, no_move=True, no_collapse=True,
                    width=-1, height=-1):

        with dpg.group(horizontal=True):

            # ---- Sidebar ----
            with dpg.child_window(width=320, height=-1, border=True,
                                  tag="sidebar_panel"):

                dpg.add_text("pixelmesh", color=(255, 200, 50), indent=_PAD)
                dpg.add_separator()

                dpg.add_spacer(height=4)
                dpg.add_text("STATUS", color=(160, 160, 160), indent=_PAD)
                dpg.add_separator()
                dpg.add_text("", tag="status_text",  indent=_PAD)
                dpg.add_text("", tag="clients_text", indent=_PAD)
                dpg.add_text("", tag="detect_text",  indent=_PAD)
                dpg.add_separator()

                dpg.add_spacer(height=4)
                dpg.add_text("DETECTION", color=(160, 160, 160), indent=_PAD)
                dpg.add_separator()
                _chk("Detection  [D]",    "chk_detection", lambda: toggle_detection())
                _chk("Clock Sync  [S]",   "chk_sync",       lambda: toggle_sync())
                _chk("ID Overlays  [O]",  "chk_overlays",  lambda: toggle_device_overlay())
                _chk("Debug Capture  [G]","chk_debug",      lambda: toggle_debug())
                _chk("Record Video  [V]", "chk_recording",  lambda: toggle_recording())
                dpg.add_text("[REC]", tag="rec_status_text",
                             color=(220, 60, 60), show=False, indent=_PAD)
                dpg.add_spacer(height=4)
                dpg.add_button(label="Reset Server  [R]",
                               callback=reset_server,
                               indent=_PAD, width=-(_PAD + 1))
                dpg.add_spacer(height=4)
                dpg.add_text("HEARTS", color=(160, 160, 160), indent=_PAD)
                dpg.add_separator()
                dpg.add_button(label="Reset Like Counter",
                               callback=heart_reset,
                               indent=_PAD, width=-(_PAD + 1))
                dpg.add_button(label="Enable / Disable Likes",
                               callback=heart_toggle,
                               indent=_PAD, width=-(_PAD + 1))
                dpg.add_button(label="Sync Stats Panel",
                               callback=lambda: dpg.configure_item(
                                   "sync_debug_window",
                                   show=not dpg.is_item_shown("sync_debug_window")
                               ), indent=_PAD, width=-(_PAD + 1))

                dpg.add_spacer(height=4)
                dpg.add_text("CAMERA HUB", color=(160, 160, 160), indent=_PAD)
                dpg.add_separator()
                with dpg.group(horizontal=True):
                    dpg.add_text("Camera status", indent=_PAD)
                    dpg.add_text("[OFF]", tag="elgato_status",
                                 color=(120, 120, 120), indent=_CHK_INDENT - 15)
                _chk("Auto Exposure", "chk_ae", _toggle_ae, enabled=False)
                dpg.add_text("ISO Gain", color=(180, 180, 180), indent=_PAD)
                dpg.add_slider_int(label="##iso", tag="sld_iso",
                                   default_value=elgato._DEFAULT_GAIN,
                                   min_value=0, max_value=160,
                                   callback=_set_iso,
                                   indent=_PAD, width=-(_PAD + 1),
                                   enabled=False)

                dpg.add_spacer(height=4)
                dpg.add_text("EFFECTS", color=(160, 160, 160), indent=_PAD)
                dpg.add_separator()
                for _ename, _elabel in effects.EFFECT_LABELS.items():
                    with dpg.group(horizontal=True, indent=_PAD):
                        dpg.add_button(
                            label=_elabel,
                            tag=f"fx_btn_{_ename}",
                            callback=lambda s, a, u: effects.trigger_effect(u),
                            user_data=_ename,
                            width=262,
                        )
                        dpg.add_button(
                            label="...",
                            callback=lambda s, a, u: effects._open_modal(u),
                            user_data=_ename,
                            width=30,
                        )

            # ---- Preview panel ----
            with dpg.child_window(tag="preview_panel", border=False,
                                  width=-1, height=-1):
                dpg.add_image("camera_texture", tag="preview_image",
                              width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT)

    # ---- Per-effect settings modals (hidden until ... is clicked) ----
    effects.build_window()

    # ---- Sync debug window (hidden by default) ----
    with dpg.window(tag="sync_debug_window", label="Clock Sync Stats",
                    width=640, height=340, pos=(340, 60), show=False,
                    no_collapse=False):
        dpg.add_text("", tag="sync_status_line", color=(160, 160, 160))
        dpg.add_text(
            "RTT = round-trip ping time (lower = better network).  "
            "Offset = estimated clock difference vs server (ms); near 0 = well-synced.",
            color=(120, 120, 120),
            wrap=620,
        )
        dpg.add_spacer(height=4)
        dpg.add_text("Blink ID  Device        RTT(ms)  Offset(ms)  Samples  Age(s)",
                     color=(180, 180, 180))
        dpg.add_separator()
        dpg.add_text("No sync data — enable Clock Sync and wait for clients to report.",
                     tag="sync_no_data", color=(120, 120, 120))
        # Placeholder rows — up to 32 shown; extra rows hidden
        for i in range(32):
            dpg.add_text("", tag=f"sync_row_{i}", show=False)

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
    elgato.on_state_change = _elgato_state_changed
    elgato.start()

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
                _detected_ids = set()
                _detection_start_time = 0.0
                detector.reset()
                with state.lock:
                    state.detecting = False
                    state.calibrated_positions.clear()
                    state.last_detections = []
                    state.last_detection_count = 0
                post_json_async("/admin/detect", {"detecting": False})
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

                    if detecting:
                        # draw_overlay reads detector's cached state (_last_stds,
                        # _decoded_pts) written by the detection thread.  NumPy
                        # reference swaps are atomic under CPython's GIL so no
                        # explicit lock is needed — at worst we see one frame stale.
                        detector.draw_overlay(canvas, scale=_scale,
                                              crop_x=_crop_x, crop_y=_crop_y)

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

                    if show_ov and not detecting:
                        draw_device_overlay(canvas)

                    fps = 1.0 / max(time.time() - frame_start, 1e-4)
                    _camera_fps = 0.9 * _camera_fps + 0.1 * fps
                    draw_hud(canvas, fps)

                    if vid_rec.active:
                        vid_rec.record(canvas)

                    if dbg_cap.active:
                        dbg_cap.record_frame(canvas)

                    # MJPEG stream — write JPEG atomically so server.py
                    # never reads a partial file.  Capped at 12fps.
                    global _last_stream_ts
                    _now = time.time()
                    if _now - _last_stream_ts >= _STREAM_INTERVAL:
                        _last_stream_ts = _now
                        _tmp = _STREAM_PATH + ".new.jpg"
                        cv2.imwrite(_tmp, canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])
                        _os.replace(_tmp, _STREAM_PATH)

                    texture_data = frame_to_texture(canvas)

            # Update texture
            dpg.set_value("camera_texture", texture_data)

            # Fit preview image inside panel
            try:
                pw, ph = dpg.get_item_rect_size("preview_panel")
                if pw > 0 and ph > 0:
                    aspect = PREVIEW_WIDTH / PREVIEW_HEIGHT
                    if pw / ph > aspect:
                        iw, ih = int(ph * aspect), ph
                    else:
                        iw, ih = pw, int(pw / aspect)
                    x0 = max((pw - iw) // 2, 0)
                    y0 = 0  # pin to top — vertical centering creates a gap at full-screen height
                    dpg.configure_item("preview_image", width=iw, height=ih)
                    dpg.set_item_pos("preview_image", [x0, y0])
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
                    if tag == "_rec_status_show":
                        dpg.configure_item("rec_status_text", show=value)
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
                                      f"Last updated: {ts}  |  {len(rows)} device(s)")
                        dpg.configure_item("sync_no_data", show=(len(rows) == 0))
                        for i in range(32):
                            if i < len(rows):
                                r = rows[i]
                                rtt  = f"{r['rtt_ms']:.1f}"    if r["rtt_ms"]    is not None else "—"
                                off  = f"{r['offset_ms']:.1f}" if r["offset_ms"] is not None else "—"
                                line = (f"{str(r['blink_id']):>8}  "
                                        f"{r['device_id']:<12}  "
                                        f"{rtt:>7}  {off:>10}  "
                                        f"{r['samples']:>7}  {r['age_s']:>6}")
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
                    # _valid_blink_ids is empty until the first blink_map poll
                    # (≤ CLIENT_FETCH_SECS after start) — skip the check until
                    # then so detections aren't silently dropped on startup.
                    if _valid_blink_ids and det.blink_id not in _valid_blink_ids:
                        continue
                    # Freeze position at first detection — centroid drifts as
                    # noise points accumulate the same decoded ID over time.
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
                    elapsed = time.time() - _detection_start_time
                    _log_timing(
                        f"{det.blink_id:>10}  {elapsed:>14.2f}s  "
                        f"{det.confidence:>12.3f}"
                    )
                if positions:
                    post_json_async("/admin/positions", {"positions": positions})
        finally:
            _detect_queue.task_done()


def poll_clients():
    global _valid_blink_ids
    while state.running:
        fetch_client_count(state)
        data = fetch_json("/admin/blink_map")
        if data is not None:
            bmap = data.get("map", {})
            new_ids = {int(bid) for bid in bmap}
            if new_ids != _valid_blink_ids:
                _valid_blink_ids = new_ids
                # Prune positions for IDs that are no longer connected
                with state.lock:
                    stale = [bid for bid in state.calibrated_positions if bid not in new_ids]
                    for bid in stale:
                        del state.calibrated_positions[bid]
        time.sleep(CLIENT_FETCH_SECS)


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
