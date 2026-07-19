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

## UI strings are ASCII-only

The Dear PyGui default font has no glyphs beyond ASCII — em dashes, arrows,
ellipses, ≈/≥, etc. render as `?` in the sidebar and status bar. cv2.putText
on the HUD has the same limitation.

Any string that reaches the UI (`set_status`, `_set_status`, `dpg.add_text`,
labels, `_iso_hint`, HUD text) must use plain ASCII: `-` not `—`, `->` not
`→`, `...` not `…`, `~` not `≈`. Log messages and code comments are exempt.
