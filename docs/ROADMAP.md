# Roadmap

Design notes for planned work. Ordered roughly by value. Items marked **frozen zone** touch
`blink_detector.py` / `blink_encoder.py` and need explicit sign-off before implementation.

## Customisable end screens

The outro card is per-show content dressed up as code. Venue, date, the closing
line and the URL all differ between the September talk and MotoCon in October,
and a card that says the wrong venue is worse than one that says nothing - which
is why the venue line came straight back out of the mock rather than being
hardcoded.

Wanted: the end screen driven by config rather than markup. A small block the
operator sets per show (venue, date, headline, footer line, whether to show the
map at all), read by the phone client the same way it reads any other state.
`/admin/mode` already has the shape for pushing show state to clients, so this
is closer to "add fields" than "build a system".

Worth doing at the same time: a preview route, so the card can be checked on a
phone before the room is full. The mock lives at `artifacts/mobiletest/` and was
served over viaduct for exactly that reason; that should be a first-class thing
rather than a scratch directory.

Blocked on nothing. Deliberately not started until the card design settles.

## Faster decode: PHASE_MS 300ms -> 250ms

Cuts the cycle 13.2s -> 11s (-17%) with the full 512-ID space intact - warmup floor, every
decode retry, and the whole timeline shrink together. Preferred over narrowing NUM_BITS,
which was analysed (Jul 2026) and rejected: 7 bits caps at 128 devices (below Brighton
scale), and the sparse 512-ID space is what makes phantom rejection work - random misreads
collide with an assigned ID only ~10% of the time at 50 phones, which the cluster-dedup
valid-ID exemption depends on. Narrowing the space breaks that protection.

- Camera side has huge margin: detection ran 34-140fps at Birmingham; 250ms phases need
  ~12fps for 3 samples/phase.
- The risk is phone-side: browser timer jitter and screen latency eat a fixed number of ms
  per phase, which is a larger fraction of a shorter phase. Validate with the shoulder-tight
  multi-phone test before any show.
- `PHASE_MS` is shared truth between `blink_encoder.py` and the client blink renderer - both
  ends change together. **Frozen zone.**
- Sequencing: only attempt after the cluster-dedup fix is field-validated; the Birmingham
  data says retries from marginal signal dominate the median, not cycle length.

## Found-state visibility during calibration

People holding a phone up cannot see its screen, so they do not know when they have been
found (and flipping to check breaks their own decode). Direction from the Jul 2026
exploration: on found, switch the screen to a steady bright green for the rest of the
detection window - the holder sees the glow change from strobing to steady, and the room
itself becomes the progress bar as it settles green phone by phone. Add a soft chime as
reinforcement (WebAudio, unlocked by the join tap; iOS ringer switch limits coverage).

- Detection safety argument: the variance gate ignores steady light (no std), but verify
  with the shoulder-tight test that steady-green neighbours do not slow the unfound phones
  between them (watch for glare onto adjacent screens).
- Rejected: vibration (iOS Safari has no support), torch control (not exposed to web),
  watch-the-projector-for-your-number (high cognitive load mid-crowd).
- Client + server state change only - no frozen files.

## Sort recording

The two recorders confuse operators: plain video recording (V / pedal switch 3 / checkbox,
manual only, debug/recordings/) vs the debug capture (auto-arms with first detection, records
the whole session, debug/<run>/). Open questions: should the plain recording auto-start with
the show; should the two merge; and the crash-restart behaviour (a watchdog restart leaves the
plain recording stopped while the debug capture re-arms on next detection).

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

## Effects editor in the browser — deferred, too risky for now

A visual editor for effect parameters, served by pixelmesh and opened in a browser, with a
live canvas preview and changes written straight back into the running show. Prototyped as a
standalone artifact (`pixelmesh effect editor`, Aug 2026): 926 lines of self-contained JS, one
canvas, no libraries, and it already speaks this project's vocabulary — the same nine effect
keys (`wave, gradient, pulse, rainbow, sparkle, sections, ripple, spotlight, groups`) and the
same seven parameters (`speed, spatial_freq, bpm, angle, split, color, color2`). Persistence in
the prototype is clipboard-copy only; it is not wired to anything.

Most of the integration already exists:

- `_render_html()` reads a file from `public/` and stamps a token into it, which is exactly the
  pattern needed to serve the page with credentials (`server.py:916`).
- `POST /admin/effect/fire` already accepts precisely this payload (`server.py:723`).
- `effects._param_cache` is a dict of `fx_{effect}_{param}` -> value, so reading current state
  is a one-line endpoint. It exists because `trigger_effect` had to stop touching DearPyGui.
- `safe_set(tag, value)` -> `ui_queue` -> render loop is the thread-safe path for writing values
  back onto the widgets (`controller.py`).

Sketch: drop the HTML in `public/`, add a route (~10 lines), `GET /admin/fx_params` returning the
mirror (~8), `POST /admin/fx_params` calling `safe_set` per key (~15), editor-side fetch-on-load
and post-on-change (~40 lines JS), `webbrowser.open()` from a sidebar button (~5). Call it a
focused half-day, most of it in the artifact rather than here.

**Why it is deferred.** Three unresolved risks, none of them the plumbing:

1. **"Save" has no destination.** Nothing persists effect parameters today — they live in
   DearPyGui widget state and die with the process. `midi_map.json` is the only settings file in
   the repo. Presets/persistence is a new feature, not a wiring job, and it is the piece that
   decides the shape of everything else.
2. **Two implementations of the same maths will drift.** The prototype reimplements the effects
   in JS; the phones render from `effects.py` plus the client. The moment they disagree the
   editor lies, and it will lie most convincingly at the moment it is being trusted.
3. **These are write endpoints**, so they cannot join `_ADMIN_PUBLIC` the way `show_stats` did.
   Token has to be stamped into the page.

Sequencing: not before Brighton. It touches the live parameter path during a period with talks
on 8 Sep and MotoCon in October, and the payoff is operator convenience rather than anything the
audience sees. Revisit once the show calendar is clear, starting with the persistence question.
