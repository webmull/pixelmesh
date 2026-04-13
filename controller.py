#!/usr/bin/env python3
"""
PixelMesh V2 — Controller

Dear PyGui controller with:
  - Live camera preview
  - Blink detection overlay (replaces AprilTag detection)
  - Device position mapping to u-space
  - Effect triggers
  - Client count polling

Hotkeys:
  D       Toggle detection
  K       Switch camera
  1-5     Trigger effects
  R       Reset server
  Tab     Toggle sidebar
  B       Blackout camera
  Q/Esc   Quit

Dependencies:
  pip install dearpygui opencv-python numpy requests
"""

import threading
import time
from queue import Queue, Full, Empty

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

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #

WINDOW_TITLE       = "PixelMesh V2"
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
ui_queue: Queue = Queue()

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
_timing_log_path: str = ""

import os as _os

_CALIBRATION_LOG_DIR = _os.path.join(_os.path.dirname(__file__), "debug", "calibration_logs")

def _open_timing_log():
    global _timing_log_path
    _os.makedirs(_CALIBRATION_LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    _timing_log_path = _os.path.join(_CALIBRATION_LOG_DIR, f"{stamp}.log")
    with open(_timing_log_path, "w") as f:
        f.write(f"detection started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'blink_id':>10}  {'time_to_detect':>16}  {'confidence':>12}\n")

def _log_timing(line: str):
    if not _timing_log_path:
        return
    with open(_timing_log_path, "a") as f:
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
    msg = "Camera initialising..."
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

    for blink_id, pos in positions.items():
        u, v = pos["u"], pos["v"]
        px = int(u * PREVIEW_WIDTH)
        py = int(v * PREVIEW_HEIGHT)
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

    dbg_label = f"Debug: REC ({dbg_cap.frame_idx} frames)" if dbg_cap.active else "Debug: OFF"

    safe_set("status_text",     status)
    safe_set("clients_text",    f"Clients: {clients}")
    safe_set("detect_text",     f"Detection: {'ON' if detecting else 'OFF'}  |  Sync: {'ON' if state.syncing else 'OFF'}")
    safe_set("det_count_text",  f"Blobs decoded: {len(_detected_ids)}")
    safe_set("effect_text",     f"Effect: {effect}")
    safe_set("debug_text",      dbg_label)
    safe_set("rec_status_text", "● RECORDING" if vid_rec.active else "")


# ------------------------------------------------------------------ #
# Actions
# ------------------------------------------------------------------ #

def toggle_sync():
    with state.lock:
        state.syncing = not state.syncing
        val = state.syncing
    post_json_async("/admin/sync", {"sync": val})
    set_status(f"Clock sync {'ON' if val else 'OFF'}")


def toggle_detection():
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
    if dbg_cap.active:
        dbg_cap.stop_run()
        set_status("Debug capture stopped")
    else:
        run_dir = dbg_cap.start_run()
        set_status(f"Debug → {run_dir}")


def toggle_recording():
    """Start or stop a plain video recording (hotkey V). Independent of debug capture."""
    if vid_rec.active:
        path = vid_rec.stop()
        set_status(f"Recording saved → {_os.path.basename(path)}")
    else:
        path = vid_rec.start()
        set_status(f"Recording → {_os.path.basename(path)}")


def toggle_sidebar():
    with state.lock:
        state.sidebar_visible = not state.sidebar_visible
        vis = state.sidebar_visible
    dpg.configure_item("sidebar_panel", show=vis)


def toggle_blackout():
    with state.lock:
        state.blackout_camera = not state.blackout_camera


def toggle_device_overlay():
    with state.lock:
        state.show_device_overlay = not state.show_device_overlay


def reset_server():
    post_json_async("/admin/reset", {})
    detector.reset()
    with state.lock:
        state.calibrated_positions.clear()
        state.syncing = False
    set_status("Reset sent")


def trigger_effect(name: str):
    color  = dpg.get_value("fx_color")    # [0–255, 0–255, 0–255, 255]
    color2 = dpg.get_value("fx_color2")
    payload = {
        "name":         name,
        "speed":        dpg.get_value("fx_speed"),
        "spatial_freq": 1.5,
        "bpm":          dpg.get_value("fx_bpm"),
        "angle":        dpg.get_value("fx_angle"),
        "color_r":      int(color[0]),
        "color_g":      int(color[1]),
        "color_b":      int(color[2]),
        "color2_r":     int(color2[0]),
        "color2_g":     int(color2[1]),
        "color2_b":     int(color2[2]),
        "split":        dpg.get_value("fx_split"),
        "orb_radius":   dpg.get_value("fx_orb_radius"),
    }
    log.info(f"[effect] {payload}")
    post_json_async("/admin/effect/fire", payload)
    with state.lock:
        state.current_effect = name
    set_status(f"Effect: {name}")


def _on_settings_changed(s, v):
    """Re-fire current effect immediately when any setting slider changes."""
    with state.lock:
        current = state.current_effect
    if current:
        trigger_effect(current)



# ------------------------------------------------------------------ #
# Camera switching
# ------------------------------------------------------------------ #

def switch_camera_next(holder: dict):
    with state.lock:
        cams = state.cameras
        idx  = state.selected_camera_idx

    if not cams:
        set_status("No cameras found")
        return

    idx = (idx + 1) % len(cams)
    cap = open_camera(cams[idx])
    if cap is None:
        set_status(f"Could not open camera {cams[idx]}")
        return

    old = holder.get("cap")
    if old:
        old.release()

    holder["cap"] = cap

    with state.lock:
        state.selected_camera_idx = idx
        state.status_text = f"Camera {cams[idx]}"


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

    elif key == dpg.mvKey_R:
        reset_server()

    elif key == dpg.mvKey_Tab:
        toggle_sidebar()

    elif key == dpg.mvKey_B:
        toggle_blackout()

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
        trigger_effect("orb")

    elif key == dpg.mvKey_8:
        trigger_effect("particles")


# ------------------------------------------------------------------ #
# UI setup
# ------------------------------------------------------------------ #

def setup_ui(holder: dict):
    dpg.create_context()

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

                dpg.add_text("PixelMesh V2", color=(255, 200, 50))
                dpg.add_separator()

                dpg.add_text("", tag="status_text")
                dpg.add_text("", tag="clients_text")
                dpg.add_text("", tag="detect_text")
                dpg.add_text("", tag="det_count_text")
                dpg.add_text("", tag="effect_text")
                dpg.add_separator()

                dpg.add_text("Detection")
                dpg.add_button(label="Toggle Detection  [D]",
                               callback=toggle_detection, width=-1)
                dpg.add_button(label="Toggle Clock Sync",
                               callback=toggle_sync, width=-1)
                dpg.add_button(label="Sync Debug Panel",
                               callback=lambda: dpg.configure_item(
                                   "sync_debug_window",
                                   show=not dpg.is_item_shown("sync_debug_window")
                               ),
                               width=-1)
                dpg.add_button(label="Toggle ID Overlays  [O]",
                               callback=toggle_device_overlay, width=-1)
                dpg.add_button(label="Toggle Debug Capture  [G]",
                               callback=toggle_debug, width=-1)
                dpg.add_text("", tag="debug_text")
                dpg.add_button(label="Record Video  [V]",
                               callback=toggle_recording, width=-1)
                dpg.add_text("", tag="rec_status_text", color=(220, 60, 60))

                dpg.add_spacer(height=6)
                dpg.add_text("Effects")
                dpg.add_button(label="1  Wave",
                               callback=lambda: trigger_effect("wave"),
                               width=-1)
                dpg.add_button(label="2  Gradient",
                               callback=lambda: trigger_effect("gradient"),
                               width=-1)
                dpg.add_button(label="3  Binary Wave",
                               callback=lambda: trigger_effect("binary_wave"),
                               width=-1)
                dpg.add_button(label="4  Pulse",
                               callback=lambda: trigger_effect("pulse"),
                               width=-1)
                dpg.add_button(label="5  Rainbow",
                               callback=lambda: trigger_effect("rainbow"),
                               width=-1)
                dpg.add_button(label="6  Colour Flood",
                               callback=lambda: trigger_effect("colour_flood"),
                               width=-1)
                dpg.add_button(label="7  Orb",
                               callback=lambda: trigger_effect("orb"),
                               width=-1)
                dpg.add_button(label="8  Particles",
                               callback=lambda: trigger_effect("particles"),
                               width=-1)

                dpg.add_spacer(height=4)
                dpg.add_text("Effect Settings", color=(200, 200, 200))
                dpg.add_text("Colour A", color=(160, 160, 160))
                dpg.add_color_edit(
                    label="##fx_color_lbl",
                    tag="fx_color",
                    default_value=(255, 255, 255, 255),
                    no_alpha=True,
                    width=-1,
                    callback=_on_settings_changed,
                )
                dpg.add_text("Colour B  (flood only)", color=(160, 160, 160))
                dpg.add_color_edit(
                    label="##fx_color2_lbl",
                    tag="fx_color2",
                    default_value=(255, 0, 0, 255),
                    no_alpha=True,
                    width=-1,
                    callback=_on_settings_changed,
                )
                dpg.add_text("Split  (flood only)", color=(160, 160, 160))
                dpg.add_slider_float(
                    label="##fx_split_lbl",
                    tag="fx_split",
                    default_value=0.5,
                    min_value=0.0, max_value=1.0,
                    format="%.2f",
                    width=-1,
                    callback=_on_settings_changed,
                )
                dpg.add_text("Orb Radius  (orb/particles)", color=(160, 160, 160))
                dpg.add_slider_float(
                    label="##fx_orb_radius_lbl",
                    tag="fx_orb_radius",
                    default_value=0.25,
                    min_value=0.05, max_value=0.6,
                    format="%.2f",
                    width=-1,
                    callback=_on_settings_changed,
                )
                dpg.add_text("Speed", color=(160, 160, 160))
                dpg.add_slider_float(
                    label="##fx_speed_lbl",
                    tag="fx_speed",
                    default_value=0.4,
                    min_value=0.05, max_value=4.0,
                    width=-1,
                    callback=_on_settings_changed,
                )
                dpg.add_text("Direction", color=(160, 160, 160))
                dpg.add_slider_float(
                    label="##fx_angle_lbl",
                    tag="fx_angle",
                    default_value=0.0,
                    min_value=0.0, max_value=360.0,
                    format="%.0f°",
                    width=-1,
                    callback=_on_settings_changed,
                )
                dpg.add_text("BPM  (pulse)", color=(160, 160, 160))
                dpg.add_slider_float(
                    label="##fx_bpm_lbl",
                    tag="fx_bpm",
                    default_value=100.0,
                    min_value=20.0, max_value=300.0,
                    format="%.0f",
                    width=-1,
                    callback=_on_settings_changed,
                )

                dpg.add_spacer(height=6)
                dpg.add_button(label="Reset Server  [R]",
                               callback=reset_server, width=-1)


            # ---- Preview panel ----
            with dpg.child_window(tag="preview_panel", border=False,
                                  width=-1, height=-1):
                dpg.add_image("camera_texture", tag="preview_image",
                              width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT)

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

    dpg.create_viewport(title=WINDOW_TITLE, width=1660, height=780)
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

    texture_data = frame_to_texture(no_camera_canvas())
    _dbg_counter = 0   # local to main — throttles debug save_frame calls

    try:
        while dpg.is_dearpygui_running():
            with state.lock:
                if not state.running:
                    break

            frame_start = time.time()

            cap = holder.get("cap")

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
                        blackout  = state.blackout_camera
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

                    if blackout:
                        canvas[:] = 0

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
                            dbg_cap.record_frame(canvas)
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
                    y0 = max((ph - ih) // 2, 0)
                    dpg.configure_item("preview_image", width=iw, height=ih)
                    dpg.set_item_pos("preview_image", [x0, y0])
            except Exception:
                pass

            update_ui_from_state()

            # Drain UI queue
            while not ui_queue.empty():
                tag, value = ui_queue.get()
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
            results, dbg_imgs = detector.process_frame(raw, ts)
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
                    u = det.cx_px / w_raw
                    v = det.cy_px / h_raw
                    positions[str(det.blink_id)] = {
                        "u": round(u, 4),
                        "v": round(v, 4),
                        "confidence": round(det.confidence, 3),
                    }
                    with state.lock:
                        state.calibrated_positions[det.blink_id] = {"u": u, "v": v}
                    if det.blink_id not in _detected_ids:
                        _detected_ids.add(det.blink_id)
                        elapsed = time.time() - _detection_start_time
                        _log_timing(
                            f"{det.blink_id:>10}  {elapsed:>14.2f}s  "
                            f"{det.confidence:>12.3f}"
                        )
                post_json_async("/admin/positions", {"positions": positions})
        finally:
            _detect_queue.task_done()


def poll_clients():
    while state.running:
        fetch_client_count(state)
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
