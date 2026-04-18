# pixelmesh

Audience device coordination using screen-blink detection. Each connected device is assigned a unique ID and identified by the camera via a Manchester-encoded blinking pattern — no QR codes, no GPS, no app install required.

Built for live events. Designed for Brighton Dome.

---

## how it works

1. A device opens `https://join.pixelmesh.live` in their browser
2. The server assigns it a unique **blink ID** (0–511)
3. The device's screen blinks a Manchester-encoded pattern at 300ms per phase
4. A camera pointed at the audience captures the screens
5. The controller decodes each blinking screen and maps it to a position (u, v) in the room
6. Once detected, devices switch to **showtime mode** and render effects in sync

---

## camera setup (Elgato Facecam 4K — do this before every run)

The camera **must** be set to manual exposure before starting the controller.

**Why auto-exposure breaks detection:** The blink signal is a screen switching between full-white and full-black at 300ms per phase. Auto-exposure sees a bright frame, reduces gain; sees a dark frame, increases gain — it tracks and cancels the blink. The resulting signal has a brightness range of ~0.28 instead of ~0.99. The decoder sees a near-flat signal and produces `empty_win` failures on every decode attempt. Detection stops working entirely.

1. Open **Elgato Camera Hub**
2. Disable **Auto Exposure**
3. Set **ISO to 624**
4. Leave shutter speed at whatever gives a stable 60fps in your venue lighting

The controller also connects to Camera Hub automatically via its local API and monitors auto-exposure throughout the session. If Camera Hub re-enables AE (which it occasionally does), the controller forces it off within 5 seconds. The **Camera Hub** section in the sidebar shows connection status, the current AE state, and an ISO slider for live adjustment without switching apps.

If signal range drops below 0.5, an amber dot appears on the HUD next to the fps counter. Causes: auto-exposure compressing amplitude, low phone screen brightness, or the ambient light sensor dimming the screen. Detection still works but takes longer — expect 25–35s instead of 13–15s.

---

## requirements

