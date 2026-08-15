# (c) Adam Davis - adamdavis.co.uk
from dataclasses import dataclass
import threading
import numpy as np

# The canvas every overlay is drawn onto, what the GUI shows, and what the
# MJPEG feed carries. Native camera resolution: the feed used to be built at
# 1280x720 and then upscaled 1.5x by whatever displayed it, which was a
# visible softening for no benefit the audience could see.
#
# Measured cost of 720 -> 1080 on the capture thread (real crowd frame):
#     build_canvas   1.52 -> 0.37 ms   (cheaper: the resize becomes a no-op)
#     gamma          0.91 -> 1.29 ms
#     contrast       0.51 -> 1.35 ms
#     frame_to_texture 3.08 -> 6.96 ms
#     total          6.02 -> 9.97 ms of a 16.7 ms budget at 60 fps
# JPEG encode, on its own thread, 2.92 -> 5.67 ms and 125 -> 221 KB a frame.
#
# If the display loop ever starts dropping frames, this pair is the first
# thing to put back to 1280x720 — everything else derives from it.
PREVIEW_WIDTH  = 1920
PREVIEW_HEIGHT = 1080


class AppState:
    def __init__(self):
        self.lock = threading.Lock()

        self.running          = True
        self.status_text      = "Ready"
        self.current_effect   = "none"

        self.client_count     = 0
        self.last_client_fetch = 0.0

        self.detecting        = False        # detection pipeline active
        self.syncing          = False        # clock sync broadcast active
        self.sidebar_visible  = True
        self.show_overlays        = True     # master switch — hides all canvas annotations
        self.show_device_overlay  = True
        self.overlay_show_render  = False   # False = show blink IDs, True = show render order (left-to-right)

        self.last_detections  = []           # list of DetectedDevice
        self.last_detection_count = 0

        self.cameras          = []
        self.selected_camera_idx = 0
        self.camera_label_to_index = {}
        self.camera_listbox_items  = []

        self.cam_offset       = (0, 0)
        self.cam_frame_size   = (PREVIEW_WIDTH, PREVIEW_HEIGHT)
        self.last_render_scale = 1.0
        self.last_crop_x      = 0
        self.last_crop_y      = 0

        self.latest_frame     = None
        self.preview_frame    = np.zeros(
            (PREVIEW_HEIGHT, PREVIEW_WIDTH, 3), dtype=np.uint8
        )

        self.camera_active        = False        # True when a camera is open
        self.flip_projection      = True         # mirror the MJPEG feed horizontally
                                                 # (default ON: every venue projects, and a
                                                 # crowd watching itself expects a mirror -
                                                 # F toggles off for desk work)

        # Calibrated positions: blink_id → {"u": float, "v": float}
        self.calibrated_positions = {}
