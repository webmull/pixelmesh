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
from queue import Queue

import cv2
import dearpygui.dearpygui as dpg
import numpy as np

from state import AppState, PREVIEW_WIDTH, PREVIEW_HEIGHT
from camera import apply_gamma, apply_contrast, apply_sharpen
from blink_detector import BlinkDetector
from debug_capture import DebugCapture
from network import post_json, post_json_async, fetch_client_count
from log import log

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #

WINDOW_TITLE       = "PixelMesh V2"
CAM_WIDTH          = 1920
CAM_HEIGHT         = 1080
TARGET_FPS         = 30
CLIENT_FETCH_SECS  = 2.0
FONT               = cv2.FONT_HERSHEY_SIMPLEX

state    = AppState()
detector = BlinkDetector()
dbg_cap  = DebugCapture()

# Throttle debug saves: one frame every N camera frames
DEBUG_SAVE_EVERY = 6
ui_queue: Queue = Queue()

# Detection timing
_detection_start_time: float = 0.0
_detected_ids: set = set()   # blink_ids seen this detection session
_timing_log_path: str = ""

import os as _os

_CALIBRATION_LOG_DIR = _os.path.join(_os.path.dirname(__file__), "calibration_logs")

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


def open_camera(idx: int) -> cv2.VideoCapture | None:
    cap = cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)
    # Disable autoexposure so the camera doesn't brighten dark phases
    # (autoexposure during the 6-phase dark guard causes false bright frames
    #  that break the run-length, making the guard undetectable)
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)   # 1 = manual on AVFoundation
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    log.info(f"[camera] idx={idx} actual_fps={actual_fps:.1f}")
    return cap


# ------------------------------------------------------------------ #
# Texture conversion
# ------------------------------------------------------------------ #

def frame_to_texture(bgr: np.ndarray) -> np.ndarray:
    rgb  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    rgba = np.dstack((rgb, np.full(rgb.shape[:2], 255, dtype=np.uint8)))
    return rgba.astype(np.float32).flatten() / 255.0


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
        cv2.rectangle(canvas, (px - 6, py - 10), (px + 6, py + 10), (0, 255, 200), 1)
        cv2.circle(canvas, (px, py), 3, (0, 255, 200), -1)
        cv2.putText(canvas, str(blink_id), (px + 10, py - 6),
                    FONT, 0.5, (220, 220, 220), 1, cv2.LINE_AA)