- Python 3.10+
- [ngrok](https://ngrok.com) account with a reserved domain (`join.pixelmesh.live`)
- A wired webcam (USB-C recommended — built-in/Continuity Camera works but degrades signal quality). The controller auto-selects an Elgato Facecam 4K if present; use `K` to cycle cameras manually.

```bash
pip install -r requirements.txt
```

---

## running

```bash
./run.sh
```

Interactive menu:

| Key | Action |
|-----|--------|
| `s` | Start server, ngrok, and controller |
| `r` | Reload — kill everything and restart |
| `d` | Die — kill everything |
| `q` | Quit |

Logs:

| File | Contents |
|------|----------|
| `/tmp/pixelmesh-server.log` | FastAPI / uvicorn output — HTTP requests, WebSocket connects/disconnects, device assignments, effect broadcasts, errors |
| `/tmp/pixelmesh-ngrok.log` | ngrok tunnel output — connection status, forwarding address, request logs |
| `/tmp/pixelmesh-controller.log` | Controller stdout/stderr — startup errors, Dear PyGui exceptions |
| `debug/pixelmesh.log` | Blink detection diagnostics — camera open parameters, exposure lock status, per-frame gate/std stats, top active grid points, decode failures, effect triggers, UI errors. Appended across restarts. |
| `debug/calibration_logs/YYYYMMDD_HHMMSS.log` | One file per detection session. Records time-to-detect and confidence for each blink ID found. Also written to the active debug run folder if debug capture is on. |
| `debug/recordings/YYYYMMDD_HHMMSS.mp4` | Annotated camera view, started/stopped with `V`. Not committed to git. |

---

## URLs

| URL | Description |
|-----|-------------|
| `https://join.pixelmesh.live` | Client app — share this with the audience |
| `http://localhost:8000/internal/dashboard` | Admin dashboard — camera stream preview, links to sim and client |
| `http://localhost:8000/internal/sim` | Simulator — fake clients for testing (local only) |
| `http://localhost:8000/internal/feed/v1` | MJPEG camera stream (30fps, direct) |

---

## controller hotkeys

| Key | Action |
|-----|--------|
| `D` | Toggle detection on/off |
| `S` | Toggle clock sync |
| `1`–`7` | Trigger effects (wave, gradient, binary wave, pulse, rainbow, colour flood, aurora) |
| `R` | Reset server |
| `Tab` | Toggle sidebar |
| `G` | Start/stop debug capture (run saved as e.g. `debug/autumn-fox-42/`, last 15 kept) |
| `V` | Start/stop video recording (saved to `debug/recordings/`) |
| `O` | Toggle device ID overlays |
| `Q` / `Esc` | Quit |

---

## device states

| State | Screen | Trigger |
|-------|--------|---------|
| **App closed / disconnected** | Black | Server shut down or connection lost |
| **Connected, waiting** | Solid indigo | Connected but detection not yet started |
| **Detection active** | White/black blink | Controller started detection |
| **Located** | Solid orange | Controller detected this device |
| **Detection ended — not found** | 3 red flashes → black | Detection stopped, device was not found |
| **Showtime — calibrated** | Effect (wave, pulse, etc.) | Effect broadcast from controller |
| **Showtime — not calibrated** | Black | Effect fired but this device has never been located |
| **Update pending** | Green flash × 5s → reload | New version of app.js deployed |

Orange and yellow clear when detection restarts or an effect fires. Not-found (red flash) transitions to black and stays until the next detection cycle. Green flash is skipped if the phone is currently orange — it reloads silently instead. Green flash only triggers when `app.js` has actually changed since the phone last loaded — a server restart with no code changes produces the same hash and no reload.

---

## effects

Effects are launched from the controller sidebar (keys 1–7). Each effect stores its own parameters — the `…` button opens a settings dialog for that effect only. Changing a parameter immediately re-fires the effect with the new value.

| Key | Effect | Description |
|-----|--------|-------------|
| `1` | Wave | Sine wave travelling across the audience |
| `2` | Gradient | Scrolling brightness gradient |
| `3` | Binary Wave | Hard on/off wave |
| `4` | Pulse | Whole audience pulses to BPM |
| `5` | Rainbow | Full spectrum hue sweep across the audience |
| `6` | Colour Flood | Two colours flooding in from opposite sides |
| `7` | Aurora | Teal-purple curtain bands drifting across the room |

| Effect | Parameters |
|--------|-----------|
| Wave | Colour A, Speed, Direction |
| Gradient | Colour A, Speed, Direction |
| Binary Wave | Colour A, Speed, Direction |
| Pulse | Colour A, BPM |
| Rainbow | Speed, Direction |
| Colour Flood | Colour A, Colour B, Split, Speed, Direction |
| Aurora | Speed |

---

## simulator

`/internal/sim` spawns N fake clients in the browser. Simulator cells only flash white/black during detection mode — they go black in showtime or when detection ends, so they don't interfere with effect testing.

---

## auto-reload

The server hashes `app.js` at startup into a `BUILD_ID` and sends it to every client on connect. If the ID differs from what the phone loaded with, it flashes green for 5 seconds then reloads.

- If the phone is currently **orange** (detected), it reloads silently — no green flash
- A server restart with no code changes produces the same hash — no reload triggered
- `app.html` is excluded from the hash because `run.sh` modifies it with a cache-bust token on every start

---

## debug capture

Press **G** to start/stop a debug run. Each run creates a friendly-named folder under `debug/` (e.g. `autumn-fox-42`). The name is shown bottom-right on the camera feed while active. Old runs are pruned automatically — only the last 15 are kept.

```
debug/autumn-fox-42/
  overlay.mp4         ← full-speed H.264 video of the annotated camera view
  calibration.log     ← copy of the calibration log for this session (if detection ran)
  summary.json        ← per-frame detection summary
  frames/
    0000_raw.jpg      ← downscaled camera frame
    0000_gray.jpg     ← grayscale used for detection
    0000_contrast.jpg ← per-point variance heatmap
    0000_overlay.jpg  ← annotated overlay (throttled)
    0000.json         ← grid point brightness data
```

Video is encoded via ffmpeg pipe in real-time — no post-processing delay.

---

## architecture

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

The display thread and detection thread run independently. Frames are passed via a `Queue(maxsize=1)` — if the detector is busy the frame is dropped and the camera loop continues unblocked.

| File | Role |
|------|------|
| `server.py` | WebSocket server, device assignment, effect broadcast |
| `controller.py` | Camera loop, GUI, detection thread management, exposure monitor |
| `effects.py` | Effect definitions, per-effect parameter storage, settings dialogs |
| `blink_encoder.py` | Manchester encoding / decoding |
| `blink_detector.py` | Grid sampler, variance gate, per-point decode, thread pool |
| `video_recorder.py` | Plain video recording via ffmpeg pipe |
| `camera.py` | Gamma, contrast helpers |
| `network.py` | HTTP helpers for controller → server calls |
| `elgato.py` | Camera Hub watchdog — AE monitor, ISO control via local WebSocket API |
| `state.py` | Shared state between threads |
| `log.py` | File logger (`debug/pixelmesh.log`) |
| `debug_capture.py` | Frame capture for offline analysis |
| `public/app.js` | Client-side blink renderer + effect engine |
| `public/sim.js` | Browser simulator (N fake clients) |

---

## signal encoding

Each device blinks one full cycle continuously:

```
[ 4 dark guard phases ]  [ Manchester(start + ID + ID + end) ]
```

- **PHASE_MS** `300ms` — duration of each screen phase
- **NUM_BITS** `9` — supports IDs 0–511
- **Cycle length** `44 phases × 300ms = 13.2s`
- Manchester: bit `1` → `[bright, dark]`, bit `0` → `[dark, bright]`
- ID is transmitted twice per cycle — up to 1 bit error corrected via majority vote

Detection uses actual frame timestamps + known `PHASE_MS` as ground truth — immune to variable camera fps. Anchor is computed from the end of the guard run (not the start) so phones that arrive mid-cycle are decoded correctly.

**Detection warmup**: the decoder requires a brightness history spanning at least one full cycle (13.2s) before it can attempt a decode. In practice, expect 15–20s from connection to first detection. At a live event with 300 phones joining over a few minutes, staggered arrival means most phones will be detected within 20s of connecting.

**Minimum camera fps**: the decoder needs at least ~10 fps to reliably sample 300ms phases (≥3 samples/phase). The camera is locked to manual exposure via AVFoundation on open to prevent it from slowing to 2–4 fps in dark rooms.

**Adaptive normalisation**: `hi` used for brightness normalisation is taken from the most recent one-cycle window (13.2s) rather than the all-time max. This prevents phone screen auto-dimming (ambient light sensor can reduce brightness by 4–5×) from pushing bright phases below the detection threshold.

**Burst frame guard**: cameras sometimes deliver several frames in rapid succession with nearly identical timestamps. The guard detection accepts a dark run if either its time span is sufficient OR its sample count meets `NUM_GUARD` — so burst deliveries are decoded correctly regardless of timestamp spread.

---

## camera exposure

The Elgato Facecam 4K ignores both `CAP_PROP_AUTO_EXPOSURE` via OpenCV and `AVCaptureExposureModeLocked` via AVFoundation — the firmware runs its own internal AE regardless. The code attempts both locks at startup but the Elgato reports `duration=0/0` meaning the lock is acknowledged but not applied.

**The only reliable fix is the Elgato Camera Hub app** — see camera setup above.

For other cameras that respect AVFoundation: `AVCaptureExposureModeLocked` is applied at startup and re-applied by a background monitor thread if fps drops below 8fps. `CAP_PROP_AUTO_EXPOSURE=0` and `CAP_PROP_EXPOSURE=-6` are also issued as a fallback.

---

## tuning

Key parameters in `blink_detector.py`:

| Parameter | Default | Notes |
|-----------|---------|-------|
| `grid_step` | `8px` | Distance between sample points. At step=8 the farthest any pixel can be from the nearest grid centre is ~5.7px — a phone just 3px wide always overlaps a patch. Covers phones at 25–30m at 1080p. |
| `sample_radius` | `4px` | Patch radius — 8×8=64px per point. Chosen to keep the 25,920×64 sampling matrix at 1.66MB, fitting inside L2/L3 cache on M1. r=6 (3.7MB) spills to RAM and makes `np.partition` 10× slower. |
| `brightness_pct` | `3` | Percentile used when sampling a patch. The ~2.8th percentile (k=1 of 64) catches even a single dark phone pixel during the dark phase. |
| `min_recent_std` | adaptive | Variance gate — auto-tuned each frame to scene noise floor. Starts at 0.10, adapts to `EMA(p90(all stds)) × 3.5`, clamped 0.05–0.15. |
| `recent_n` | `24` | Samples in recent window (~0.4s at 60fps) |
| `history_seconds` | `30.0` | Rolling brightness history per point (≥ 2 full cycles) |
| `decode_interval` | `0.2s` | Time between decode attempts per point (only applies to undiscovered phones) |

**Decode backoff**: grid points that fail to decode back off exponentially — retry interval is `min(decode_interval × 2^failures, 5.0s)`. The failure counter resets to zero on a successful decode.

**Stream display gate**: the binary stream overlay is only shown once a point has been active for at least 4 seconds AND has fewer than 6 consecutive decode failures. Real phones decode within ~26s; noise accumulates failures indefinitely.

**Phantom ID suppression**: any two IDs whose centroids are within 120px of each other are deduplicated — the lower-confidence one is dropped. Prevents a single phone from reporting two IDs due to symmetric Manchester patterns.

**Guard-phase decode extension**: after the main decode loop, the detector also tries points in `_ever_active` whose std has dropped below gate within the last 1.8s — recovering phones whose guard phase coincided with their warmup threshold crossing.

**Backward-scan decoder**: when a phone starts blinking before detection begins, the guard phase of its first complete cycle lands near the end of the history window. After failing to find enough forward data, the decoder anchors from the guard start and scans pre-guard history. Confidence is penalised 5% per assumed bit.

---

## performance

The display thread runs at full camera speed (~60fps). The detection thread runs independently at ~50fps on M1. The HUD shows both: `45 fps  det 48 fps  DET`.

**Tested hardware: Apple M1 Pro, 16GB RAM**

The bottleneck at 300+ phones is not compute — it is the 13.2s warmup cycle each phone must complete before its first decode attempt.

Key optimisations:

- **Threaded detection**: frames passed via `Queue(maxsize=1)` — if the detector is busy the frame is dropped and the display loop continues immediately
- **Time-budget decode cap**: decode attempts run until a 50ms wall-clock budget is exhausted, not a fixed count
- **Early-exit decoder**: exits as soon as confidence ≥ 0.95 — giving ~7× speedup per decode with clean signal
- **No re-decode of found phones**: once a phone's ID is known it is skipped entirely
- **Cache-friendly patch size**: `sample_radius=4` keeps the sampling matrix at 1.66MB (fits L2/L3 cache)
- **Vectorised std**: single `np.std(buf, axis=1)` over an `(N, recent_n)` circular buffer
- **Precomputed flat indices**: patch sampling is one numpy gather per frame, no per-point slicing
- **Vectorised decoder window scans**: numpy boolean indexing releases the GIL, running ~10–20× faster than Python list comprehensions
- **Gated history recording**: `add_sample` only called for points with std ≥ 0.003
- **Pre-allocated texture buffer**: persistent `(H, W, 4)` float32 buffer eliminates a 14MB/frame allocation
- **Conditional heatmap**: variance heatmap only built when debug capture is active
