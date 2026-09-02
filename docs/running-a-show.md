# Running a show

Operator runbook: camera setup, detection, effects, games and the post-show report.
Extracted from the README so that stays about what pixelmesh *is* rather than how one
particular person drives it.

## Running a show

### 1. Camera setup

The camera **must be on manual exposure** before starting detection.

**Why auto-exposure breaks things.** The blink signal is a screen switching between full-white
and full-black at 250 ms per phase. Auto-exposure tracks and cancels the blink. The resulting
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

**ISO trim from the floor.** The Spotlight 2 remote adjusts ISO without going back to the
laptop, which is the only way to tune exposure once the room is full:

| Control | Effect |
|---------|--------|
| Tap the touch panel | ISO up one step (5), one buzz |
| Laser pointer button | ISO down one step, two buzzes |
| Either, at the end of the range or with Camera Hub down | three buzzes, nothing changes |

The buzz count is the feedback: you never need to look at the screen.

Two separate controls rather than one clever gesture, because the panel gives us nothing else
to work with. It does not report which half was pressed (both halves send `0x0050`), and it
reports no press duration (the release arrives immediately however long you hold it), so
neither up/down nor click-and-hold is possible. Deriving direction from click count fights the
hand - clicking repeatedly to step up reads as double-clicks and reverses.
`tools/presenter_probe.py` runs all of those experiments if it ever needs revisiting.

A control only reports over HID++ while it is **diverted**, and pixelmesh diverts both while it
runs - so while the app is up, the panel does not left-click and the pointer button drives ISO
rather than its normal action. Each is restored to exactly the state it was found in on exit;
restoring blindly to "off" silences the panel for everything that runs afterwards, so the prior
state is read before it is changed.

The control ids are `0x0050` (panel) and `0x01b0` (pointer). Identify a control by pressing
only that one and watching `--live` output: a census of "press everything" says which ids
exist but not which button sends which.

Trim is an **offset on whichever baseline the phase uses**, not an absolute gain, so a tune
made mid-show survives the automatic moves above: +15 gives detection 50 and showtime 115.
The sidebar slider stays absolute and rebases the trim, so the two never disagree. `Reset
Server` clears it, since that starts a fresh show.

Setup is `brew install hidapi` plus `pip install hid`; no macOS permission is needed, because
the remote is opened non-exclusively. Without the `hid` module the controller runs exactly as
before and logs one line. The remote drops off Bluetooth when idle - press it to wake it, and
the sidebar's REMOTE row shows the link and battery.

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

Expect 11 to 15 s from a phone connecting to first detection at typical range. The bottom-right
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
  Joined overall:   52  (5 left before the run)

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

`Connected` is the crowd that was actually connected when detection ran, which is the same
population the `[detect] end` line in the calibration log scores against. The server's own
`total_connected` counts every phone that ever joined since it booted, so scoring against that
would count anyone who locked their screen or walked out as missed, and would mark every
detection run after the first as a failure. That figure still appears as `Joined overall`, but
only when it differs, so the drop-off stays visible without distorting the hit rate.

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
