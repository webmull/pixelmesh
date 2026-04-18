# (c) Adam Davis - adamdavis.co.uk
from dataclasses import dataclass
import threading
import numpy as np

PREVIEW_WIDTH  = 1280
PREVIEW_HEIGHT = 720


class AppState:
    def __init__(self):
        self.lock = threading.RLock()

        self.running          = True
        self.status_text      = "Ready"
        self.current_effect   = "none"

        self.client_count     = 0
        self.last_client_fetch = 0.0

        self.detecting        = False        # detection pipeline active
        self.syncing          = False        # clock sync broadcast active
        self.sidebar_visible  = True
        self.show_device_overlay = True

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
        self.last_x_offset    = 0
        self.last_available_w = PREVIEW_WIDTH
        self.last_available_h = PREVIEW_HEIGHT

        self.latest_frame     = None
        self.display_frame    = None
        self.preview_frame    = np.zeros(
            (PREVIEW_HEIGHT, PREVIEW_WIDTH, 3), dtype=np.uint8
        )

        self.camera_active        = False        # True when a camera is open

        # Calibrated positions: blink_id → {"u": float, "v": float}
        self.calibrated_positions = {}
