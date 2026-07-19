---
marp: true
paginate: true
theme: default
class: lead
---

<!-- _class: lead -->

# **PixelMesh**

### Turn an audience into a pixel display

#### using **one camera** and **no app install**

<!--
Speaker note: PixelMesh is the audience-coordination system I've been
building. The job: every phone in the room becomes a synchronised pixel
in a giant low-res display. The whole thing runs from a single Facecam
pointed at the crowd.
-->

---

## The problem

- **300 phones**, dark venue, one camera, ~30 m away.
- Find every phone, assign it a unique ID, know where it is.
- **< 20 seconds** from the audience opening the URL to the show starting.

> No QR codes. No printed tags. No GPS. No app install.

<!--
Everyone solves this with computer vision and printed AprilTags. That's
how V1 worked and the printing step is what limited scale.
-->

---

## The trick: phones blink their own ID

- Each phone Manchester-encodes its **9-bit ID** by flashing white/black.
- **300 ms per phase** → 13.2 s for one full transmission.
- Starts with **4 dark "guard" phases** — a known signature the decoder locks onto.
- ID transmitted **twice per cycle**; majority vote corrects a single bit error.

#### Why Manchester?

- **Self-clocking:** every bit has a transition.
- **DC-balanced:** mean brightness constant per bit, so the camera's AGC can't bias different phones differently.

---

## The detection insight

> Don't find phones first. Sample a **fixed grid** and let the phones reveal themselves.

- Grid of **32,400 sample points** across the 1080p frame (every 8 px).
- Watch each point's brightness over time.
- Adaptive variance gate picks the candidates (`EMA(p90) × 3.5`).
- **Decode in the time domain, not the frame domain** — fps fluctuates, but the phones blink on real wall-clock time.

#### Result

A bog-standard CV problem becomes 2D signal processing.

---

## Live numbers

| | |
|---|---|
| Detection-pipeline code | **~1,300 lines Python** |
| Phones tested concurrently | **300+** |
| First detect (typical) | **15–20 s** |
| Minimum warmup | **13.2 s** (one full cycle) |
| Range | **25–30 m** at 1080p |
| Cycle | **44 phases** = 4 guard + 40 data |
| ID space | **9-bit**, 0–511 |
| Min camera fps | **10 fps** (≥3 samples per 300 ms phase) |
| Threading | display + detection decoupled via `Queue(maxsize=1)` |

---

<!-- _class: lead -->

## **The punchline**

> Turned a computer vision problem
> into a **signal processing problem** on a 2D grid.

> 1,300 lines of Python.
> Everything else is plumbing.

<!--
End on this. The slide is the takeaway: choose the right problem
representation and the implementation collapses.
-->

---

## Failure modes (if asked)

- **Auto-exposure kills the signal.** The camera tracks the blink and cancels it out — manual exposure is mandatory. We watchdog Camera Hub to keep AE off.
- **Photographer flashes** raise the variance floor and burn decode budget.
- **Aggressive ambient-light dimming** (iPhone low-power, deep sleep) makes phones take longer.

#### What we don't worry about

- Per-phone calibration: there isn't any.
- Camera fps stability: time-domain decoding doesn't care.
- Phone joining late: guard-anchor decoding handles mid-cycle arrivals.
