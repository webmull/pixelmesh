# (c) Adam Davis - adamdavis.co.uk
"""
PixelMesh V2 — Effects panel

Owns the Effects floating window, per-effect parameter storage,
the trigger_effect() function, and the live effect preview pane.

Call effects.init(state, set_status) once before building the UI,
then effects.build_window() inside setup_ui().
Call effects.register_preview_texture() inside the dpg texture_registry block.
Call effects.start_preview_thread() after setup_ui().
"""

import math
import threading
import time

import cv2
import dearpygui.dearpygui as dpg
import numpy as np

from network import post_json_async
from log import log
import game

# Wired up by init()
_state      = None
_set_status = None
_ui_queue   = None   # main-thread UI dispatch (controller's ui_queue)


def init(state, set_status, ui_queue=None):
    global _state, _set_status, _ui_queue
    _state      = state
    _set_status = set_status
    _ui_queue   = ui_queue


# ------------------------------------------------------------------ #
# Per-effect parameter registry                                        #
# ------------------------------------------------------------------ #

# (param, label, widget, kwargs)
EFFECT_PARAMS = {
    "wave": [
        ("color",        "Colour",    "color",        {"default_value": (255, 255, 255, 255)}),
        ("speed",        "Speed",     "slider_float", {"default_value": 0.4,  "min_value": 0.05, "max_value": 4.0}),
        ("angle",        "Direction", "slider_float", {"default_value": 0.0,  "min_value": 0.0,  "max_value": 360.0, "format": "%.0f°"}),
        ("spatial_freq", "Frequency", "slider_float", {"default_value": 2.0,  "min_value": 0.5,  "max_value": 10.0}),
    ],
    "gradient": [
        ("color",  "Colour",    "color",        {"default_value": (255, 255, 255, 255)}),
        ("speed",  "Speed",     "slider_float", {"default_value": 0.4, "min_value": 0.05, "max_value": 4.0}),
        ("angle",  "Direction", "slider_float", {"default_value": 0.0, "min_value": 0.0,  "max_value": 360.0, "format": "%.0f°"}),
    ],
    "binary_wave": [
        ("color",        "Colour",    "color",        {"default_value": (255, 255, 255, 255)}),
        ("speed",        "Speed",     "slider_float", {"default_value": 2.0,  "min_value": 0.05, "max_value": 4.0}),
        ("angle",        "Direction", "slider_float", {"default_value": 0.0,  "min_value": 0.0,  "max_value": 360.0, "format": "%.0f°"}),
        ("spatial_freq", "Frequency", "slider_float", {"default_value": 0.5,  "min_value": 0.5,  "max_value": 10.0}),
    ],
    "pulse": [
        ("color", "Colour", "color",        {"default_value": (255, 255, 255, 255)}),
        ("bpm",   "BPM",    "slider_float", {"default_value": 100.0, "min_value": 20.0, "max_value": 300.0, "format": "%.0f"}),
    ],
    "rainbow": [
        ("speed",        "Speed",     "slider_float", {"default_value": 0.4, "min_value": 0.05, "max_value": 4.0}),
        ("angle",        "Direction", "slider_float", {"default_value": 0.0, "min_value": 0.0,  "max_value": 360.0, "format": "%.0f°"}),
        ("spatial_freq", "Frequency", "slider_float", {"default_value": 1.5, "min_value": 0.5,  "max_value": 10.0}),
    ],
    "colour_flood": [
        ("color",  "Colour A", "color",        {"default_value": (0,   0, 255, 255)}),
        ("color2", "Colour B", "color",        {"default_value": (255, 0,   0, 255)}),
        ("angle",  "Angle",    "slider_float", {"default_value": 45.0, "min_value": 0.0, "max_value": 360.0, "format": "%.0f°"}),
        ("split",  "Split",    "slider_float", {"default_value": 0.5,  "min_value": 0.0, "max_value": 1.0,   "format": "%.2f"}),
    ],
    "aurora": [
        ("speed", "Speed", "slider_float", {"default_value": 0.4, "min_value": 0.05, "max_value": 4.0}),
    ],
    "ripple": [
        ("color",        "Colour",    "color",        {"default_value": (255, 255, 255, 255)}),
        ("angle",        "Origin",    "slider_float", {"default_value": 0.0,  "min_value": 0.0,  "max_value": 360.0, "format": "%.0f°"}),
        ("speed",        "Speed",     "slider_float", {"default_value": 0.5,  "min_value": 0.05, "max_value": 4.0}),
        ("spatial_freq", "Frequency", "slider_float", {"default_value": 3.0,  "min_value": 0.5,  "max_value": 10.0}),
    ],
    "groups": [
        ("color",        "Colour A", "color",        {"default_value": (255, 40,  40,  255)}),
        ("color2",       "Colour B", "color",        {"default_value": (40,  40,  255, 255)}),
        ("speed",        "Speed",    "slider_float", {"default_value": 0.5,  "min_value": 0.05, "max_value": 4.0}),
        ("spatial_freq", "Columns",  "slider_float", {"default_value": 2.0,  "min_value": 2.0,  "max_value": 16.0, "format": "%.0f"}),
    ],
    "sparkle": [
        ("color",        "Colour A", "color",        {"default_value": (255, 200, 80,  255)}),
        ("color2",       "Colour B", "color",        {"default_value": (80,  180, 255, 255)}),
        ("speed",        "Rate",     "slider_float", {"default_value": 5.0,  "min_value": 0.1,  "max_value": 10.0}),
        ("split",        "Density",  "slider_float", {"default_value": 0.05, "min_value": 0.0,  "max_value": 1.0}),
    ],
    "sections": [
        ("color",        "Colour A", "color",        {"default_value": (255, 40,  40,  255)}),
        ("color2",       "Colour B", "color",        {"default_value": (40,  40,  255, 255)}),
        ("speed",        "Speed",    "slider_float", {"default_value": 0.4,  "min_value": 0.0,  "max_value": 4.0}),
        ("spatial_freq", "Columns",  "slider_float", {"default_value": 2.0,  "min_value": 1.0,  "max_value": 8.0,  "format": "%.0f"}),
        ("bpm",          "Rows",     "slider_float", {"default_value": 2.0,  "min_value": 1.0,  "max_value": 8.0,  "format": "%.0f"}),
    ],
}

