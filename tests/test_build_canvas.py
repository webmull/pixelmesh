# (c) Adam Davis - adamdavis.co.uk
"""
build_canvas ran for months without coverage, then a refactor added a 1:1
fast path that defined cx/cy in only one branch - the state writes below the
branch read them unconditionally, so the first camera frame killed the capture
thread with UnboundLocalError and the watchdog restart-looped the controller.
ast.parse and the rest of the suite were green throughout: nothing executed
the function. These do.

Run with:  python -m pytest tests/
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PIXELMESH_LAUNCHED", "1")
os.environ.setdefault("PIXELMESH_ADMIN_TOKEN", "test")

import numpy as np

import controller
from state import PREVIEW_WIDTH, PREVIEW_HEIGHT


def _frame(h, w):
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_one_to_one_path_sets_crop_state():
    """The show configuration: camera == preview, no resize, no crop."""
    canvas = controller.build_canvas(_frame(PREVIEW_HEIGHT, PREVIEW_WIDTH))
    assert canvas.shape == (PREVIEW_HEIGHT, PREVIEW_WIDTH, 3)
    assert controller.state.last_crop_x == 0
    assert controller.state.last_crop_y == 0
    assert controller.state.last_render_scale == 1.0


def test_one_to_one_path_returns_a_copy():
    """The fast path must not alias the camera buffer."""
    f = _frame(PREVIEW_HEIGHT, PREVIEW_WIDTH)
    canvas = controller.build_canvas(f)
    assert canvas is not f
    assert not np.shares_memory(canvas, f)


def test_scaled_path_still_fills_the_preview():
    """A different camera (720p) goes through resize + centre crop."""
    canvas = controller.build_canvas(_frame(720, 1280))
    assert canvas.shape == (PREVIEW_HEIGHT, PREVIEW_WIDTH, 3)


def test_wider_aspect_crops_horizontally():
    """An ultrawide source must crop, and the crop must land in state."""
    canvas = controller.build_canvas(_frame(1080, 2560))
    assert canvas.shape == (PREVIEW_HEIGHT, PREVIEW_WIDTH, 3)
    assert controller.state.last_crop_x > 0


def test_texture_double_buffer_advances_and_alternates():
    """frame_to_texture's two slots must both produce full-size uploads."""
    seq0 = controller._tex_seq
    a = controller.frame_to_texture(_frame(PREVIEW_HEIGHT, PREVIEW_WIDTH))
    b = controller.frame_to_texture(_frame(720, 1280))
    assert controller._tex_seq == seq0 + 2
    assert a.shape == b.shape
