# pixelmesh

**Turn a live audience into a pixel display.**

![A dark venue full of phones held up, screens glowing](public/stats/london_opener_hero.gif)

Every phone that opens a URL becomes one pixel of a crowd-sized screen. A single camera pointed
at the audience finds each phone by the pattern it blinks, then effects sweep the room by real
spatial position.

No app install. No QR codes. No GPS, no seat map, no calibration step. The phones find
themselves.

<p align="center">
  <img src="public/stats/decoder_view.gif" width="49%" alt="The decoder reading blinking phones, binary streams beside each one">
  <img src="public/stats/spatial_calibration.gif" width="49%" alt="Phones being located and pinned to positions in the frame">
</p>

---

## How a show runs

**1. Connect.** The audience opens a URL. Each phone is assigned an ID.

**2. Blink.** Every unfound phone flashes a Manchester-encoded ID in white and black, 250 ms
per phase.

**3. Locate.** One camera decodes every blinking screen at once and pins each phone to its
position in the frame. First finds land around 11 seconds.

**4. Render.** Effects sequence across the crowd by position. Waves, ripples, spotlights, plus
games and a post-show report.

---

## Contents

- [How detection works](#how-detection-works) — the interesting part
- [Quick start](#quick-start)
- [The controller](#the-controller)
- [HTTP API](#http-api)
- [Architecture](#architecture)
- [Effects](#effects)
- [Development](#development)
- [Documentation](#documentation)
- [Origins](#origins)
- [Security](#security-and-what-is-deliberately-open)
- [Status and licence](#status-and-licence)

---

## How detection works

The hard problem is not lighting phones up, it is knowing **which phone is where** without
asking anyone to do anything. pixelmesh solves it by making each screen say its own name in
light, and reading the whole room with one camera.

### Signal encoding

Each device blinks one cycle, continuously:

```
[ 4 dark guard phases ] [ Manchester( start + ID + ID + end ) ]
```

| Parameter | Value | Notes |
|-----------|-------|-------|
| `PHASE_MS` | 250 ms | Duration of each screen phase |
| `NUM_BITS` | 8 | Supports IDs 0 to 255 |
| `NUM_GUARD` | 4 | Dark guard phases before the data |
| `CYCLE_LEN` | 40 phases | 10.0 s per full cycle |

- **Manchester coded.** Bit `1` is `[bright, dark]`, bit `0` is `[dark, bright]`. Every bit
  carries a transition, so the receiver stays in step without a shared clock, and the screen
  averages to constant brightness rather than drifting the camera's auto-exposure.
- **The ID is sent twice per cycle**, so a single bit error is recovered by majority vote.
- **Timestamps are ground truth.** Decoding uses actual frame times against the known
  `PHASE_MS`, which makes it immune to variable camera frame rate.
- **The anchor is the end of the guard run**, not the start, so a phone arriving mid-cycle
  still decodes.

### What that costs

**Warmup.** The decoder needs brightness history spanning one full cycle (10.0 s) before it can
attempt anything. Expect 11 to 15 s from connecting to being found.

**Minimum frame rate.** About 12 fps, to sample 250 ms phases at three or more samples each.
The detector is capped at 20 fps, which leaves five samples per phase and returns the rest of
the machine to the display thread.

**Decode budget.** 50 ms per frame, candidates sorted highest-variance first so real phones
outrank noise. It is a fixed budget, so a busier room means each phone waits longer rather than
the system falling over.

The decoder is deliberately pessimistic. Failed points back off exponentially to a 5 s cap.
Phantom IDs within 60 px are deduplicated, though IDs belonging to connected phones are exempt,
because at real crowd density genuine neighbours sit 15 to 50 px apart. Phones that started
blinking before detection began are recovered by a backward scan over pre-guard history, with
confidence penalised for every bit it had to assume.

---

## Quick start

**Requirements**

- **Python 3.14**
- **A wired USB webcam.** Manual exposure matters more than resolution — auto-exposure hunts
  for the blink and flattens it. An Elgato Facecam 4K is auto-selected if present.
- **An HTTPS tunnel** (ngrok or equivalent) if phones join over the internet. Phones need a
  secure context; several browser features the client relies on are HTTPS-only.
- **`tmux`.** `run.sh` wraps itself in a session named `pixelmesh` and will not start without
  it. `brew install tmux`.
- **`hidapi`**, only if you want the Spotlight remote's ISO trim. `brew install hidapi`, which
  the `hid` package in `requirements.txt` binds to. Everything else runs without it.

### Recommended hardware

This is the rig the code is shaped around. Only the first two are needed to run a show. The
rest exist because of a specific thing that went wrong in a room.

**Camera: Elgato Facecam 4K.** Auto-selected when present. Detection reads a phone as a
*change* in brightness, which makes auto-exposure the enemy: it hunts for the flicker and
flattens the thing being measured. This camera is the recommendation despite being awkward
about it. Its firmware runs an internal AE loop and ignores both the OpenCV and the
AVFoundation exposure locks, so the only reliable way to pin exposure is Elgato Camera Hub.
`elgato.py` talks to Camera Hub over its local WebSocket API, re-disables AE every 5 s when
Camera Hub flips it back on by itself, and exposes live ISO. What you get in exchange is
exposure that does not move for the length of a show.

Any wired USB webcam works. Other cameras get `AVCaptureExposureModeLocked` at startup,
re-applied if fps drifts, and no ISO control. Resolution matters far less than holding
exposure still: 1080p that does not drift beats 4K that does.

**Laptop: a Mac.** The controller UI (Dear PyGui), the AVFoundation exposure lock, the HID++
Spotlight integration and the zsh scripts are all macOS-shaped. The server and the phone
client are plain Python and plain web and run anywhere.

**Remote: Logitech Spotlight 2.** ISO is set automatically around the detection toggle, 35
while detecting and 100 at showtime, which leaves the manual override on a slider at the
laptop. That is the wrong place for it. A room changes exposure when it fills up, and by
then you are standing in front of it. The Spotlight puts the trim in your hand: one control
each way, one buzz for up and two for down, so the direction is confirmed without looking at
anything. `tools/presenter_probe.py` records how the HID++ side was worked out.

**Footswitch: BOSS FS-1-WL.** Three switches, hands-free: a fresh detection run, cycle
effects, and hide or show the camera overlays. It is wireless and may wake after the app
starts, so the port scanner retries every 5 s in the background rather than giving up at
boot. The switch messages differ by power-on mode, so mappings are learned once with
`python3 midi.py --learn` and stored in `midi_map.json`, not hardcoded.

**Projector, connected before `run.sh`.** The stage view is a browser page. Plug the display
in first and stop the controller before unplugging it: disconnecting a display while the
controller runs wedges the GUI, because GLFW does not survive macOS display reconfiguration.

### Without the hardware

Most of this runs on any machine with Python. Detection is the part that needs the rig.

| Works anywhere | Needs the hardware |
|----------------|--------------------|
| Server, phone client, effects | Locating phones with a camera |
| The full test suite, no camera or network | The Dear PyGui controller window (macOS) |
| `./sim.sh 12`, a crowd of simulated phones | Elgato exposure lock, MIDI pedal, Spotlight remote |
| | The avatar race, which needs phones the camera has placed |

`./sim.sh 12 --local` spawns twelve real browser instances against a local server, each with
its own profile and therefore its own device id, so the server sees twelve genuinely distinct
phones rather than twelve tabs. Fire effects at them from the controller and you can see most
of the system work without a camera, a venue or an audience.

The macOS-shaped parts are the controller UI (Dear PyGui), the AVFoundation exposure lock,
the HID++ Spotlight integration and the zsh scripts. The server and the phone client are
plain Python and plain web.

```bash
pip install -r requirements.txt --break-system-packages
./run.sh
```

Homebrew's Python is externally managed (PEP 668), so plain `pip install` refuses. There is no
venv here by design, and `--break-system-packages` is what lets it write to the site-packages
the controller actually reads.

The controller will not launch directly — start it through `run.sh`, which wraps itself in a
`tmux` session named `pixelmesh` and reattaches if one already exists.

| Key | Action |
|-----|--------|
| `s` | Start server, tunnel and controller |
| `r` | Reload: kill everything and restart |
| `d` | Die: kill everything |
| `q` | Quit |

Server and tunnel start in parallel; the controller waits on the server's `/health` endpoint,
polling twice a second for up to 10 s before launching anyway.

**Local URLs**

| URL | What it is |
|-----|------------|
| `/admin/show_stats` | Live show state as JSON. Public, no auth |
| `/internal/dashboard` | Admin dashboard |
| `/internal/feed/v1` | Live camera feed. A canvas viewer in browsers, raw MJPEG to curl |
| `/internal/debug` | Debug runs: annotated video and calibration logs |

---

## The controller

The operator window is a Dear PyGui app. The camera feed fills it, overlays are drawn onto
the frame with cv2 rather than as UI widgets, and `Tab` shows or hides a sidebar that floats
over the preview instead of reflowing it. The sidebar has three tabs:

| Tab | Holds |
|-----|-------|
| `SCENE` | Camera Hub exposure and ISO, frame ROI trim, capture and recording, remote and pedal status |
| `RUN` | Detection start and stop, server reset, the effect buttons, end of show |
| `GAME` | The rope climb race and the hearts counter |

There is no mouse-only path through a show. Every control that matters during one is also on
a key, a MIDI pedal or the Spotlight remote, because the operator is usually standing away
from the laptop.

| Key | Action |
|-----|--------|
| `D` | Start or stop detection |
| `S` | Start or stop clock sync |
| `R` | Reset the server: drop every phone, clear positions, fresh run |
| `V` | Start or stop video recording |
| `G` | Debug capture on or off |
| `H` | Hide or show all overlays |
| `O` | Device marker overlay |
| `P` | Cycle overlay mode |
| `F` | Flip the projection |
| `Tab` | Collapse or expand the sidebar |
| `Q` | Quit |

Two hardware inputs sit alongside the keys, both hands-free by design, because the operator
is in a dark room and cannot read a screen. A BOSS FS-1-WL wireless footswitch carries the
three most show-useful actions, and a Logitech Spotlight 2 trims camera ISO from the floor.
Both are optional: `midi.py` and `presenter.py` report as disconnected and everything else
carries on. See [Recommended hardware](#recommended-hardware).

The HUD text is rendered with cv2 onto the camera frame, not by Dear PyGui, which is why
every string reaching it has to be plain ASCII. See
[CONTRIBUTING.md](CONTRIBUTING.md#what-good-looks-like).

---

## HTTP API

Several admin routes are token-free but **local only** — anything arriving through the tunnel
is refused. They exist so a static page can drive a show without holding a token that changes
every launch.

| Route | Method | Purpose |
|-------|--------|---------|
| `/admin/show_stats` | GET | Counts, timings and current effect. Public, safe to poll |
| `/admin/mode` | GET / POST | Detection, recording and overlays on or off. Token required |
| `/admin/overlays` | POST | Camera overlays on or off |
| `/admin/recording` | POST | Start or stop recording |
| `/admin/recording/latest` | GET | The most recent **finished** recording |
| `/admin/end` | POST | End the show and put every phone on its closing card |

`show_stats` is additive by contract — keys are added, never renamed or removed, because other
things poll it:

```json
{
  "like_count": 122,
  "total_connected": 53,
  "detected": 51,
  "connected_now": 48,
  "detecting": false,
  "found_fastest_ms": 10250,
  "found_median_ms": 14800,
  "found_slowest_ms": 47000
}
```

Mode changes are *requested*, not applied. The recorder and detector live in the controller
process, so the server records a request with a sequence number and the controller applies it
on its next poll. That is what stops the two disagreeing about ordering.

---

## Architecture

```
browser clients  ──WS──►  server.py (FastAPI)
                               │
                        controller.py (Dear PyGui)
                         ┌─────┴──────┐
                   display thread  detection thread
                   (60fps camera)  (capped at 20fps)
                         │              │
                    camera.py    blink_detector.py
                    (OpenCV)     (grid sampler + decoder)
                                       │
                                 blink_encoder.py
                                 (Manchester codec)
```

Two processes, not one. `server.py` talks to phones; `controller.py` owns the camera, the
detector and the recorder.

The display and detection threads run independently, passing frames through a
`Queue(maxsize=1)` — if the detector is busy the frame is dropped and the camera loop continues
rather than blocking. The operator feed rides a side channel: a stream thread JPEG-encodes the
newest display frame and pushes it over one persistent WebSocket, with per-viewer stale-frame
dropping so a slow viewer skips ahead instead of building a queue.

The detector samples brightness on a grid every 8 px — roughly 25,000 points at 1080p — dense
enough that a phone 25 to 30 m away still lands inside a sample patch.

---

## Effects

Ten effects, sequenced by real spatial position rather than by index:

`Wave` · `Gradient` · `Pulse` · `Rainbow` · `Sparkle` · `Sections` · `Ring` · `Ripple` ·
`Spotlight` · `Groups`

Each has its own parameters, and changing one re-fires immediately. Seven are driveable from a
foot pedal; three need a mouse because they take a point or a colour per column.

---

## Development

```bash
python3 -m pytest tests/ -q
```

247 tests, no network and no camera required. They cover the Manchester codec round-trip across
frame rates, the decode pipeline, show state, shutdown behaviour and the admin routes.

Two areas are marked **frozen zone** in the roadmap: the blink protocol and the detection
gates. Both are shared truth across `blink_encoder.py`, the client renderer and the server's ID
pool, and all three have to move together. Changes there want measuring, not reasoning about.

---

## Documentation

| Document | What it covers |
|----------|----------------|
| [docs/running-a-show.md](docs/running-a-show.md) | Operator runbook: camera setup, detection, effects, games, post-show report |
| [docs/operations.md](docs/operations.md) | Debug capture, logs, load testing, crowd simulation, tuning knobs |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is next, and what was deliberately not done |
| [docs/TODO.md](docs/TODO.md) | Known gaps |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to propose a change, and the two files that are frozen |
| [SECURITY.md](SECURITY.md) | How to report a vulnerability, and which gaps are already known |

---

## Origins

### Why this exists

A side project, built solo by Adam Davis and tested in front of real audiences rather than
in a lab.

If you have been to an arena show in the last decade you have probably worn a PixMob
wristband: an LED band handed out at the door, driven by infrared from the lighting rig so a
whole stadium can be painted on cue. They work beautifully and they need a supply chain.
Someone has to manufacture them, ship them, hand them out and sweep them up again, for every
show, on every date of a tour.

The question behind pixelmesh is whether you can get there with the device everyone already
brought. A phone is a brighter, higher resolution, individually addressable pixel that also
has a clock, a network connection and a speaker, and nobody has to distribute it. The one
thing it does not have is a **position**. A wristband is located because the lighting desk
knows which block it was given to. A phone in a room is not located at all.

So the whole problem collapses into one question: where is each phone, without asking anyone
to do anything about it. That question is what this repository answers.

### What came before

A crowd's screens as pixels, and where this one picks up:

- **[Junkyard Jumbotron](https://github.com/c4fcm/Junkyard-Jumbotron)**
  (MIT Media Lab, 2011) — mismatched, unmodified screens stitched into one display by a
  server-driven calibration pattern.
- **[PixelPhones](https://seblee.me/2011/09/pixelphones-a-huge-display-made-with-smart-phones/)**
  (Seb Lee-Delisle, 2011) — a crowd's phones held up as one coordinated display.
- **pixelmesh V1** — AprilTags on lock screens with homography calibration. It worked. Printing
  the tags was the friction.
- **pixelmesh V2** (this repo) — the phones find themselves. Each screen blinks its ID and one
  camera reads the whole room.

### Guiding principles

- **Time over position.** Sync clocks first; spatial layout is optional decoration.
- **Detection over configuration.** The system finds you; you don't set anything up.
- **Fast join over precision.** A phone joining five seconds late should still play.
- **Robustness over perfection.** Partial detections, dropped frames and reconnects are the
  norm, not the exception.

---

## Security, and what is deliberately open

Read this before putting the server on anything but loopback.

**Most admin routes need a token.** `run.sh` generates `PIXELMESH_ADMIN_TOKEN` on every
launch and the controller sends it as `X-Admin-Token`.

**Seven do not.** They are listed in `_ADMIN_PUBLIC` and are guarded by
`_is_local_request()` instead: `/admin/show_stats`, `/admin/overlays`, `/admin/end`,
`/admin/recording`, `/admin/recording/latest`, `/admin/game/start` and `/admin/effect/stop`.
The talk deck driving a show is a static HTML file. It cannot hold a token that is regenerated
every launch, so these are exempted and the locality check is the only thing in front of them.

**How locality is decided, and where it breaks.** ngrok forwards the public address to
127.0.0.1, so a tunnelled request also arrives from loopback. The forwarding headers are what
separate the two, and `_is_local_request()` refuses anything carrying them. That is sound for
a tunnel that always sets them. It is **not** sound if you bind the server to a LAN address,
or front it with a reverse proxy that does not set forwarding headers: on that network,
anyone can end your show, start a race, stop your effects, and read
`/internal/feed/v1` and `/admin/recording/latest`, which are the live camera and the recording
of a room full of people.

If you run this anywhere but a laptop at the front of a room, put the whole thing behind
authentication you control.

**Found something.** Report it privately through the
[Security tab](https://github.com/webmull/pixelmesh/security/advisories/new), not a public
issue. [SECURITY.md](SECURITY.md) covers what is worth reporting and what is already a known
and deliberate trade.

**What the audience gives you.** A device id in their own `localStorage`, taps, and a position
in a camera frame. No accounts, no personal data, and the closing card is rendered from what
the phone already knows. The recordings on disk are a different matter: they are video of
identifiable people, they are gitignored for that reason, and they are yours to look after.

---

## Status and licence

A personal project, built for live events and run at real ones. It is shared because the
detection approach is worth reading, not because it is a product: no support, no stability
promise, and opinionated hardware assumptions.

Licensed under the [MIT Licence](LICENSE) — use it, change it, ship it, just keep the
copyright notice.