EFFECT_LABELS = {
    "wave":         "Wave",
    "gradient":     "Gradient",
    "binary_wave":  "Binary Wave",
    "pulse":        "Pulse",
    "rainbow":      "Rainbow",
    "colour_flood": "Colour Flood",
    "aurora":       "Aurora",
    "ripple":       "Ripple",
    "groups":       "Groups",
    "sparkle":      "Sparkle",
    "sections":     "Sections",
}


# ------------------------------------------------------------------ #
# Param helpers                                                        #
# ------------------------------------------------------------------ #

def _tag(effect: str, param: str) -> str:
    return f"fx_{effect}_{param}"


def _get(effect: str, param: str, default):
    try:
        v = dpg.get_value(_tag(effect, param))
        return v if v is not None else default
    except Exception:
        return default


# ------------------------------------------------------------------ #
# Trigger                                                              #
# ------------------------------------------------------------------ #

def trigger_effect(name: str):
    with _state.lock:
        positions = _state.calibrated_positions.copy()
    if not positions:
        _set_status("No devices detected - effect blocked")
        return
    color  = _get(name, "color",  (255, 255, 255, 255))
    color2 = _get(name, "color2", (255,   0,   0, 255))
    payload = {
        "name":         name,
        "speed":        _get(name, "speed",        0.4),
        "spatial_freq": _get(name, "spatial_freq", 1.5),
        "bpm":          _get(name, "bpm",          100.0),
        "angle":        _get(name, "angle",        0.0),
        "color_r":      int(color[0]),
        "color_g":      int(color[1]),
        "color_b":      int(color[2]),
        "color2_r":     int(color2[0]),
        "color2_g":     int(color2[1]),
        "color2_b":     int(color2[2]),
        "split":        _get(name, "split", 0.5),
    }
    if name == "groups":
        # Sort phones left→right by u, divide into n equal-count groups.
        # Caps n to phone count so every group has at least one phone.
        n = max(2, round(_get(name, "spatial_freq", 6)))
        sorted_bids = sorted(positions, key=lambda bid: positions[bid].get("u", 0))
        total = len(sorted_bids)
        n = min(n, total)   # can't have more groups than phones
        groups = {
            bid: min(n - 1, int(i * n / total))
            for i, bid in enumerate(sorted_bids)
        }
        payload["spatial_freq"] = n   # phones need the effective n for norm = col/n
        payload["groups"] = groups
    log.info(f"[effect] {payload}")
    post_json_async("/admin/effect/fire", payload)
    game.clear_winner_highlight()
    with _state.lock:
        _state.current_effect = name
    _set_status(f"Effect: {name}")


