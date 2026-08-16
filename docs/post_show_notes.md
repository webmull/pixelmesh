# What a Manchester audience taught a blink detector

It's the evening of 5 May 2026, and somewhere in Manchester a room of strangers is about to become a screen.

The room doesn't know that yet. They've been told to load a URL — `pixelmesh.show` — and they're doing what audiences do, which is glancing at their phones, glancing at each other, glancing at the front of the room where Adam Davis is about to introduce **pixelmesh V2**: a system that will, in the next thirteen seconds, find every one of them.

There's a single camera at the front. An Elgato Facecam 4K, 1920×1080, auto-exposure forced off so the gain doesn't drift mid-show. Behind it, a laptop running a Python controller built on Dear PyGui, watching the camera's raw stream and tiling it with an invisible grid of 8-pixel cells. Each cell carries a tiny patch sampler: a 4×4 window, sample radius 2, sampling the 10th percentile of pixel brightness. Per cell, per frame, a single floating-point number — and from that number, an audience.

When Adam hits **start**, the phones start blinking.

---

## The encoding

Each phone has been handed a `blink_id` between 0 and 511, chosen by the server's `/admin/assign` endpoint when the websocket first opened. The phone's job is to broadcast that ID by turning its own screen white-and-black in a precise temporal pattern, 300 milliseconds per phase, 44 phases per cycle, 13.2 seconds end-to-end. That pattern is **Manchester encoding**: a 1 bit becomes the phase pair `[1,0]`, a 0 becomes `[0,1]`. Every bit has a guaranteed transition, which means even a phone tilted into shadow, even a phone whose absolute brightness is half what it should be, can still be decoded as long as the *change* survives.

There's a trick at the front of every cycle. Manchester normally forbids two consecutive bright phases — that's an illegal symbol. So pixelmesh deliberately broadcasts four of them in a row at the start of the cycle, `[1,1,1,1]`, as a synchronisation preamble. It's a marker that *cannot* occur inside any legitimate data stream, which means the decoder can lock onto cycle boundaries with zero ambiguity.

The detector's job is the inverse. For every active grid cell, it keeps a rolling buffer of the last N brightness samples, computes the temporal standard deviation, and asks: is this cell *flickering* at the right rate? Cells whose `std` clears an adaptive gate — the 90th percentile of all cell stds, smoothed by EMA, multiplied by 3.5, clamped to `[0.05, 0.15]` — get promoted into the `_ever_active` set, a flat integer index into the grid's `_points` array. Active cells get their samples thresholded to binary, run through a Manchester decoder, and matched against the preamble. When four consecutive bright phases line up, the next 18 samples are 9 bits of ID. If those 9 bits resolve to a known `blink_id`, the cell's `(cx, cy)` becomes that phone's position in the room.

This entire pipeline is vectorised. The grid sampler doesn't loop over cells in Python — it slices a NumPy array once and returns 1,200 patch percentiles in a single call. Pre-allocated buffers, decode budget caps, diff-based phone-finder shortcuts. Adam has spent months tuning it down to the millisecond, and there's a `CLAUDE.md` at the root of the repo with a polite but firm instruction: *do not modify these files without permission.*

That's the system that walked into Manchester. Most of it worked.

---

## The phone in the top-left corner

About a minute in, Adam notices something on the overlay. There's a woman seated in the back-left of the room — top-left of the camera frame, between the imaginary boxes at grid cells 19 and 12 — whose phone is *clearly* blinking. White, black, white, black. Visible across the room. And yet the controller is showing nothing for her: no decoded ID, no green dot, no stream of `010110…` characters drifting next to her position. She's blinking into a void.

Post-show analysis tells the story. The detector samples each cell's brightness as a *percentile*, not a mean — specifically, the 3rd percentile of pixels in an 8×8 patch. The intention is to be robust against highlights: a bright wall behind the phone shouldn't be allowed to drown out the phone-vs-no-phone signal. But this woman's phone happened to land near a patch boundary where five out of sixty-four pixels were on the phone screen and the rest were on the dark wall behind her. With `k = int(64 × 0.03) = 1`, the detector was always picking the second-darkest pixel — which, for this patch, was always *off* the phone, even when the screen was bright. The patch's reported brightness barely moved. From the detector's point of view, she wasn't blinking at all.