def draw_hud(canvas: np.ndarray, fps: float):
    with state.lock:
        n = state.last_detection_count
        detecting = state.detecting

    color  = (0, 220, 100) if detecting else (120, 120, 120)
    label  = f"DETECTING  FPS:{fps:.0f}  blobs:{n}" if detecting else f"PASSIVE  FPS:{fps:.0f}"
    cv2.putText(canvas, label, (10, 24), FONT, 0.55, color, 1, cv2.LINE_AA)


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
        n_det     = state.last_detection_count
        effect    = state.current_effect

    dbg_label = f"Debug: REC ({dbg_cap.frame_idx} frames)" if dbg_cap.active else "Debug: OFF"

    safe_set("status_text",     status)
    safe_set("clients_text",    f"Clients: {clients}")
    safe_set("detect_text",     f"Detection: {'ON' if detecting else 'OFF'}  |  Sync: {'ON' if state.syncing else 'OFF'}")
    safe_set("det_count_text",  f"Blobs decoded: {n_det}")
    safe_set("effect_text",     f"Effect: {effect}")
    safe_set("debug_text",      dbg_label)


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
        post_json_async("/admin/detect", {"detecting": True})
        _open_timing_log()
        set_status("Detection ON")
    else:
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
    color = dpg.get_value("fx_color")   # [0–255, 0–255, 0–255, 255]
    payload = {
        "name":         name,
        "speed":        dpg.get_value("fx_speed"),
        "spatial_freq": 1.5,
        "bpm":          dpg.get_value("fx_bpm"),
        "angle":        dpg.get_value("fx_angle"),
        "color_r":      int(color[0]),
        "color_g":      int(color[1]),
        "color_b":      int(color[2]),
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
        trigger_effect("sweep_bar")


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
                dpg.add_button(label="Toggle ID Overlays  [O]",
                               callback=toggle_device_overlay, width=-1)
                dpg.add_button(label="Toggle Debug Capture  [G]",
                               callback=toggle_debug, width=-1)
                dpg.add_text("", tag="debug_text")

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
                dpg.add_button(label="5  Sweep Bar",
                               callback=lambda: trigger_effect("sweep_bar"),
                               width=-1)

                dpg.add_spacer(height=4)
                dpg.add_text("Effect Settings", color=(200, 200, 200))
                dpg.add_text("Colour", color=(160, 160, 160))
                dpg.add_color_edit(
                    label="##fx_color_lbl",
                    tag="fx_color",
                    default_value=(255, 255, 255, 255),
                    no_alpha=True,
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
    holder = {"cap": None}

    setup_ui(holder)

    threading.Thread(target=lambda: poll_clients(), daemon=True).start()
    threading.Thread(target=camera_scan_worker, args=(holder,), daemon=True).start()

    delay = 1.0 / TARGET_FPS
    texture_data = frame_to_texture(no_camera_canvas())

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
                    with state.lock:
                        state.latest_frame = raw.copy()

                    frame = apply_gamma(raw)
                    frame = apply_contrast(frame)
                    frame = apply_sharpen(frame)

                    canvas = build_canvas(frame)

                    with state.lock:
                        blackout   = state.blackout_camera
                        detecting  = state.detecting
                        show_ov    = state.show_device_overlay

                    if blackout:
                        canvas[:] = 0

                    if detecting:
                        ts_now = time.time()
                        results, dbg_imgs = detector.process_frame(raw, ts_now)
                        with state.lock:
                            _scale  = state.last_render_scale
                            _crop_x = state.last_crop_x
                            _crop_y = getattr(state, "last_crop_y", 0)
                        detector.draw_overlay(canvas, scale=_scale,
                                              crop_x=_crop_x, crop_y=_crop_y)

                        with state.lock:
                            state.last_detections      = results
                            state.last_detection_count = len(results)
                            frame_counter = getattr(state, "_frame_counter", 0) + 1
                            state._frame_counter = frame_counter

                        # Debug capture
                        if dbg_cap.active:
                            dbg_cap.record_frame(canvas)   # every frame → video
                            if frame_counter % DEBUG_SAVE_EVERY == 0:
                                dbg_cap.save_frame(
                                    raw=raw,
                                    gray=dbg_imgs.gray,
                                    thresh=dbg_imgs.contrast if dbg_imgs.contrast is not None else np.zeros_like(dbg_imgs.gray),
                                    overlay=canvas.copy(),
                                    blobs=detector.get_blobs(),
                                    detections=results,
                                )

                        if results:
                            h_raw, w_raw = raw.shape[:2]
                            positions = {}
                            for det in results:
                                u = det.cx_px / w_raw
                                v = det.cy_px / h_raw
                                positions[str(det.blink_id)] = {
                                    "u": round(u, 4),
                                    "v": round(v, 4),
                                    "confidence": round(det.confidence, 3),
                                }
                                with state.lock:
                                    state.calibrated_positions[det.blink_id] = {
                                        "u": u, "v": v
                                    }
                                if det.blink_id not in _detected_ids:
                                    _detected_ids.add(det.blink_id)
                                    elapsed = time.time() - _detection_start_time
                                    _log_timing(f"{det.blink_id:>10}  {elapsed:>14.2f}s  {det.confidence:>12.3f}")
                            post_json_async("/admin/positions", {"positions": positions})

                    if show_ov:
                        draw_device_overlay(canvas)

                    fps = 1.0 / max(time.time() - frame_start, 1e-4)
                    draw_hud(canvas, fps)

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
                try:
                    dpg.set_value(tag, value)
                except Exception as e:
                    log.info(f"[ui] queue error tag={tag} err={e}")

            dpg.render_dearpygui_frame()

            elapsed = time.time() - frame_start
            if elapsed < delay:
                time.sleep(delay - elapsed)

    finally:
        post_json("/admin/reset", {})
        cap = holder.get("cap")
        if cap:
            cap.release()
        dpg.destroy_context()


def poll_clients():
    while state.running:
        fetch_client_count(state)
        time.sleep(CLIENT_FETCH_SECS)


if __name__ == "__main__":
    main()