_settings_debounce_timer: threading.Timer | None = None
_settings_debounce_lock = threading.Lock()


def _on_settings_changed(s, v, user_data):
    """Re-fire the active effect after a short debounce (150 ms).

    DPG fires this callback on every drag tick from sliders and colour pickers,
    which would otherwise send 20+ broadcasts per second while the user drags.
    We cancel any pending timer and restart it so the broadcast only fires
    once the user stops moving.

    The actual re-fire is dispatched onto the main thread via the controller's
    ui_queue rather than called directly from this Timer thread — DPG's
    get_value isn't thread-safe and rapid drag events can otherwise deadlock
    against DPG's internal mutexes (cause of the silent freeze on 05 May).
    """
    global _settings_debounce_timer
    with _state.lock:
        current = _state.current_effect
    if not current:
        return

    with _settings_debounce_lock:
        if _settings_debounce_timer is not None:
            _settings_debounce_timer.cancel()

        def _fire():
            # Enqueue rather than call trigger_effect here: this runs on the
            # Timer's background thread and trigger_effect calls dpg.get_value.
            if _ui_queue is not None:
                _ui_queue.put(("_refire_effect", None))

        _settings_debounce_timer = threading.Timer(0.15, _fire)
        _settings_debounce_timer.daemon = True
        _settings_debounce_timer.start()


# ------------------------------------------------------------------ #
# UI                                                                   #
# ------------------------------------------------------------------ #

_MODAL_W = 300


def _build_modal(name: str):
    """Create a hidden settings window for this effect."""
    params = EFFECT_PARAMS.get(name, [])
    modal_tag = f"fx_modal_{name}"
    # Estimate height: ~52px per param + title bar + close button
    h = len(params) * 52 + 80
    with dpg.window(
        tag=modal_tag,
        label=f"{EFFECT_LABELS[name]} - Settings",
        show=False,
        no_collapse=True,
        no_resize=True,
        width=_MODAL_W,
        height=h,
    ):
        _P  = 8
        _BW = _MODAL_W - _P * 2 - 16
        for param, label, widget, kwargs in params:
            t = _tag(name, param)
            dpg.add_text(label, color=(160, 160, 160), indent=_P)
            if widget == "color":
                dpg.add_color_edit(
                    label=f"##{t}", tag=t,
                    no_alpha=True, indent=_P, width=_BW,
                    callback=_on_settings_changed, user_data=name,
                    **kwargs,
                )
            elif widget == "slider_float":
                dpg.add_slider_float(
                    label=f"##{t}", tag=t,
                    indent=_P, width=_BW,
                    callback=_on_settings_changed, user_data=name,
                    **kwargs,
                )
        dpg.add_spacer(height=6)
        dpg.add_button(
            label="Close",
            indent=_P, width=_BW,
            callback=lambda: dpg.configure_item(modal_tag, show=False),
        )


