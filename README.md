# pixelmesh

**Turn a live audience into a pixel display.**

![A dark venue full of phones held up, screens glowing](public/stats/london_opener_hero.gif)

Every phone that opens a URL becomes one pixel of a crowd-sized screen. A single camera pointed
at the audience finds each phone by the pattern it blinks. No app install, no QR codes, no GPS,
no seat map, no calibration step. Once located, every device renders light effects in perfect
sync: waves sweep the room, ripples spread from a click on the camera preview, and the crowd
becomes the show.

Built for live events and proven at them: 50+ phones located by one camera in real venues.
Headed for Brighton Dome (MotoCon26, October 2026). The public site lives at
[pixelmesh.live](https://pixelmesh.live), in its own
[pixelmesh.website](https://github.com/webmull/pixelmesh.website) repo.

**How a show runs, in four beats:**

1. **Connect.** The audience opens `pixelmesh.show`. Each phone gets an ID and a like button to
   keep it busy.
2. **Blink.** The operator presses `D`. Every unfound phone flashes a Manchester-encoded ID,
   white/black at 300 ms per phase.
3. **Locate.** The camera decodes every blinking screen simultaneously and pins each phone to
   its position in the frame. Typical time to first find: 15 to 20 s.
4. **Render.** Effects sequence across the crowd by real spatial position. Games, likes, and a
   post-show report round out the set.

---

## Contents

- [Quick start](#quick-start)
- [Remote control](#remote-control)
- [Running a show](#running-a-show)
- [What the audience sees](#what-the-audience-sees)
- [How detection works](#how-detection-works)
- [Architecture](#architecture)
- [Tuning and performance](#tuning-and-performance)
- [Operations](#operations)
- [Tests](#tests)
- [The website](#the-website)
- [Origins](#origins)
- [Roadmap](#roadmap)

---

## Quick start

**Requirements**

- Python 3.14 (Homebrew `python@3.14`; `run.sh` pins `python3.14`; deps live in its global
  site-packages, with no venv)
- [ngrok](https://ngrok.com) account with the reserved domain `pixelmesh.show`, set up as a
  cloud endpoint (see [Show URL and offline page](#show-url-and-offline-page))
- A wired USB webcam. The controller auto-selects an Elgato Facecam 4K if present
- Elgato Camera Hub for manual-exposure control; `run.sh` launches and babysits it

```bash
pip install -r requirements.txt --break-system-packages
./run.sh
```

Homebrew's Python is marked externally-managed (PEP 668), so plain `pip install` refuses to
run. There is no venv here by design, so `--break-system-packages` is the flag that lets it
write to the global site-packages the controller actually reads from.

The controller must be started via `run.sh`; it will not launch directly. `run.sh` wraps itself
in a `tmux` session named `pixelmesh`, and re-running it reattaches if the session already
exists.

| Key | Action |
|-----|--------|
| `s` | Start server, ngrok, and controller (plus `caffeinate` for the show's lifetime, a controller crash watchdog, and the Elgato Camera Hub if it isn't running) |
| `r` | Reload: kill everything and restart |
| `d` | Die: kill everything |
| `q` | Quit |

Server and ngrok start in parallel. The controller then waits for the server's `/health`
endpoint, polling twice a second for up to 10 s before launching anyway.

**URLs**

| URL | Description |
|-----|-------------|
| `https://pixelmesh.show` | Audience URL, the one to share on screen. Offline it serves a holding page that doubles as pre-show onboarding and auto-joins when the show starts |
| `https://pixelmesh.live` | Public site, in its own [pixelmesh.website](https://github.com/webmull/pixelmesh.website) repo, deployed by DigitalOcean on every push to its `main` |
| `https://pixelmesh.show/admin/show_stats` | Live show state as JSON. Public, no auth |
| `http://localhost:8000/admin/mode` | Turn detection, recording and overlays on and off remotely. Token required, CORS-enabled |
| `http://localhost:8000/internal/dashboard` | Admin dashboard |
| `http://localhost:8000/internal/feed/v1` | Live camera feed at up to 60 fps. Browsers get a canvas viewer fed binary JPEG frames over WebSocket (newest frame only, cannot lag); the same URL serves raw MJPEG to `<img>` embeds and curl |
| `http://localhost:8000/internal/debug` | Debug runs: annotated videos and calibration logs |

---

## Remote control

### `GET /admin/show_stats`

The only public admin route: read-only, unauthenticated, CORS-open, and free of anything
identifying. Counts and effect names, never a `device_uuid`. It is cheap enough to poll (four
`len()` calls and two globals, no locks), which the talk deck's join slide does every three
seconds.

```json
{
  "like_count":      160,      // taps on the like button, cumulative
  "total_connected": 52,       // phones that have ever joined this run
  "detected":        47,       // phones the camera has placed
  "connected_now":   48,       // phones holding a live socket right now
  "spectators":      1,        // stage page and other non-phone viewers
  "detecting":       false,    // is a detection run in progress
  "effect":          "pulse",  // effect currently playing, or null
  "effect_started":  1786819751289   // ms timestamp of the last fire, or null
}
```

`total_connected` only ever rises; `connected_now` falls when someone locks their screen or
leaves, which is why both exist. `effect_started` exists because the name alone cannot
distinguish "wave fired again" from "wave is still playing", and the talk deck uses it to
re-announce a re-fired effect. Keys are additive: the first three predate the rest and are
consumed elsewhere, so nothing is renamed or removed.

### `POST /admin/mode`

Turns controller-owned things on and off over HTTP, so the show can be driven from something
other than the keyboard or the pedal: a phone on a lectern, a browser tab, a script.

Ships with three modes:

| Mode | Effect |
|------|--------|
| `detection` | Starts or stops a detection run, the same path as hotkey `D` and pedal switch 1 |
| `recording` | Starts or stops a plain video recording, the same path as hotkey `V` |
| `overlays` | Shows or hides the device markers on the feed. Sets **both** `show_overlays` (the `H` master switch) and `show_device_overlay` (`O`) |

`overlays` sets both flags on purpose. `show_overlays` gates every canvas annotation, so either
flag alone can silently veto the other, and firing an effect from the pedal forces the master
off (the "showtime stomp" that cleans the feed). Setting only the device flag after an effect
changes the state and nothing on screen, which is exactly how it first shipped. `actual`
reports `show_overlays and show_device_overlay` for the same reason: it has to mean what the
room can actually see.

```bash
curl -X POST http://localhost:8000/admin/mode \
     -H "X-Admin-Token: $PIXELMESH_ADMIN_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"detection": true}'

# several at once
-d '{"detection": true, "recording": true}'
```

`GET /admin/mode` returns the same shape:

```json
{
  "modes": {
    "detection": {"enabled": true,  "seq": 2, "actual": true},
    "recording": {"enabled": true,  "seq": 1, "actual": false},
    "overlays":  {"enabled": false, "seq": 0, "actual": false}
  },
  "supported": ["detection", "recording", "overlays"]
}
```

**`enabled` is what was asked for; `actual` is what the controller reports is really true.**
They legitimately disagree: a recording requested with no camera attached never starts, and the
operator can flip a mode locally with the pedal without any request. Poll `GET` to confirm a
change landed rather than trusting the `POST` response, whose reply still carries the *old*
`actual` because the controller has not polled yet.

**Requests, not desired state.** Each mode carries a `seq` that only increments, and the
controller applies a mode only when it sees a `seq` it has not applied. A plain desired-state
flag would fight the operator: stop detection with the pedal, and a second later the controller
would read `"detection": true` still sitting there and switch it straight back on. For the same
reason `seq` bumps even when the value is unchanged. Asking for a mode you already requested is
a real instruction, not a no-op.

The controller adopts the current `seq` values on its first poll **without applying them**, so
a request made while it was closed does not fire at launch.

**Auth and CORS.** Unlike `show_stats` this route is *not* public. It can stop detection
mid-show, so it requires `X-Admin-Token`. It *is* CORS-enabled, including a proper `OPTIONS`
preflight, so a browser on another origin can call it. Those are separate things: CORS makes
the browser willing to send the request, the token decides whether it is honoured. The
preflight is answered before the token check because browsers strip credentials from the
`OPTIONS` probe by specification; it reaches no handler and returns no data. A 403 carries the
CORS headers too, so a caller with a bad token sees `403` rather than an opaque browser CORS
error.

Unknown mode names and non-boolean values are rejected with `400`, and an unknown name rejects
the whole request rather than partially applying it, so a typo fails loudly.

To add a mode: add a name to `MODES` in `server.py` and a branch to `_apply_mode` in
`controller.py`.

### `POST /admin/overlays`: token-free, local only

One narrow exception to all of the above, for the talk deck, which turns overlays on when it
reaches the camera slide and cannot hold a token that `run.sh` regenerates every launch.

```bash
curl -X POST http://localhost:8000/admin/overlays \
     -H 'Content-Type: application/json' -d '{"enabled": true}'
```

It drives the same `seq` counter as `/admin/mode`, so the two cannot disagree about ordering.
It is **not** open to the world: `_is_local_request()` serves only loopback clients carrying no
ngrok forwarding headers, and returns `403` otherwise. Loopback alone would prove nothing,
because ngrok forwards `pixelmesh.show` to `127.0.0.1` and tunnelled traffic therefore also
arrives from a local address. The forwarding headers are what separate them.

The handler reads and parses the body itself rather than declaring `payload: dict`, which would
make FastAPI insist on `Content-Type: application/json`. That matters more than it looks: a
JSON content type is not CORS-"simple", so the browser sends a preflight first, and a preflight
from a `file://` page to a local address is what Chrome's Private Network Access rules refuse.
The deck therefore posts as `text/plain` and no preflight happens at all, the same reason its
`show_stats` GET has always worked. Responses also carry
`Access-Control-Allow-Private-Network: true` for any caller that does preflight.

This is a deliberate stopgap and `docs/TODO.md` tracks replacing it. Any page open in a browser
on the show laptop can still poke it, since CORS is `*`. The blast radius is cosmetic: markers
flicker on the feed, and it cannot stop detection or recording.

---

## Running a show

### 1. Camera setup

The camera **must be on manual exposure** before starting detection.

**Why auto-exposure breaks things.** The blink signal is a screen switching between full-white
and full-black at 300 ms per phase. Auto-exposure tracks and cancels the blink. The resulting
signal has a brightness range of ~0.28 instead of ~0.99, producing `empty_win` failures on
every decode attempt.

The Elgato Facecam 4K ignores both OpenCV and AVFoundation exposure locks, because the firmware
runs its own internal AE loop. **The only reliable fix is Elgato Camera Hub:**

1. Open **Elgato Camera Hub**
2. Disable **Auto Exposure**
3. Set **ISO to 624** (gain 53, the controller's default)
4. Leave shutter at whatever gives stable 60 fps in your venue

**Two-phase ISO.** The controller then drives gain automatically around the detection toggle,
because the two phases of a show want opposite things:

| Phase | Gain | Why |
|-------|------|-----|
| Detecting | 35 (`_DETECTION_ISO_GAIN`) | Dark phases need to read near-zero. Sensor noise in the dark phase compresses amplitude, and the variance gate then drops dim or distant phones |
| Showtime | 100 (`_AUDIENCE_ISO_GAIN`) | The feed should read as a bright lit crowd, not a dim wash |

Both are applied for you; the ISO slider still overrides either afterwards.

**Watchdog.** `elgato.py` connects to Camera Hub via its local WebSocket API and monitors AE
throughout the session. Camera Hub occasionally re-enables AE on its own, and the watchdog
forces it back off within 5 seconds. `run.sh` also keeps the Hub itself alive: it launches it
before the stack if absent and relaunches it in the background (~10 s check) if it dies, since
the AE watchdog and ISO control die with it. The sidebar shows live status, current AE state,
and an ISO slider. A manually-set ISO survives Camera Hub reconnects.

For other cameras, `AVCaptureExposureModeLocked` is applied at startup and re-applied if fps
drops below 8. `CAP_PROP_AUTO_EXPOSURE=0` and `CAP_PROP_EXPOSURE=-6` are also set as fallback.

**Signal quality.** If the blink amplitude is compressed, detection still works but takes 25 to
35 s instead of 13 to 15 s. Causes: AE cancelling the blink, low phone brightness, or ambient
light sensors dimming screens. When the exposure monitor spots a fixable cause it surfaces an
amber hint under the sidebar's ISO slider.

**Displays: plug in before starting, don't hot-unplug.** Disconnecting a display (projector
HDMI) while the controller runs wedges the GUI, because GLFW cannot survive the macOS
display-topology change (MaccTech, Jul 2026: frozen app, `r` reload, ~90 s to full recovery
including re-detection). Connect the projector before `run.sh`, and stop the controller before
unplugging. If it happens mid-show, a watchdog in `run.sh` notices a dead controller within ~2 s
and restarts it automatically, bounded at 3 restarts per 60 s and then giving up loudly in
`/tmp/pixelmesh-controller.log` so a crash-loop is visible. `r` remains the manual fallback.

### 2. Detection

Press **D** to start detection. The camera decodes each blinking screen and maps it to a
position in the room.

![Detector's view: binary ID streams overlaid on blinking phone screens](public/stats/decoder_view.gif)

- **Blocked if nobody is connected.** `D` with no clients shows "No clients connected"
- **Partial re-detection.** Already-located phones keep their positions; only unfound phones
  are asked to blink again
- **Auto-stops** when all connected phones are found
- **Positions persist** across detection runs, cleared only by an explicit Reset (`R`)
- **Identity survives reconnects.** WS drops (iOS backgrounding, network blips) keep the
  phone's blink ID and stored position for 30 minutes of silence, and the phone returns to its
  located view immediately on reconnect
- **Dead socket eviction.** If `update_position` fails on a stale TCP connection,
  `_drop_connection` fires immediately so the phone reconnects and receives the message on its
  next `hello`
- **Clean state per run.** The detector's internal candidate set is cleared at the start of
  every detection session, preventing fps degradation across multiple runs without an app
  restart

Expect 15 to 20 s from a phone connecting to first detection at typical range. The bottom-right
HUD shows `camera fps / detection fps` (text goes green while detecting) plus a
`found / connected` counter, amber while chasing and green once everyone is found. Both render
in real Verdana rasterised onto the canvas, so they also appear in recordings and the stream. A
thick green border marks the active detection region: the inner ROI when one is set, the full
frame otherwise.

**ROI (region of interest).** The detection grid normally covers the entire camera frame. ROI
crops it, excluding the top, bottom, left, or right edges as a fraction of the frame, so the
detector only looks where the audience actually is.

This matters because anything that changes brightness (a monitor, a moving light, a reflective
surface) can activate grid points and consume CPU. Trimming the ROI to the audience band
eliminates those false sources before they reach the detector. In venues with active stage
lighting it roughly doubles detection-thread throughput.

Use the sliders in the **SCENE** tab. Left and Right are display-space: they trim the side you
see in the preview and keep meaning that under Projection Flip, because the camera-space config
swaps automatically so slider, dimmed band and label always agree with your eyes. The excluded
region is dimmed on the camera feed, a boundary line marks where detection begins, and the ROI
label sits on a dark backing box. Sliders are disabled while detection is active, and changes
take effect on the next run.

### 3. Effects

Click the sidebar buttons to fire effects. Each effect has its own parameter dialog (`...`
button), and changing a value immediately re-fires with the new settings. Effects are blocked
until at least one phone has been detected. The grid leads with the seven pedal-friendly
effects in the pedal's cycle order; the three that need the mouse (Ripple's click point,
Spotlight's cursor, Groups' column colours) sit at the end.

Listed in sidebar order:

| Effect | Parameters |
|--------|------------|
| Wave | Colour, Speed, Direction, Frequency |
| Gradient | Colour, Speed, Direction |
| Pulse | Colour, BPM |
| Rainbow | Speed, Direction, Frequency |
| Sparkle | Colour A, Colour B, Rate, Density. A soft tinkle bloom that picks colour per cycle |
| Sections | Colour A, Colour B, Speed, Columns, Rows |
| Ring | Colour, Breath, Thickness. An outer ring that eases in to the centre and back out, one breath every 8.3 s by default |
| Ripple | Speed. Click-armed; the controller fires it from your click point on the camera preview |
| Spotlight | Colour, Radius. Follows the operator's cursor across the camera preview, and the tightest radius picks out a single phone |
| Groups | Columns plus a colour swatch per column (up to 16 stripes), Chase speed |

The active effect is highlighted in orange in the sidebar. An animated thumbnail above the
effect list previews the selected effect in real time.

Effects can be designed away from the venue in
[`tools/effect_editor.html`](tools/effect_editor.html), a standalone page that runs the same
shader math against a simulated crowd and generates the `EFFECT_PARAMS` block to paste back
into `effects.py`. Ring was built in it.

**Projection flip (`F`)** mirrors the camera feed and controller preview so the projector reads
the right way round, and the HUD redraws onto the flipped canvas so labels stay readable. It is
**on by default**, because a crowd watching itself expects a mirror. Toggle it off for desk work
(the checkbox lives under CAMERA HUB on the SCENE tab).

**Overlay modes (`P`)** toggle between showing blink IDs (0-based) and render order
(left-to-right spatial rank) on the camera feed. Render order is what the effects engine uses
to sequence phones across the crowd.

### 4. Avatar race

Every detected phone gets a procedurally-generated character (body colour, hat style and skin
tone all hashed from its blink ID) and races left-to-right on the stage projection (`/stage`).
Tap the phone to step forward; first across the finish line wins.

- **Target:** 40 taps to finish (`RACE_TAPS_PER_PLAYER` in `game.py`)
- **Tap rate cap:** ~12 taps/s per phone, beyond which taps are ignored
- **Progress broadcast:** 10 Hz. The stage eases each runner's displayed x toward the latest
  server position so motion stays smooth even on slow networks
- **Lanes auto-fit.** The track divides evenly across however many runners are in the round,
  scaling avatar size down so 70+ phones still fit cleanly
- **Phone-side avatar preview.** The user's own character is painted into the race card header
  alongside their live rank ("12th of 47"), so they can find themselves in a crowded projection
- **Winner overlay.** Text-only, with looping confetti until the operator triggers the next
  thing. A manual stop ends the round silently

Launch from the sidebar with **Start Avatar Race**, and open `/stage` on a second screen or
projector to display it.

### 5. Likes

A global like counter on the waiting screen. Tap the thumbs-up to add to it, and flying heart
animations play locally. Taps are batched server-side at roughly three broadcasts per second so
simultaneous taps from 300 people don't flood connections.

**Sidebar controls:** Reset Like Counter, Enable/Disable Likes

### 6. Post-show report

A plain-text summary is generated automatically every time the server is reset (`R`). The file
is saved silently to `debug/reports/`, and detection-end runs (`D` off) open it in the default
editor.

```
══════════════════════════════════════════════
   pixelmesh - show report
   24 Apr 2026, 20:15
══════════════════════════════════════════════

AUDIENCE
  Connected:        47 phones
  Detected:         43  (91%)
  Missed:            4

DETECTION
  Started:        20:15:04
  Completed:      20:28:31  (13m 27s)
  Median time:    18.4s
  Fastest:        12.1s  — Phone 12  (conf 0.94)
  Slowest:        41.3s  — Phone 7   (conf 0.71)

ENGAGEMENT
  Likes:           284

══════════════════════════════════════════════
```

Sections are omitted if they didn't happen, and reports are kept indefinitely. Combined with
the per-run calibration logs and debug captures, every show leaves a full paper trail:

![Spatial map of a real show: every phone plotted at its detected position, shaded by time-to-decode](public/stats/07_spatial_labelled.png)

### Foot controller (BOSS FS-1-WL)

A wireless three-switch pedal that runs the whole show hands-free:

| Switch | Action |
|---|---|
| 1 | Fresh detection run: reset, then detect. Stomp again to stop |
| 2 | Clears all camera overlays, then steps through the effects (wave, gradient, pulse, rainbow, sparkle, sections, ring) |
| 3 | Toggle video recording |

One-time setup: pair via Audio MIDI Setup, MIDI Studio, Bluetooth, then run
`python3.14 midi.py --learn` and stomp each switch when prompted. The pedal's messages depend
on its power-on mode, so they are learned into `midi_map.json` rather than hardcoded. The
controller scans for the pedal every 5 s, so it can connect or wake at any point in a session.

The **MIDI panel** on the SCENE tab shows the pedal link state ("connected" or "waiting for
pedal") and the last 15 received commands, newest first in gold. Stomps, effect fires, connects
and any unmapped presses all appear, so pedal activity is verifiable at a glance mid-show.
Keyboard `D` keeps plain toggle semantics for partial re-detection workflows.

### Controller hotkeys

| Key | Action |
|-----|--------|
| `D` | Toggle detection |
| `S` | Toggle clock sync |
| `H` | Toggle all camera overlays (blink streams, device IDs, ROI boundary) |
| `O` | Toggle device ID overlays |
| `P` | Toggle overlay mode (blink IDs or render order) |
| `F` | Flip projection (mirror feed and preview) |
| `R` | Reset server |
| `Tab` | Toggle sidebar |
| `G` | Start/stop debug capture |
| `V` | Start/stop video recording |
| `Q` / `Esc` | Quit |

---

## What the audience sees

Phones cycle through these views as the show progresses:

| View | Shown when | What the audience sees |
|------|-----------|------------------------|
| `idle` | Disconnected or server reset | Black screen |
| `waiting` | Connected, show not started | "Get ready" and a like button |
| `blinking` | Detection active, not yet found | White/black blink pattern |
| `located` | Position confirmed | Map showing their spot in the crowd |
| `missed` | Detection ended, not found | 3 red flashes, then black |
| `effects` | Showtime | Synchronised light effect |
| `game` | Avatar race active | Tap-to-run card with personal avatar and live rank |

Each card is a fixed full-screen div. `setView()` is the only point that changes the display,
and cards are shown or hidden via `style.display`, never via CSS class toggles.

**Waiting screen.** Shares the holding page's design language (wordmark, grid background,
breathing glow) so the audience sees one continuous brand from pre-show to found. It carries a
numbered three-step card (keep the page open, brightness to full and auto-lock off, how to hold
the phone), the like button (tap for a white screen-blink flash, a burst of scattering pixel
squares and a flying thumb, with the count popping on every update including other people's
likes), and a Wake Lock request to keep the phone awake.

**Connection status bar.** Fixed chrome above the home indicator, shown on the waiting and
located views only: a black band fading out at the sides with a pixel-square marker. A steady
pixel with an occasional double-blink wink means connected, continuous hard blinking means
reconnecting, and a triple-blink means just joined. It shows "Connecting..." from the instant a
fresh page loads, so startup can never look like a blank screen.

**Connection resilience.** A fresh page that cannot connect reloads to the holding page after
15 s; a mid-wait disconnect shows the amber state and hands over after 8 s; and a liveness
watchdog on wall-clock time catches the states no socket event reaches (constructor hangs, iOS
freezing the page in background), with grace periods so a mid-handshake socket is never
reloaded.

**Located screen.** "Found you!" with a position map: white dots for other detected phones, a
large animated green dot for this phone, and the pre-show reminders ("Hold your screen up when
the show begins · Brightness to full · Turn off auto-lock"). The green dot pulses at 2 Hz on a
`requestAnimationFrame` loop synced to the visibility API, so it restarts automatically when
the screen wakes from sleep.

---

## How detection works

### Signal encoding

Each device blinks one full cycle continuously:

```
[ 4 dark guard phases ]  [ Manchester(start + ID + ID + end) ]
```

| Parameter | Value | Notes |
|-----------|-------|-------|
| `PHASE_MS` | 300 ms | Duration of each screen phase |
| `NUM_BITS` | 9 | Supports IDs 0 to 511 |
| `NUM_GUARD` | 4 | Dark guard phases before the Manchester data |
| `CYCLE_LEN` | 44 phases | 13.2 s per full cycle |

- Manchester: bit `1` is `[bright, dark]`, bit `0` is `[dark, bright]`
- The ID is transmitted twice per cycle, so up to 1 bit error is corrected via majority vote
- Decoding uses actual frame timestamps plus the known `PHASE_MS` as ground truth, which makes
  it immune to variable camera fps
- The anchor is computed from the end of the guard run, so phones arriving mid-cycle still
  decode correctly

**Warmup.** The decoder needs a brightness history spanning at least one full cycle (13.2 s)
before attempting a decode. Expect 15 to 20 s from connection to first detection.

**Minimum fps.** About 10 fps, to reliably sample 300 ms phases at three or more samples per
phase.

### Decode pipeline behaviours

- **Decode backoff.** Failed points retry at `min(decode_interval × 2^failures, 5 s)`, so 0.2 s
  doubles up to a 5 s cap. The counter resets on success.
- **Stream display gate.** The binary stream overlay is only shown once a point has held at
  least 1.5 s of history with fewer than 6 consecutive failures.
- **Phantom ID suppression.** Two IDs within 60 px are deduplicated and the lower-confidence
  one is dropped. Reduced from 120 px to allow phones closer together in a dense crowd (about a
  1.6 m exclusion radius at 30 m and 1080p). IDs belonging to connected phones (`valid_ids`)
  are exempt: at meetup density real neighbours sit 15 to 50 px apart in frame, and the 16 Jul
  demo showed a decoded, still-blinking phone (conf 0.87) silently discarded for a whole run
  because a found neighbour 40 px away outranked it. Only unassigned phantom IDs are dropped
  now, and each exempted keep is logged once (`[blink] kept valid ID=...`).
- **Backward-scan decoder.** Phones that started blinking before detection began are decoded
  from pre-guard history, with confidence penalised 5% per assumed bit and a further 30% per
  copy error.
- **Stale-entry eviction.** Entries below gate for more than 13.2 s are evicted every 3 seconds
  of wall-clock time. Logged at DEBUG as
  `[blink] evicted N stale pts from _ever_active (remaining=M)`.
- **Guard-phase extension.** After the main decode loop, points whose std has just dropped
  below gate are retried, recovering phones whose dark guard phase coincided with their warmup
  threshold crossing.

---

## Architecture

```
browser clients  ──WS──►  server.py (FastAPI)
                                │
                         controller.py (Dear PyGui)
                          ┌─────┴──────┐
                    display thread  detection thread
                    (60fps camera)  (background, ~50fps)
                          │              │
                     camera.py    blink_detector.py
                     (OpenCV)     (grid sampler + decoder)
                                        │
                                  blink_encoder.py
                                  (Manchester codec)
```

The display and detection threads run independently. Frames pass via `Queue(maxsize=1)`, so if
the detector is busy the frame is dropped and the camera loop continues unblocked.

The operator feed rides a side channel: a stream thread JPEG-encodes the newest display frame
and pushes it over one persistent WebSocket to the server (with per-frame HTTP POST as an
automatic fallback), and the server fans frames out to feed viewers over WebSocket with
per-viewer stale-frame dropping, so a slow viewer skips to the newest frame instead of building
a queue.

**The GUI wears the brand.** The camera preview fills the whole window and the sidebar floats
over it on a semi-transparent scrim. `Tab` hides and shows the sidebar outright; the preview
never moves.

- **Chrome.** Black (#020204, the site's background), the pixelmesh wordmark as the sidebar
  header, bold Verdana section headings with no separator lines.
- **Type.** Verdana at an effective 16 px throughout, 26 px for tab labels.
- **Dock icon.** The cube, set via AppKit at runtime, because GLFW ignores viewport icons on
  Cocoa.
- **HUD and ROI text.** Rasterised onto the canvas with PIL in the same Verdana. DPG overlay
  layers (viewport drawlists, autosized floating windows) do not render reliably on the macOS
  Metal backend, so anything that must always be visible stays on the canvas.

The sidebar is three tabs:

| Tab | Contains |
|-----|----------|
| **SCENE** | Camera hub (AE, ISO, projection flip), capture, frame ROI, the MIDI panel. Opens by default |
| **RUN** | Detection, overlays, effects, server controls |
| **GAME** | Avatar race, likes |

| File | Role |
|------|------|
| `server.py` | WebSocket server, device assignment, effect broadcast, like counter, camera feed relay |
| `controller.py` | Camera loop, GUI, detection thread management, exposure monitor |
| `effects.py` | Effect definitions, per-effect parameter storage, settings dialogs |
| `blink_encoder.py` | Manchester encoding and decoding |
| `blink_detector.py` | Grid sampler, variance gate, per-point decode, thread pool |
| `game.py` | Avatar race: server routes, tap handling, controller UI |
| `midi.py` | Foot controller: pedal discovery, learned key map, effect cycling |
| `report.py` | Post-show report generator, writing a plain-text summary to `debug/reports/` |
| `video_recorder.py` | Plain video recording via ffmpeg pipe |
| `camera.py` | Gamma and contrast helpers, plus the rounded ID badge shared by the overlay and the detector |
| `network.py` | HTTP helpers and the feed WebSocket client for controller-to-server calls |
| `elgato.py` | Camera Hub watchdog: AE monitor, ISO control via local WebSocket API |
| `state.py` | Shared state between threads |
| `log.py` | File logger (`debug/pixelmesh.log`) |
| `debug_capture.py` | Frame capture for offline analysis |
| `dashboard.html` | Admin dashboard page served at `/internal/dashboard` |
| `public/app.js` | Client-side blink renderer, effect engine, waiting/located/game UI |
| `public/stage.js` | Stage projection: avatar race rendering, confetti, winner overlay |
| `ngrok.pixelmesh.yml` | Show tunnel, binding the internal endpoint behind the cloud endpoint |
| `ngrok.cloud-policy.yml` | Traffic policy for `pixelmesh.show`, including the offline holding page |
| `content/` | Posts, talk slides, and other written material |
| `tools/` | Offline work: the effect editor, detector replay, signal heatmaps, GIF/still generators |
| `docs/` | Notes kept alongside the code |
| `artifacts/` | Local working files: sample clips and screenshots. Untracked |
| `docs/testplan.md` | Field test checklist, including the must-pass list before Brighton |
| `docs/ROADMAP.md` | Design notes for planned work |
| `docs/TODO.md` | Running task list |
| `docs/post_show_notes.md` | What happened at each show and what to change next time |

---

## Tuning and performance

**Two resolutions, on purpose.** The canvas every overlay is drawn onto, and therefore what the
MJPEG feed carries, is native `1920x1080` (`PREVIEW_WIDTH/HEIGHT` in `state.py`). The
operator's preview texture inside the controller window is `1280x720`
(`TEXTURE_WIDTH/HEIGHT` in `controller.py`). The audience sees native; the panel one person is
looking at does not need to be.

That split exists because DearPyGui uploads the preview as float32 RGBA on every render frame:
33 MB at native against 15 MB at 720p, or 2.0 GB/s versus 0.9 at 60 render fps. The symptom was
counter-intuitive, in that **fps fell when the window was made smaller**, because a smaller
window rasterises faster, so the render loop spins faster, so it pushes more of those textures
per second and starves the capture thread. Maximising slowed the render loop down and handed
the bandwidth back.

Capture-thread budget per frame at native, measured on a real crowd frame:

| Stage | ms |
|-------|-----|
| `build_canvas` | 0.37 |
| Gamma and contrast | 2.64 |
| Spotlight glow | 0.05 |
| `frame_to_texture` (including downscale) | 3.26 |
| **Total** | **6.32** of 16.7 available at 60 fps |

Before the spotlight and texture fixes that total was 10.24 ms. If it ever needs to come down
further, putting `PREVIEW_WIDTH/HEIGHT` back to `1280, 720` gives up native feed quality and is
the one-line revert.

Key parameters in `blink_detector.py`. The last two are overridden at startup in
`controller.py`, so the effective value is listed:

| Parameter | Value | Notes |
|-----------|-------|-------|
| `grid_step` | 8 px | Distance between sample points. At step 8 the farthest any pixel can sit from the nearest grid centre is about 5.7 px, so a phone just 3 px wide always overlaps a patch. Covers phones at 25 to 30 m at 1080p |
| `sample_radius` | 2 px | Patch radius, a 4×4 = 16 px patch. Reduced from 4 after the 05 May post-show analysis: the 8×8 patch frequently spanned phone plus dark background for back-row or partly-occluded phones, dragging the percentile to background-dark in *both* phases so std collapsed below the gate floor. At one missed phone (raw pixel std 0.37) the r=4 patch read 0.03 and the r=2 patch read 0.35 |
| `brightness_pct` | 10 | Percentile picked from the patch, paired with `sample_radius` so `k = int(flat_size × pct/100) = 1`, always the second-darkest pixel. At r=4 (64 px) p3 also gave k=1, but at r=2 (16 px) p10 is needed; p3 there gives k=0, which reads the absolute darkest pixel and is hypersensitive to sub-pixel noise |
| `min_recent_std` | adaptive | Starts at 0.10, then auto-tuned to `EMA(p90(all stds)) × 3.5`, clamped to 0.05 and 0.15. Asymmetric EMA (α=0.4 up, α=0.05 down), so a brightness spike raises the gate within 2 to 3 frames |
| `recent_n` | 18 | Samples in the recent window, about 1.2 s at 15 fps. Reduced from the 24 default for a ~25% cheaper `np.std` with no decode impact at typical frame rates |
| `history_seconds` | 15.0 | Rolling brightness history per point. Reduced from the 30 s default, halving list size and `add_sample` trim cost while staying well above the 13.2 s minimum for a full decode cycle |
| `decode_interval` | 0.2 s | Time between decode attempts per point, for undiscovered phones only |
| `roi_*_frac` | 0.0 | Fraction of the frame excluded from the detection grid on each edge. Controlled via the sidebar sliders |

**Tested on Apple M1 Pro, 16 GB RAM.** The display thread runs at ~60 fps and the detection
thread at ~50 fps. The bottleneck at 300+ phones is not compute. It is the 13.2 s warmup each
phone must complete before its first decode attempt.

| Optimisation | Impact |
|---|---|
| `Queue(maxsize=1)` frame drop | Display loop never blocked by detector |
| 50 ms wall-clock decode budget | Prevents per-frame overrun regardless of phone count |
| Early-exit decoder at confidence ≥ 0.95 | About 7× speedup per decode with a clean signal |
| No re-decode of found phones | Zero cost per frame once located |
| `sample_radius=2`, a 0.4 MB matrix | 25920×16 samples, more L2/L3-friendly than r=4 |
| `np.std` over a circular buffer | Single vectorised call, GIL released |
| Precomputed flat patch indices | One numpy gather per frame, no per-point slicing |
| Gated history recording | `add_sample` only called for active or gate-crossing points |
| Pre-allocated texture buffer | Eliminates a 14 MB/frame allocation |
| Batched like broadcasts | A ~3/s cap prevents O(clients²) WebSocket storms |
| WS feed with per-viewer stale-drop | One socket per hop, no per-frame HTTP; slow viewers skip to the newest frame instead of queueing |
| Cached HUD text rasterisation | Verdana glyphs render once per distinct label, so steady state is a tiny numpy blend |

---

## Operations

### Debug capture

Press **G** to start or stop a debug run. Each run is saved to a friendly-named folder under
`debug/` (for example `autumn-fox-42`). Runs are kept indefinitely, so prune manually if disk
space matters.

**A debug run does not stop when detection stops.** It stays up until you press `G` again or
quit the app, and `run.mp4` takes every frame regardless of whether detection is running. Only
the per-frame JPEGs under `frames/` are gated on it. At 1920×1080 that is roughly
**1.1 GB/hour**, plus a continuous 1080p ffmpeg encode competing with the show on the same
machine. It used to auto-start with detection (`_DEBUG_AUTO_ON_DETECT`), which meant one
overnight session quietly wrote 2.6 GB across 7 hours with detection off the whole time. It is
off by default now, so start a run deliberately when it is worth capturing.

```
debug/autumn-fox-42/
  run.mp4             ← annotated camera view (H.264, real-time encoded)
  calibration.log     ← time-to-detect and confidence per blink ID
  summary.json        ← per-frame detection summary
  frames/
    0000_raw.jpg      ← downscaled camera frame
    0000_gray.jpg     ← grayscale used for detection
    0000_contrast.jpg ← per-point variance heatmap
    0000_overlay.jpg  ← annotated overlay (throttled)
    0000.json         ← grid point brightness data
```

### Logs

| File | Contents |
|------|----------|
| `/tmp/pixelmesh-server.log` | FastAPI and uvicorn: HTTP, WebSocket, assignments, broadcasts, errors |
| `/tmp/pixelmesh-ngrok.log` | ngrok tunnel: connection status, forwarding address |
| `/tmp/pixelmesh-controller.log` | Controller stdout and stderr: startup errors, Dear PyGui exceptions |
| `debug/pixelmesh.log` | Blink detection diagnostics: gate and std stats, decode failures, effect triggers. Appended across restarts |
| `debug/calibration_logs/YYYYMMDD_HHMMSS.log` | One file per detection session: time-to-detect and confidence per blink ID |
| `debug/reports/YYYYMMDD_HHMMSS.txt` | Post-show report, generated automatically on every reset |
| `debug/recordings/YYYYMMDD_HHMMSS.mp4` | Video recording (hotkey `V`). Not committed to git |

### Show URL and offline page

`pixelmesh.show` is an always-on **cloud endpoint** at ngrok's edge, not a direct tunnel. The
agent (started by `run.sh` from `ngrok.pixelmesh.yml`) binds the internal endpoint
`https://pixelmesh-agent.internal`, and the cloud endpoint's traffic policy forwards to it when
the agent is up. When it isn't, the edge serves a holding page: wordmark, "The show has not yet
begun", and a 10 s countdown that quietly `fetch`-probes the origin, reloading only when the
response stops carrying the `x-pixelmesh-holding` marker header, meaning the show is actually
up. No reload flash while waiting, no laptop involvement while offline, and no
`ERR_NGROK_3200`.

The policy, including the embedded holding-page HTML, is versioned at `ngrok.cloud-policy.yml`.
The dashboard serves whatever was pasted last, so re-paste after editing the file (dashboard,
Universal Gateway, Endpoints, `pixelmesh.show`, Traffic Policy).

**How the pieces fit:**

| Piece | Where | Role |
|---|---|---|
| Reserved domain `pixelmesh.show` | ngrok dashboard, Domains | DNS for the show URL (CNAME at the registrar per ngrok's instructions) |
| Cloud endpoint on `pixelmesh.show` | Dashboard, Endpoints | Always-on edge listener that runs the traffic policy |
| Traffic policy | Pasted from `ngrok.cloud-policy.yml` | `forward-internal` to the agent; holding page when the forward fails **or** returns a 5xx (agent up, app closed) |
| Internal endpoint `pixelmesh-agent.internal` | Claimed by the agent at start | Private rendezvous between edge and laptop, not publicly reachable |
| Agent authtoken | `~/Library/Application Support/ngrok/ngrok.yml` | Default agent config; `run.sh` passes it alongside the project config |
| Tunnel definition | `ngrok.pixelmesh.yml` | Binds the internal endpoint to `localhost:8000`, inspection off, compression on |

**One-time setup on a new ngrok account:**

1. Reserve `pixelmesh.show` under **Universal Gateway, Domains** and point the registrar's DNS
   at ngrok per the instructions shown.
2. **Endpoints, New, Cloud Endpoint**, and bind `https://pixelmesh.show`.
3. Paste the contents of `ngrok.cloud-policy.yml` into the endpoint's Traffic Policy and save.
4. Put the account authtoken in the default agent config (`ngrok config add-authtoken ...`).
   There is nothing to configure for the internal endpoint, since the agent claims it on start.

**Region.** The domain's "Region and IP resolution" is pinned to **Europe** in the dashboard
(Jul 2026). It defaults to global latency-aware DNS, but `pixelmesh.show` reaches ngrok via a
Namecheap ALIAS record, and ALIAS flattening resolves from Namecheap's US servers. "Global"
therefore answered with US PoPs and every audience message crossed the Atlantic twice (RTT p50
273 ms at 500 clients, 55 ms once pinned to EU). If the domain setup ever changes, re-verify
with `python3 tools/load_test.py --host pixelmesh.show --wss`.

**Verifying the three states** (from `run.sh`, where `s` starts everything):

- Nothing running, and `pixelmesh.show` shows the holding page served at the edge.
- Full stack running, and it shows the audience app.
- Tunnel up but server stopped, and it shows the holding page again via the 5xx catch.

### Load testing

`tools/load_test.py` simulates a crowd against a running server with the real protocol per
client (hello, assign, heartbeats, sync-ping RTT probes) plus a mid-hold like storm, and
reports percentiles with pass/fail verdicts. Locally: `python3 tools/load_test.py --clients 500`.
Through the real edge, add `--host pixelmesh.show --wss`. Baselines (Jul 2026, M1 Pro):
500/500 clients, zero drops, RTT p50 4 ms local and 55 ms through the EU edge, server at 12%
CPU.

### Performance sentinel

The display loop self-monitors frame pacing and logs a `[perf]` warning only when the average
exceeds 45 ms over a 5 s window, where healthy is 24 to 40 ms. That is the signature that
caught the July `save_frame` stall. Silence means pacing is fine.

### Client auto-reload

The server hashes `app.js` at startup into a `BUILD_ID`, stamped into both the page's script
tag and `server_hello`. The client compares the server's build against the one embedded in its
own page, rather than localStorage history, which used to reload already-current pages once per
deploy. A fresh page therefore always matches and connects once, while a stale parked page
reloads exactly once.

- Same code plus a server restart gives the same hash, so no reload
- New `app.js` plus a server restart gives a new hash, and parked clients reload within seconds

---

## Tests

```
python3 -m pytest tests/ -q
```

Eight files, no network and no fixtures beyond a `conftest.py`:

| File | Covers |
|------|--------|
| `test_blink_encoder.py` | Encode/decode round-trips and structural invariants, with a simulated camera |
| `test_server_pool.py` | Blink ID pool management and the `blink_assignments` / `blink_reverse` pair |
| `test_show_stats.py` | The `/admin/show_stats` payload: shape, counts, show state, and that the keys the talk deck reads still exist |
| `test_mode_api.py` | `/admin/mode`: sequence semantics, rejection of bad input, request vs actual, and the token/CORS/preflight behaviour |
| `test_end_show.py` | `POST /admin/end`: that one call both stops effects and sends the closing card, in an order that cannot leave a phone dark-then-lit |
| `test_effect_gate.py` | Why an effect button could silently do nothing: phones dropping out of `/admin/blink_map` drained the controller's copy of the room, and `trigger_effect` gated on it |
| `test_camera_pick.py` | Camera selection: that only the Elgato is ever opened, including when it is absent, so no other camera is woken by probing |
| `test_shutdown.py` | Recordings survive a quit: real ffmpeg round-trips verified with `ffprobe`, `stop()` under a live capture thread, the SIGTERM handler in a real subprocess, and unique filenames |

`test_shutdown.py` needs `ffmpeg` and `ffprobe`, and those tests skip without them. It also
asserts two invariants against the *source* rather than by running it, because `controller.py`
cannot be imported in a test (it needs DearPyGui, a display and `PIXELMESH_LAUNCHED`) and both
failures are silent: a signal handler that takes `state.lock` deadlocks instead of erroring,
and a `kill -9` in `run.sh` truncates a recording without any sign until you try to play it.

`conftest.py` sets `PIXELMESH_ADMIN_TOKEN` for the session. Without it every test that imports
`server` dies with `SystemExit`, because the module refuses to load unauthenticated, which is
correct in production and fatal in a test runner.

Handlers are tested by calling them directly rather than over HTTP: they take plain dicts and
return plain dicts, so an HTTP client would exercise nothing extra, and it keeps `httpx` out of
the dependency list. The exception is the admin middleware, which is driven through the real
ASGI stack in `test_mode_api.py`, because the token check, the preflight and the CORS headers
only exist at that layer and there is nowhere else to test them.

---

## The website

[pixelmesh.live](https://pixelmesh.live) lives in its own repo,
**[pixelmesh.website](https://github.com/webmull/pixelmesh.website)** (checked out alongside
this one at `~/Desktop/projects/pixelmesh.website`). It used to sit in `site/` here; the split
happened on 11 Aug 2026 and the site's history came across with it.

- Static site on DigitalOcean App Platform, served from that repo's root. **Every push to its
  `main` deploys it.**
- The hero loop, detection clip, poster, and `og-image.jpg` are cut from real show footage with
  ffmpeg. Sources are the debug captures under `debug/` here and the London opener edit.
- Brand rule: **pixelmesh is always lowercase**, and no em dashes in site copy.

---

## Origins

Three generations of one idea, a crowd's phones as pixels:

- **[PixelPhones](https://seblee.me/2011/09/pixelphones-a-huge-display-made-with-smart-phones/)**
  (Seb Lee-Delisle, 2011): phones held up and positioned by hand.
- **pixelmesh V1:** AprilTags on lock screens, homography calibration. It worked; printing the
  tags was the friction.
- **pixelmesh V2** (this repo): the phones find themselves. Each screen blinks its ID and one
  camera reads the whole room. No tags, no calibration, no install.

### Guiding principles

- **Time over position.** Sync clocks first; spatial layout is optional decoration.
- **Detection over configuration.** The system finds you, you don't set anything up.
- **Fast join over precision.** A phone joining 5 seconds late should still play.
- **Robustness over perfection.** Partial detections, dropped frames, and reconnects are the
  norm, not the exception.

---

## Roadmap

Full design notes in [docs/ROADMAP.md](docs/ROADMAP.md). Headlines:

- **Faster decode.** `PHASE_MS` 300 to 250 ms cuts every timeline 17% with the ID space intact
- **Found-state visibility.** Steady green on found, so raised phones show their status from
  behind
- **Blackout command.** Instant all-phones-off for dramatic moments
- **Photo-light warning.** Pre-show prompt and HUD alert when strobes degrade detection
- **Spatial coherence pre-filter.** Kill lone-pixel noise before it reaches the decode budget
- **Drawn ROI.** Free-hand polygon regions instead of edge percentages
- **Souvenirs.** A personal post-show page per phone: their pixel's story, as a shareable GIF
