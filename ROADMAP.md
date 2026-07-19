# Roadmap

Design notes for planned work. Ordered roughly by value. Items marked **frozen zone** touch
`blink_detector.py` / `blink_encoder.py` and need explicit sign-off before implementation.

## Blackout command

Instant all-phones-off for dramatic moments.

## Photo-light / flash warning before demo

Venue photographers' strobes and audience camera flashes flood the detector with bright spikes,
which `_ever_active` then tracks as candidate signals (eats decode budget, slows time-to-find).
Add a one-screen pre-show prompt the operator confirms before pressing D ("Photo lights /
flashes will degrade detection — ask the photographer to hold during calibration").
Belt-and-braces: detect a sudden frame-wide brightness spike and surface an amber HUD warning +
log line so the operator knows mid-detection.

## Extend decode-fail back-off cap

The detector already has exponential back-off on failed decodes (`min(interval × 2^failures, 5s)`,
see tuning section in the README), so a phantom retries once per 5 s at steady state, not every
0.2 s. That's already ~96 % of the available win. The remaining gain is small: extend the cap to
e.g. 60 s after 20 consecutive failures, or "permanent suspension" after 100, capped at ~5 min.
Field data: ~6 phantoms × 16 retries (existing 5 s cap) = ~96 wasted decodes / 80 s run, ≪1 % of
detection-thread time. Not a real perf problem; defer until a venue actually shows budget pressure.

- **Must NOT count `warmup` / `low_history` failures** — only structural failures on points with
  full history. Otherwise every fresh point gets back-off during its first 13.2 s.
- **Must clear all back-offs when `_valid_blink_ids` changes** — otherwise a phone connecting
  with a previously-flagged grid point stays suppressed.
- **Frozen zone.** Worst-case failure mode is "real phone takes longer to detect" (recoverable
  via D toggle).

## Spatial coherence check before decode

A real phone covers ~3–15 px and triggers 2–6 adjacent grid points all blinking in sync; an
isolated noise spike (sensor jitter, lone reflection, sub-pixel motion) only triggers one grid
point. Before a candidate enters `_ever_active` (or before its first decode attempt), require at
least one neighbour within ~16 px (2 grid steps) to also have `recent_std` above some fraction
(e.g. 60 %) of the candidate's. Kills lone-pixel noise before it ever consumes decode budget or
shows a stream overlay. Also reduces the "ID streams over clearly non-blinking sources" cosmetic
clutter visible in the 05 May footage.

- **Must skip the check for very strong std (e.g. ≥ 0.30)** — a phone at extreme distance (40 m+)
  or at the very edge of frame might cover only a single grid point but have plenty of signal;
  don't penalise it.
- **Must use only the recent_std field, not full history** — coherence is a current-frame
  property; checking history would be cycle-aligned and expensive.
- One slice + threshold per candidate, ≪ ms cost. **Frozen zone.** Validate against debug
  captures from a quiet venue (where current detector picks up sensor noise) and a busy one
  (false negatives possible).

## Drawn ROI instead of top/bottom/left/right box

Current ROI is four edge-percentages giving a single inclusive rectangle. Real venues have
non-rectangular audience zones (L-shaped seating, audience surrounded by exclusion regions like
windows + ceiling lights + reflective floor). Replace with a free-hand polygon (or stack of
additive/subtractive boxes) the operator draws directly on the camera preview. Internally store
as a binary mask the size of `(CAM_HEIGHT // grid_step, CAM_WIDTH // grid_step)` and gate
grid-point sampling by the mask. UI: click-and-drag to draw, right-click to delete vertices,
modifier-click to subtract regions. Persist the mask to a settings file so each venue's ROI is
recoverable. Major UX win for venues with windows/screens that can't be cropped out by edge-only
ROI; also lets the operator paint *around* the audience rather than guess at percentages.

- Detector accepts a 2D mask in addition to the four `roi_*_frac` values; old fractions become a
  fallback when no mask is set.
- Show drawn ROI on the camera preview as a translucent overlay (excluded zones dimmed, just like
  today) — same draw_roi_overlay pattern but polygon-aware.
- **Frozen zone** (detector mask support) plus non-trivial UI in `controller.py`. Worth the
  effort once a venue's geometry is genuinely incompatible with edge cropping.

## Souvenirs — personal post-show page per phone

Every connected device gets a unique URL after the show with a recap of *their* pixel: when they
joined, which effects they participated in, and a short GIF of just their pixel's colour over the
duration of the show. Massive shareability (audience posts it, organic reach) and a reason to
keep the tab open after the lights come up. Reuses existing infrastructure: `device_id` already
identifies each phone, the broadcast loop already knows what each pixel was rendering at every
frame, and `video_recorder.py` already captures grid state.

Implementation sketch: (a) during the show, record a per-`device_id` colour timeline at ~5 fps to
memory (bounded ring buffer, drop oldest if RAM tight); (b) on show end (or on a sidebar "Freeze
souvenirs" action), persist each timeline to disk keyed by `device_id`; (c) phones reconnect to
`/souvenir/<device_id>` after the show and get a generated GIF + stats page; (d) device_id token
is already in the phone's localStorage, so the URL can be auto-presented on the existing client
without a manual code.

Open questions: retention window (24 h? until next show?), whether to include a panoramic
crowd-cam frame for "where you were sitting" context, GDPR position on storing per-device
timelines (likely fine — no PII, just anonymous colour traces). Doesn't touch the frozen
detection files; lives entirely in `server.py` + a new `souvenir.py` module + a new template.