The fix turned out to be two-fold and only obvious in hindsight.

First: shrink the patch. A `sample_radius` of 2 instead of 4, giving a 4×4 window of 16 pixels instead of 64. With sixteen pixels, `pct=3` collapses to `k=0` — the absolute darkest pixel, which is hyper-fragile. So `pct` had to climb in lockstep, to 10, keeping `k=1` and recovering robustness. A diagnostic tool (`tools/sample_at_phone.py`) sampled the missing phone's location in retrospect and confirmed it: at r=4/p3 the phone produced a brightness `std` of 0.03 — well below any sensible gate. At r=2/p10, the same phone produced 0.35. A factor of ten, just from changing two numbers.

Second: there was a stale-cache bug in the ROI rebuild path. When the operator changes the region of interest mid-session, `_rebuild_grid(h, w)` recomputes `_grid_shape` and rebuilds the `_points` list. But `_ever_active`, `_diff_discovered`, and `_diff_centroids` were all keyed by *integer index into the old _points list*. After a rebuild, those indices either pointed at the wrong cell or at no cell at all, and `draw_overlay` would crash hundreds of times a second with `list index out of range`. The fix: capture each cached entry's `(px, py)` *before* the rebuild, then remap by coordinate after the new `_points` is built. Stale flat-array buffers (`_last_stds`, `_std_buf`) are cleared outright. It's a four-line change that took two hours to find, because the symptom was an entirely different phone failing to decode.

These two bugs hid behind each other. The patch-bias bug looked like an ROI bug. The ROI bug looked like a patch-bias bug. Only after both were fixed did the missing back-row phones come back.

---

## The colour picker that froze the show

The other failure was more theatrical. Mid-show, while Adam was tuning the colour of a Group effect — a piece of the show where a subset of the audience-as-pixels animates in unison — the controller UI simply *froze*. Not crashed. Not errored. Frozen. The camera was still running, the server was still serving, but the Dear PyGui window had stopped responding to anything.

The cause was a threading bug, and a particularly nasty one. The colour picker, when dragged rapidly, was firing a `threading.Timer` callback to debounce repaints. That callback called `dpg.get_value()` to read the current colour. But `dpg.get_value()` is not thread-safe — it's documented to be main-thread-only — and under sufficient drag velocity, the contention silently deadlocks the UI loop. No exception. No traceback. Just a frozen window and an audience watching the operator suddenly look stricken.

The post-show fix was to route every UI read through a main-thread `ui_queue`, and to eliminate `threading.Timer` from the colour-picker path entirely. There's a related fix in the `toggle_detection` worker that bypasses the `_ui_syncing` gate — same class of bug, different surface. Both shipped, both verified.

---

## What Manchester actually was

Manchester wasn't a soft launch. It was a stress test that masqueraded as a demo: a real audience, a real venue, real lighting that had nothing to do with the controlled conditions of Adam's flat. Phones in the back row at oblique angles, against dark walls, under house lights, held loosely in fingers that move imperceptibly. Operator gestures — a colour-picker drag — that aren't part of any test suite because no test suite ever drags a slider for forty seconds straight.

The system came home with both classes of failure mapped, both fixes landed, and one piece of evidence that the architecture was right: every bug found that night was a bug *at the edge* of a working pipeline, not a bug at its core. The detector's vectorised inner loop — Adam's frozen zone, the part with the polite warning — never wavered. The Manchester encoding never lost a frame. The websocket layer kept its grip on every phone in the room.

What broke was the seams. The patch-sampler at the boundary of phone-and-wall. The ROI rebuild at the boundary of two grid layouts. The UI thread at the boundary of Dear PyGui's threading model. Edges, all of them. The kind of thing you only find by walking your code into a room of strangers and watching it try to find them.

The next demo will have those edges sealed. There will be new ones, of course. There always are.
