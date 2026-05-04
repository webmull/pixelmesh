# pixelmesh

Audience device coordination using screen-blink detection. Each phone that opens the web app is assigned a unique ID and located by a camera pointed at the crowd — no QR codes, no GPS, no app install. Once found, every device renders light effects in perfect sync, turning the audience into a pixel display.

Built for live events. Designed for Brighton Dome. Tested with the **Elgato Facecam 4K**.

---

## quick start

**Requirements**

- Python 3.10+
- [ngrok](https://ngrok.com) account with a reserved domain (`pixelmesh.show`)
- A wired USB webcam — the controller auto-selects an Elgato Facecam 4K if present
- [ttyd](https://github.com/tsl0922/ttyd) — installed automatically via Homebrew on first run if not present

```bash
pip install -r requirements.txt
```

**Run**

The controller must be started via `run.sh` — it will not launch directly.

```bash
./run.sh
```

| Key | Action |
|-----|--------|
| `s` | Start server, ngrok, and controller |
| `r` | Reload — kill everything and restart |
| `d` | Die — kill everything |
| `q` | Quit |

Server and ngrok start in parallel. The controller waits up to 10s for the server's `/health` endpoint before launching.

`run.sh` automatically wraps itself in a `tmux` session named `pixelmesh`. If the session already exists (e.g. after detaching), re-running `./run.sh` reattaches to it.

**URLs**

| URL | Description |
|-----|-------------|
| `https://pixelmesh.show` | Audience URL — share this on screen |
| `https://pixelmesh.show/admin/show_stats` | Live show stats JSON — `like_count`, `total_connected`, `detected`. Public, no auth |
| `http://localhost:8000/internal/dashboard` | Admin dashboard |
| `http://localhost:8000/internal/sim` | Browser simulator (fake clients) |
| `http://localhost:8000/internal/feed/v1` | MJPEG camera stream (30fps) |
| `http://localhost:8000/internal/debug` | Debug runs — annotated videos and calibration logs |
| `https://ssh.pixelmesh.live` | Remote terminal — browser-based, password protected |

**Remote terminal**

On startup a second ngrok tunnel exposes a browser-based terminal (via `ttyd`) at a dynamic URL shown in the status line:

```
terminal → https://ssh.pixelmesh.live  (pixel / mesh)
```

Open that URL from anywhere, enter the credentials, and you have full interactive access to the `pixelmesh` tmux session — the same terminal running `run.sh`. Credentials are configured in `ngrok.pixelmesh.yml`.

---

## running a show

### 1. camera setup

The camera **must be on manual exposure** before starting detection.

**Why auto-exposure breaks things:** The blink signal is a screen switching between full-white and full-black at 300ms per phase. Auto-exposure tracks and cancels the blink — the resulting signal has a brightness range of ~0.28 instead of ~0.99, producing `empty_win` failures on every decode attempt.

The Elgato Facecam 4K ignores both OpenCV and AVFoundation exposure locks — the firmware runs its own internal AE loop. **The only reliable fix is Elgato Camera Hub:**

1. Open **Elgato Camera Hub**
2. Disable **Auto Exposure**
3. Set **ISO to 624**
4. Leave shutter at whatever gives stable 60fps in your venue

**Watchdog:** `elgato.py` connects to Camera Hub via its local WebSocket API and monitors AE throughout the session. Camera Hub occasionally re-enables AE on its own — the watchdog forces it back off within 5 seconds. The sidebar shows live status, current AE state, and an ISO slider for live adjustment.

For other cameras: `AVCaptureExposureModeLocked` is applied at startup and re-applied if fps drops below 8fps. `CAP_PROP_AUTO_EXPOSURE=0` and `CAP_PROP_EXPOSURE=-6` are also set as fallback.

**Signal quality:** if signal range drops below 0.5, an amber dot appears on the HUD. Detection still works but takes 25–35s instead of 13–15s. Causes: AE compressing amplitude, low phone brightness, or ambient light sensor dimming screens.

---

### 2. detection

Press **D** (or MIDI pad 8) to start detection. The camera decodes each blinking screen and maps it to a position in the room.

- **Blocked if nobody is connected** — `D` with no clients shows "No clients connected"
- **Partial re-detection** — already-located phones keep their positions; only unfound phones are asked to blink again
- **Auto-stops** when all connected phones are found
- **Positions persist** across detection runs — only cleared by an explicit Reset (`R`)
- **Identity preserved through reconnects** — brief WS drops (iOS background, network blip) preserve blink ID and position; the phone returns to located view immediately on reconnect. Full cleanup only after 90s of no contact
- **Dead socket eviction** — if `update_position` fails on a stale TCP connection, `_drop_connection` fires immediately so the phone reconnects and receives the message on its next `hello`
- **State reset on each run** — the detector's internal `_ever_active` set is cleared at the start of every detection session, preventing fps degradation across multiple runs without an app restart

Expect 15–20s from a phone connecting to first detection at typical range. The HUD shows `camera fps / detection fps` when detection is active.

**ROI (Region of Interest):** The detection grid normally covers the entire camera frame. ROI lets you crop it down — excluding the top, bottom, left, or right edges as a percentage of the frame — so the detector only looks inside the region where the audience actually is.

Why this matters: the blink detector works by finding bright spots that vary rhythmically over time. Anything that changes brightness — a monitor, a moving light, a reflective surface — can activate grid points and consume CPU. Trimming the ROI to the audience area eliminates those false sources before they reach the detector, and typically gives a noticeable fps improvement in venues with active stage lighting.

Use the sliders in the **SCENE** tab. The excluded region is dimmed on the camera feed and a blue boundary line marks where detection begins. Sliders are disabled while detection is active; changes take effect on the next detection run.

---

### 3. effects

Click the sidebar buttons to fire effects. Each effect has its own parameter dialog (`...` button) — changing a value immediately re-fires with the new settings. Effects are blocked until at least one phone has been detected.

| Effect | Parameters |
|--------|------------|
| Wave | Colour, Speed, Direction |
| Gradient | Colour, Speed, Direction |
| Binary Wave | Colour, Speed, Direction |
| Pulse | Colour, BPM |
| Rainbow | Speed, Direction |
| Colour Flood | Colour A, Colour B, Split, Speed, Direction |
| Aurora | Speed |
| Ripple | Colour, Origin angle, Speed, Frequency |
| Snake | Colour, Speed, Tail length |

The active effect is highlighted in orange in the sidebar. An animated thumbnail above the effect list previews the selected effect in real time.

**Overlay modes (P):** toggle between showing blink IDs (0-based) or render order (left-to-right spatial rank) on the camera feed. Render order is what the effects engine uses to sequence phones across the crowd.

---

### 4. bug game

A tap-reaction game. All phones receive a bug at random private intervals within a 20-second round — each player's bug appears at an unpredictable moment. Lowest reaction time wins.

- **Round**: 20 seconds, hard wall-clock timer
- **Slot**: 1.4 seconds to tap once the bug appears
- **Timer bar**: drains over the full 20s, synced to server time — all phones show the same remaining time regardless of when their bug appeared
- **Progress pill**: `X / Y tapped` shown live on every device

**Winner screen outcomes:**

| Result | Display |
|--------|---------|
| Fastest tap | **YOU WIN** in gold |
| Tied fastest | **IT'S A DRAW** in gold |
| Tapped but not fastest | **NOT THIS TIME** with your time |
| Missed the slot | **YOU MISSED IT** in red |

Winner's phone number and time shown below in all cases. The controller leaderboard shows all reaction times ranked fastest-first, with no-tap phones listed below a separator.

Launch from the sidebar: **Start Bug Game**. The leaderboard window opens automatically.

---

### 5. likes

A global like counter on the waiting screen. Tap the thumbs-up to add to it — flying heart animations play locally. Taps are batched server-side at ~3 broadcasts/second so simultaneous taps from 300 people don't flood connections.

**Sidebar controls:** Reset Like Counter · Enable/Disable Likes

---

### 6. post-show report

A plain-text summary is generated automatically every time the server is reset (`R`). The file opens immediately in the default text editor.

```
══════════════════════════════════════════════
   PIXELMESH — SHOW REPORT
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

BUG GAME
  Players:          43
  Tapped:           38  (88%)
  Winner:        Phone 24  —  142ms
  Median react:   387ms
  No tap:            5

══════════════════════════════════════════════
```

Sections are omitted if they didn't happen (e.g. no BUG GAME section if the game was never started). Reports are saved to `debug/reports/` and kept indefinitely.

---

### controller hotkeys

| Key | Action |
|-----|--------|
| `D` | Toggle detection |
| `S` | Toggle clock sync |
| `H` | Toggle all camera overlays (blink streams, device IDs, ROI boundary) |
| `O` | Toggle device ID overlays |
| `P` | Toggle overlay mode (blink IDs / render order) |
| `R` | Reset server |
| `Tab` | Toggle sidebar |
| `G` | Start/stop debug capture |
| `V` | Start/stop video recording |
| `Q` / `Esc` | Quit |

### MIDI (Akai LPD8 mk2)

Connected automatically on startup if present.

**Pad 8** — Toggle detection

| Knob | CC | Action |
|------|----|--------|
| K1 | 70 | ISO gain (0–160) |
| K2 | 71 | Video recording — turn up to start, back to stop |
| K3 | 72 | Device ID overlays — turn up to show |
| K4 | 73 | Clock sync — turn up to enable |
| K8 | 77 | Server reset |

---

## client experience

### screens

Phones cycle through these views as the show progresses:

| View | Shown when | What the audience sees |
|------|-----------|------------------------|
| `idle` | Disconnected / server reset | Black screen |
| `waiting` | Connected, show not started | "Get ready" + like button |
| `blinking` | Detection active, not yet found | White/black blink pattern |
| `located` | Position confirmed | Map showing their spot in the crowd |
| `missed` | Detection ended, not found | 3 red flashes → black |
| `effects` | Showtime | Synchronised light effect |
| `game_wait` | Game active, bug not yet appeared | Black screen |
| `game` | Bug game (countdown / tap / result) | Bug game UI |

Each card is a fixed full-screen div. `setView()` is the only point that changes the display — cards are shown/hidden via `style.display`, never via CSS class toggles.

---

### waiting screen

Displayed when connected and waiting for the show:

- **"Get ready"** headline + brightness/auto-lock reminder
- Pulsing dot with assigned phone number
- Rotating crowd messages — solo messages when alone, crowd-count messages once others join
- **Like button** — tap to add to the global count; flying heart SVGs animate across the screen
- **Wake Lock** requested to prevent the phone sleeping

---

### located screen

Shown once the camera has confirmed the phone's position:

- **"Found you!"** with "Here's your position in the crowd" subtitle
- A position map — white dots for other detected phones, large animated green dot for this phone
- **"You're all set · Hold your screen up when the show begins"**
- **"Brightness to full · Turn off auto-lock"** reminder
- The green dot pulses at 2Hz using a `requestAnimationFrame` loop synced to the visibility API — it restarts automatically if the screen wakes from sleep

---

## internals

### architecture

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

The sidebar is organised into three tabs: **SCENE** (exposure, ROI sliders, live information panel — opens by default), **RUN** (detection, overlays, effects, server controls), and **GAME** (bug game, likes).

The display and detection threads run independently. Frames pass via `Queue(maxsize=1)` — if the detector is busy the frame is dropped and the camera loop continues unblocked.

| File | Role |
|------|------|
| `server.py` | WebSocket server, device assignment, effect broadcast, like counter |
| `controller.py` | Camera loop, GUI, detection thread management, exposure monitor |
| `effects.py` | Effect definitions, per-effect parameter storage, settings dialogs |
| `blink_encoder.py` | Manchester encoding / decoding |
| `blink_detector.py` | Grid sampler, variance gate, per-point decode, thread pool |
| `game.py` | Bug game — server routes, parallel scheduling, controller UI and leaderboard |
| `report.py` | Post-show report generator — writes plain-text summary to `debug/reports/` |
| `video_recorder.py` | Plain video recording via ffmpeg pipe |
| `camera.py` | Gamma, contrast helpers |
| `network.py` | HTTP helpers for controller → server calls |
| `elgato.py` | Camera Hub watchdog — AE monitor, ISO control via local WebSocket API |
| `state.py` | Shared state between threads |
| `log.py` | File logger (`debug/pixelmesh.log`) |
| `debug_capture.py` | Frame capture for offline analysis |
| `public/app.js` | Client-side blink renderer, effect engine, waiting/located/game UI |
| `public/sim.js` | Browser simulator (N fake clients) |

---

### signal encoding

Each device blinks one full cycle continuously:

```
[ 4 dark guard phases ]  [ Manchester(start + ID + ID + end) ]
```

| Parameter | Value | Notes |
|-----------|-------|-------|
| `PHASE_MS` | 300ms | Duration of each screen phase |
| `NUM_BITS` | 9 | Supports IDs 0–511 |
| Cycle length | 13.2s | 44 phases × 300ms |

- Manchester: bit `1` → `[bright, dark]`, bit `0` → `[dark, bright]`
- ID transmitted twice per cycle — up to 1 bit error corrected via majority vote
- Detection uses actual frame timestamps + known `PHASE_MS` as ground truth — immune to variable camera fps
- Anchor computed from the end of the guard run so phones arriving mid-cycle are decoded correctly

**Warmup:** the decoder needs a brightness history spanning at least one full cycle (13.2s) before attempting a decode. Expect 15–20s from connection to first detection.

**Minimum fps:** ~10fps to reliably sample 300ms phases (≥3 samples/phase).

---

### tuning

Key parameters in `blink_detector.py`:

| Parameter | Default | Notes |
|-----------|---------|-------|
| `grid_step` | 8px | Distance between sample points. At step=8 a phone just 3px wide always overlaps a patch. Covers phones at 25–30m at 1080p. |
| `sample_radius` | 4px | Patch radius — 8×8=64px per point. Keeps the sampling matrix at 1.66MB, fitting inside L2/L3 cache on M1. r=6 (3.7MB) spills to RAM and makes `np.partition` 10× slower. |
| `brightness_pct` | 3 | Percentile used when sampling a patch. The ~2.8th percentile catches even a single dark pixel during the dark phase. |
| `min_recent_std` | adaptive | Auto-tuned to `EMA(p90(all stds)) × 3.5`, clamped 0.05–0.15. Asymmetric EMA (α=0.4 up, α=0.05 down) — a brightness spike raises the gate within 2–3 frames. |
| `recent_n` | 18 | Samples in recent window (~1.2s at 15fps). Reduced from 24 — ~25% cheaper `np.std` with no decode impact at typical frame rates. |
| `history_seconds` | 15.0 | Rolling brightness history per point. Reduced from 30s — halves list size and `add_sample` trim cost; well above the 13.2s minimum needed for a full decode cycle. |
| `decode_interval` | 0.2s | Time between decode attempts per point (undiscovered phones only) |
| `roi_top_frac` / `roi_bottom_frac` / `roi_left_frac` / `roi_right_frac` | 0.0 | Fraction of frame to exclude from the detection grid on each edge. Controlled via sidebar sliders. |

**Notable behaviours:**

- **Decode backoff** — failed points retry at `min(interval × 2^failures, 5s)`. Counter resets on success.
- **Stream display gate** — binary stream overlay only shown once a point has been active ≥4s with fewer than 6 consecutive failures.
- **Phantom ID suppression** — two IDs within 60px are deduplicated; lower-confidence one is dropped. Reduced from 120px to allow detection of phones closer together in a dense crowd (≈1.6m exclusion radius at 30m/1080p).
- **Backward-scan decoder** — phones that started blinking before detection began are decoded from pre-guard history. Confidence penalised 5% per assumed bit.
- **Stale-entry eviction** — entries below gate for >13.2s are evicted every 3 seconds (wall-clock). Logged at DEBUG: `[blink] evicted N stale pts from _ever_active (remaining=M)`.
- **Guard-phase extension** — after the main decode loop, points in `_ever_active` whose std has just dropped below gate are retried, recovering phones whose guard phase coincided with their warmup threshold crossing.

---

### performance

**Tested on Apple M1 Pro, 16GB RAM.** Display thread runs at ~60fps; detection thread at ~50fps. HUD shows `camera fps / detection fps` during detection.

The bottleneck at 300+ phones is not compute — it is the 13.2s warmup each phone must complete before its first decode attempt.

Key optimisations:

| Optimisation | Impact |
|---|---|
| `Queue(maxsize=1)` frame drop | Display loop never blocked by detector |
| 50ms wall-clock decode budget | Prevents per-frame overrun regardless of phone count |
| Early-exit decoder at confidence ≥0.95 | ~7× speedup per decode with clean signal |
| No re-decode of found phones | Zero cost per frame once located |
| `sample_radius=4` — 1.66MB matrix | Fits L2/L3 cache; avoids RAM latency |
| `np.std` over circular buffer | Single vectorised call, GIL released |
| Precomputed flat patch indices | One numpy gather per frame, no per-point slicing |
| Gated history recording | `add_sample` only called for active/gate-crossing points |
| Pre-allocated texture buffer | Eliminates 14MB/frame allocation |
| Batched like broadcasts | ~3/s cap prevents O(clients²) WebSocket storms |

---

### auto-reload

The server hashes `app.js` at startup into a `BUILD_ID` injected into every page response. On connect, `server_hello` sends the current `BUILD_ID`. If the client's stored ID differs, it reloads immediately.

- Same code + server restart → same hash, no reload
- New `app.js` + server restart → new hash, all clients reload within seconds

---

### simulator

`/internal/sim` spawns N fake clients in the browser. Simulator cells flash white/black during detection only — they go black in showtime mode so they don't interfere with effect testing.

---

### debug capture

Press **G** to start/stop a debug run. Each run is saved to a friendly-named folder under `debug/` (e.g. `autumn-fox-42`). Runs are kept indefinitely — prune manually if disk space matters.

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

---

### logs

| File | Contents |
|------|----------|
| `/tmp/pixelmesh-server.log` | FastAPI / uvicorn — HTTP, WebSocket, assignments, broadcasts, errors |
| `/tmp/pixelmesh-ngrok.log` | ngrok tunnel — connection status, forwarding address |
| `/tmp/pixelmesh-controller.log` | Controller stdout/stderr — startup errors, Dear PyGui exceptions |
| `debug/pixelmesh.log` | Blink detection diagnostics — gate/std stats, decode failures, effect triggers. Appended across restarts. |
| `debug/calibration_logs/YYYYMMDD_HHMMSS.log` | One file per detection session — time-to-detect and confidence per blink ID |
| `debug/reports/YYYYMMDD_HHMMSS.txt` | Post-show report — generated automatically on every reset |
| `debug/recordings/YYYYMMDD_HHMMSS.mp4` | Video recording (hotkey `V`). Not committed to git. |

---

## origins

The idea of using a crowd's phones as pixels traces back to Seb Lee-Delisle's [PixelPhones](https://seblee.me/2011/09/pixelphones-a-huge-display-made-with-smart-phones/) (2011) — phones held up in an audience, manually positioned to form a coordinated display.

**PixelMesh V1** built on the same idea but located each phone via an AprilTag printed on its lock screen and a homography-based calibration step. It worked, but the printed-tag step was the friction point that limited scale.

**PixelMesh V2** (this repo) replaces AprilTag calibration with screen-blink detection: each phone Manchester-encodes its assigned ID by flashing white/black at 300 ms per phase, and a single camera decodes the position of every phone in the room live — no calibration, no printed tags, no app install.

Reused from V1: the WebSocket protocol, the u-space `[0,1]² ` coordinate system, and the effects engine. Replaced: AprilTags → blink detection.

### guiding principles

The four principles the project keeps coming back to:

- **Time over position** — sync clocks first; spatial layout is optional decoration.
- **Detection over configuration** — the system finds you, you don't set anything up.
- **Fast join over precision** — a phone joining 5 seconds late should still play.
- **Robustness over perfection** — partial detections, dropped frames, and reconnects are the norm, not the exception.

---

## todo

- **Blackout command** — instant all-phones-off for dramatic moments
- **Batch `phone_located` broadcasts** — `/admin/positions` currently broadcasts once per detected phone, so N new phones × M connected clients = N·M sends in one POST (e.g. 30 × 200 ≈ 6 000). Replace with a single `phones_located` message carrying all new `{blink_id: {u,v}}` per batch; needs matching handler in `public/app.js` alongside the existing `phone_located` handler. Defer until after a venue with ≥80 phones — small rooms aren't affected.