def _open_modal(name: str):
    # Close any other open effect panel first
    for n in EFFECT_LABELS:
        t = f"fx_modal_{n}"
        if n != name and dpg.does_item_exist(t) and dpg.is_item_shown(t):
            dpg.hide_item(t)

    modal_tag = f"fx_modal_{name}"
    if not dpg.does_item_exist(modal_tag):
        return
    vw = dpg.get_viewport_width()
    vh = dpg.get_viewport_height()
    h_est = len(EFFECT_PARAMS.get(name, [])) * 52 + 80
    x = max(0, (vw - _MODAL_W) // 2)
    y = max(0, (vh - h_est) // 2)
    dpg.set_item_pos(modal_tag, [x, y])
    dpg.show_item(modal_tag)


def build_window():
    # Pre-build all modal dialogs (hidden)
    for name in EFFECT_LABELS:
        _build_modal(name)


# ------------------------------------------------------------------ #
# Effect preview pane                                                  #
# ------------------------------------------------------------------ #

PREV_W, PREV_H = 280, 140   # pixels — fits sidebar width

# Fake crowd: 10 cols × 5 rows = 50 evenly-spaced phones
_N_COLS, _N_ROWS = 10, 5
_PREV_US = [c / (_N_COLS - 1) for r in range(_N_ROWS) for c in range(_N_COLS)]
_PREV_VS = [r / (_N_ROWS - 1) for r in range(_N_ROWS) for c in range(_N_COLS)]


def _hash_float(n: int) -> float:
    """Wang integer hash → float in [0, 1). Used to seed per-phone randomness."""
    n = (n ^ 61) ^ (n >> 16)
    n = (n + (n << 3)) & 0x7FFFFFFF
    n =  n ^ (n >> 4)
    n = (n * 0x27D4EB2D) & 0x7FFFFFFF
    n =  n ^ (n >> 15)
    return (n & 0x7FFFFFFF) / 0x7FFFFFFF


def _hsl_to_rgb(h, s, l):
    c = (1 - abs(2*l - 1)) * s
    x = c * (1 - abs((h * 6) % 2 - 1))
    m = l - c / 2
    i = int(h * 6) % 6
    r, g, b = [(c,x,0),(x,c,0),(0,c,x),(0,x,c),(x,0,c),(c,0,x)][i]
    return ((r+m)*255, (g+m)*255, (b+m)*255)


def _shade_preview(effect, u, v, idx, t, params):
    sp    = params.get("speed", 0.4)
    sf    = params.get("spatial_freq", 1.5)
    angle = params.get("angle", 0.0)
    r     = params.get("color_r", 255)
    g     = params.get("color_g", 255)
    b     = params.get("color_b", 255)
    r2    = params.get("color2_r", 255)
    g2    = params.get("color2_g", 0)
    b2    = params.get("color2_b", 0)
    split = params.get("split", 0.5)

    a_rad = math.radians(angle)
    d = (u * math.cos(a_rad) + v * math.sin(a_rad) + 1) / 2

    if effect == "wave":
        i = 0.5 + 0.5 * math.sin(2*math.pi*(d*sf - t*sp))
        return (i*r, i*g, i*b)

    if effect == "gradient":
        i = (d - t*sp) % 1
        return (i*r, i*g, i*b)

    if effect == "binary_wave":
        i = 1 if math.sin(2*math.pi*(d*sf - t*sp)) > 0 else 0
        return (i*r, i*g, i*b)

    if effect == "pulse":
        bpm = params.get("bpm", 100)
        i = max(0, math.sin(2*math.pi*(bpm/60)*t))
        return (i*r, i*g, i*b)

    if effect == "rainbow":
        hue = ((d*sf - t*sp) % 1 + 1) % 1
        return _hsl_to_rgb(hue, 1.0, 0.5)

    if effect == "colour_flood":
        ca, sa = math.cos(a_rad), math.sin(a_rad)
        raw = u*ca + v*sa
        corners = [0, ca, sa, ca+sa]
        dmin, dmax = min(corners), max(corners)
        dn = (raw - dmin) / (dmax - dmin) if dmax > dmin else 0.5
        blend = max(0, min(1, (dn - split) / 0.08 + 0.5))
        return (r + (r2-r)*blend, g + (g2-g)*blend, b + (b2-b)*blend)

    if effect == "aurora":
        phase = u*3.0 + math.sin(v*2.5 + t*sp*0.5)*0.5 - t*sp*0.4
        curtain = (0.5 + 0.5*math.sin(phase*math.pi)) ** 2.5
        hue = (150 + math.sin(phase*0.8 - t*sp*0.15)*60 + 360) % 360
        lum = 0.06 + curtain*0.50
        return _hsl_to_rgb(hue/360, 1.0, lum)

    if effect == "ripple":
        ou = 0.5 + 0.5*math.cos(a_rad)
        ov = 0.5 + 0.5*math.sin(a_rad)
        dist = math.sqrt((u-ou)**2 + (v-ov)**2)
        i = 0.5 + 0.5*math.sin(2*math.pi*(dist*sf - t*sp))
        return (i*r, i*g, i*b)

    if effect == "groups":
        n    = max(2, round(sf))
        col  = min(n - 1, int(u * n))
        norm = ((col / n - t * sp) % 1 + 1) % 1   # 0–1, sweeping
        if norm < 0.5:
            return (r,  g,  b)
        else:
            return (r2, g2, b2)

    if effect == "sparkle":
        # Per-phone random rate + phase seeded from point index; binary flash
        h0 = _hash_float(idx)
        h1 = _hash_float(idx * 7 + 1)
        h2 = _hash_float(idx * 13 + 2)
        flash_rate = 0.5 + h0          # rate varies 0.5×–1.5× base speed
        phase      = h1                # unique phase offset per phone
        density    = params.get("split", 0.5)
        threshold  = 1.0 - 2.0 * density   # density=0 → rarely on, 1 → mostly on
        iv = 1.0 if math.sin(2 * math.pi * (t * sp * flash_rate + phase)) > threshold else 0.0
        if h2 < 0.5:
            return (iv * r,  iv * g,  iv * b)
        else:
            return (iv * r2, iv * g2, iv * b2)

    if effect == "sections":
        # Grid of n_cols × n_rows sections; checkerboard A/B; diagonal sweep wave
        n_cols = max(1, round(sf))
        n_rows = max(1, round(params.get("bpm", 2)))
        col = min(n_cols - 1, int(u * n_cols))
        row = min(n_rows - 1, int(v * n_rows))
        is_a = (col + row) % 2 == 0
        col_frac = col / max(n_cols - 1, 1) if n_cols > 1 else 0.5
        row_frac = row / max(n_rows - 1, 1) if n_rows > 1 else 0.5
        wave = math.sin(2 * math.pi * ((col_frac + row_frac) * 0.5 - t * sp))
        iv = 1.0 if wave > 0 else 0.0
        if is_a:
            return (iv * r,  iv * g,  iv * b)
        else:
            return (iv * r2, iv * g2, iv * b2)

    return (0, 0, 0)


def _render_preview(t: float) -> np.ndarray:
    with _state.lock:
        effect = _state.current_effect

    params = {}
    if effect and effect != "none":
        try:
            for param, *_ in EFFECT_PARAMS.get(effect, []):
                params[param] = _get(effect, param, None)
            color  = params.get("color",  (255, 255, 255, 255)) or (255, 255, 255, 255)
            color2 = params.get("color2", (255,   0,   0, 255)) or (255, 0, 0, 255)
            params["color_r"],  params["color_g"],  params["color_b"]  = int(color[0]),  int(color[1]),  int(color[2])
            params["color2_r"], params["color2_g"], params["color2_b"] = int(color2[0]), int(color2[1]), int(color2[2])
        except Exception:
            pass

    img = np.zeros((PREV_H, PREV_W, 4), dtype=np.float32)
    img[:, :, 3] = 1.0

    dot_r    = 5
    margin_x = dot_r + 4
    margin_y = dot_r + 4

    for i, (u, v) in enumerate(zip(_PREV_US, _PREV_VS)):
        if effect and effect != "none":
            cr, cg, cb = _shade_preview(effect, u, v, i, t, params)
        else:
            cr, cg, cb = 40, 40, 40
        cx = int(margin_x + u * (PREV_W - 2*margin_x))
        cy = int(margin_y + v * (PREV_H - 2*margin_y))
        cv2.circle(img, (cx, cy), dot_r, (cr/255, cg/255, cb/255, 1.0), -1, cv2.LINE_AA)

    return img.flatten()


def _preview_worker():
    start = time.time()
    while _state.running:
        try:
            flat = _render_preview(time.time() - start)
            dpg.set_value("effect_preview_texture", flat)
        except Exception as e:
            log.debug(f"[preview] {e}")
        time.sleep(0.1)   # 10 fps


def register_preview_texture():
    """Call inside the dpg texture_registry block."""
    blank = np.zeros(PREV_H * PREV_W * 4, dtype=np.float32)
    dpg.add_dynamic_texture(PREV_W, PREV_H, blank, tag="effect_preview_texture")


def build_preview_widget(indent: int = 8):
    """Call inside the sidebar after the effects buttons."""
    dpg.add_spacer(height=6)
    dpg.add_text("PREVIEW", color=(160, 160, 160), indent=indent)
    dpg.add_separator()
    dpg.add_spacer(height=4)
    dpg.add_image("effect_preview_texture", width=310, height=150,
                  indent=0)


def start_preview_thread():
    threading.Thread(target=_preview_worker, daemon=True, name="fx-preview").start()


# ------------------------------------------------------------------ #
# TODO: new effects                                                    #
# ------------------------------------------------------------------ #
#
# ripple_wave — concentric rings expanding outward from a centre point.
#   Each ring is a bright band that fades as it travels outward.
#   Params: speed, colour, ring_width, origin_u, origin_v.
#   Client: dist = sqrt((u-ou)²+(v-ov)²); brightness = wave(dist - t*speed).
#
