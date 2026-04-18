"""
PixelMesh V2 — Effects panel

Owns the Effects floating window, per-effect parameter storage,
and the trigger_effect() function.

Call effects.init(state, set_status) once before building the UI,
then effects.build_window() inside setup_ui().
"""

import dearpygui.dearpygui as dpg
from network import post_json_async
from log import log

# Wired up by init()
_state      = None
_set_status = None


def init(state, set_status):
    global _state, _set_status
    _state      = state
    _set_status = set_status


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
        ("speed",        "Speed",     "slider_float", {"default_value": 0.4,  "min_value": 0.05, "max_value": 4.0}),
        ("angle",        "Direction", "slider_float", {"default_value": 0.0,  "min_value": 0.0,  "max_value": 360.0, "format": "%.0f°"}),
        ("spatial_freq", "Frequency", "slider_float", {"default_value": 2.0,  "min_value": 0.5,  "max_value": 10.0}),
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
}

EFFECT_LABELS = {
    "wave":         "1  Wave",
    "gradient":     "2  Gradient",
    "binary_wave":  "3  Binary Wave",
    "pulse":        "4  Pulse",
    "rainbow":      "5  Rainbow",
    "colour_flood": "6  Colour Flood",
    "aurora":       "7  Aurora",
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
    log.info(f"[effect] {payload}")
    post_json_async("/admin/effect/fire", payload)
    with _state.lock:
        _state.current_effect = name
    _set_status(f"Effect: {name}")


def _on_settings_changed(s, v, user_data):
    """Re-fire the active effect immediately when a setting changes."""
    with _state.lock:
        current = _state.current_effect
    if current:
        trigger_effect(current)


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
        label=f"{EFFECT_LABELS[name]} — Settings",
        show=False,
        no_collapse=True,
        no_resize=True,
        width=_MODAL_W,
        height=h,
    ):
        _P = 8
        with dpg.group(indent=_P):
            for param, label, widget, kwargs in params:
                t = _tag(name, param)
                dpg.add_text(label, color=(160, 160, 160))
                if widget == "color":
                    dpg.add_color_edit(
                        label=f"##{t}", tag=t,
                        no_alpha=True, width=-(_P + 1),
                        callback=_on_settings_changed, user_data=name,
                        **kwargs,
                    )
                elif widget == "slider_float":
                    dpg.add_slider_float(
                        label=f"##{t}", tag=t,
                        width=-(_P + 1),
                        callback=_on_settings_changed, user_data=name,
                        **kwargs,
                    )
            dpg.add_spacer(height=6)
            dpg.add_button(
                label="Close",
                width=-(_P + 1),
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
# TODO: new effects                                                    #
# ------------------------------------------------------------------ #
#
# ripple_wave — concentric rings expanding outward from a centre point.
#   Each ring is a bright band that fades as it travels outward.
#   Params: speed, colour, ring_width, origin_u, origin_v.
#   Client: dist = sqrt((u-ou)²+(v-ov)²); brightness = wave(dist - t*speed).
#
# snake — a bright head travels a continuous path across the room,
#   leaving a fading tail.  Path is a row-by-row sweep (u 0→1, then
#   next v row, alternating direction).
#   Params: speed, colour, tail_length.
#   Client: phone lit when head position is within tail_length of (u,v);
#   brightness falls off with distance behind the head.
