# PixelMesh V2 — Claude Instructions

## Detection code is frozen

**Never modify the following files without Adam's explicit permission:**

- `blink_detector.py`
- `blink_encoder.py`

These files contain a heavily optimised detection pipeline (vectorised patch sampling,
pre-allocated buffers, _ever_active gating, decode budget, diff-based phone finder).
Every line has been deliberately tuned. Unsolicited changes — even "improvements",
refactors, bug fixes, or formatting — are not allowed.

If a task requires touching these files, stop and ask first.
