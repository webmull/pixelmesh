# (c) Adam Davis — adamdavis.co.uk
"""
PixelMesh V2 — File logger
All diagnostic output goes to debug/pixelmesh.log instead of stdout.
"""

import logging
import os

DEBUG_DIR = os.path.join(os.path.dirname(__file__), "debug")
os.makedirs(DEBUG_DIR, exist_ok=True)

_handler = logging.FileHandler(
    os.path.join(DEBUG_DIR, "pixelmesh.log"), mode="a", encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

log = logging.getLogger("pixelmesh")
log.setLevel(logging.DEBUG)
log.addHandler(_handler)
log.propagate = False   # don't echo to root logger / stderr
