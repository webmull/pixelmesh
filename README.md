# PixelMesh V2

Audience device coordination using screen-blink detection. Each connected device is assigned a unique ID and identified by the camera via a Manchester-encoded blinking pattern — no QR codes, no GPS, no app install required.

Built for live events. Designed for Brighton Dome.

---

## How it works

1. A device opens `https://local.pixelmesh.live` in their browser
2. The server assigns it a unique **blink ID** (0–511)
3. The device's screen blinks a Manchester-encoded pattern at 450ms per phase
4. A camera pointed at the audience captures the screen
5. The controller decodes each blinking screen and maps it to a position (u, v) in the room
6. Once detected, devices switch to **showtime mode** and render effects in sync

---

## Requirements

- Python 3.10+
- [ngrok](https://ngrok.com) account with a reserved domain (`local.pixelmesh.live`)
- A wired webcam (USB-C recommended — built-in/Continuity Camera works but degrades signal quality). The controller auto-selects an Elgato Facecam 4K if present; use `K` to cycle cameras manually.

```bash
pip install -r requirements.txt
```

---

## Running

```bash
./run.sh
```

Interactive menu — options:

| Key | Action |
|-----|--------|
| `s` | Start server, ngrok, and controller |
| `r` | Reload — kill everything and restart |
| `d` | Die — kill everything |
| `q` | Quit |

Logs are written to:
- `/tmp/pixelmesh-server.log`
- `/tmp/pixelmesh-ngrok.log`
- `/tmp/pixelmesh-controller.log`
- `debug/pixelmesh.log` (blink detection diagnostics)
- `calibration_logs/YYYYMMDD_HHMMSS.log` (time-to-detect per device, one file per detection run)

---

## URLs

| URL | Description |
|-----|-------------|
| `https://local.pixelmesh.live` | Client app — share this with audience |
| `https://local.pixelmesh.live/sim` | Simulator — fake clients for testing |
| `http://localhost:8000` | Local access |

---

## Controller hotkeys

| Key | Action |
|-----|--------|
| `D` | Toggle detection on/off |
| `K` | Switch camera |
| `1`–`5` | Trigger effects (wave, gradient, binary wave, pulse, sweep bar) |
| `R` | Reset server |
| `Tab` | Toggle sidebar |
| `B` | Blackout camera feed |
| `G` | Start/stop debug capture |
| `O` | Toggle device ID overlays |
| `Q` / `Esc` | Quit |

---

## Device states

| State | Screen | Trigger |
|-------|--------|---------|
| **App closed / disconnected** | Black | Server shut down or connection lost |
| **Connected, waiting** | Solid yellow | Connected but detection not yet started |
| **Detection active** | White/black blink | Controller started detection |
| **Located** | Solid orange | Controller detected this device — holds through detection end until next command |
| **Detection ended — not found** | 3 red flashes → black | Detection stopped, device was not found |
| **Showtime — calibrated** | Effect (wave, pulse, etc.) | Effect broadcast from controller |
| **Showtime — not calibrated** | Black | Effect fired but this device has never been located |
| **Update pending** | Green flash × 5s → reload | New version of app.js deployed |

Orange (found) and yellow (waiting) clear when detection restarts or an effect fires. Not-found (red flash) transitions to black and stays black until the next detection cycle. Green flash is skipped if the phone is currently showing orange — it reloads silently instead. Green flash only triggers when `app.js` has actually changed since the phone last loaded — a server restart with no code changes produces the same hash and no reload.

---

## Effects editor

Effects are launched from the controller sidebar (keys 1–5). Parameters apply to all effects:

| Control | Effect |
|---------|--------|
| **Colour** | RGB tint — replaces the default white |
| **Speed** | Animation rate (0.05–4.0) |
| **Direction** | Angle in degrees — 0°=left→right, 90°=top→bottom, 180°=right→left, 270°=bottom→top, any angle for diagonal |
| **BPM** | Pulse rate (Pulse effect only) |

Adjusting any slider immediately re-fires the current effect with the new settings. Clicking an effect button fires it fresh with the current settings. Keys 1–5 use the current settings.

---

## Simulator

`/sim` spawns N fake clients in the browser. Simulator cells only flash white/black during detection mode — they go black in showtime or when detection ends, so they don't interfere with effect testing.

---

## Auto-reload

The server hashes `app.js` at startup into a `BUILD_ID` and sends it to every client on WebSocket connect. If the ID differs from the one stored in `localStorage` (i.e. new client code was deployed), the phone flashes green for 5 seconds then reloads to pick up the latest version — no manual refresh needed.

- If the phone is currently **orange** (detected), it reloads silently without the green flash so the state isn't disrupted
- A server restart with no code changes produces the same hash — no reload triggered
- `app.html` is excluded from the hash because `run.sh` modifies it with a cache-bust token on every start

---

## Debug capture

Press **G** in the controller to start/stop a debug run. Each run creates a timestamped folder under `debug/`:

```
debug/20240409_123456/
  overlay.mp4       ← full-speed H.264 video of the annotated camera view
  summary.json      ← per-frame detection summary
  frames/
    0000_raw.jpg    ← downscaled camera frame
    0000_gray.jpg   ← grayscale used for detection
    0000_contrast.jpg ← per-point variance heatmap
    0000_overlay.jpg  ← annotated overlay (throttled)
    0000.json         ← grid point brightness data
```

Video is encoded via ffmpeg pipe in real-time — no post-processing delay.

---

## Architecture

```
browser clients  ──WS──►  server.py (FastAPI)
                                │
                         controller.py (Dear PyGui)
                                │
                    ┌───────────┴───────────┐
               camera.py              blink_detector.py
               (OpenCV)               (grid sampler + decoder)
                                            │
                                      blink_encoder.py
                                      (Manchester codec)
```

| File | Role |
|------|------|
| `server.py` | WebSocket server, device assignment, effect broadcast |
| `controller.py` | Camera loop, blink detection, GUI |
| `blink_encoder.py` | Manchester encoding / decoding |
| `blink_detector.py` | Grid sampler, variance gate, per-point decode |
| `camera.py` | Gamma, contrast, sharpen helpers |
| `network.py` | HTTP helpers for controller → server calls |
| `state.py` | Shared state between threads |
| `log.py` | File logger (`debug/pixelmesh.log`) |
| `debug_capture.py` | Frame capture for offline analysis |
| `public/app.js` | Client-side blink renderer + effect engine |
| `public/sim.js` | Browser simulator (N fake clients) |

---

## Signal encoding

Each device blinks one full cycle continuously:

```
[ 6 dark guard phases ]  [ Manchester(start + ID + ID + end) ]
```

- **PHASE_MS** `300ms` — duration of each screen phase
- **NUM_BITS** `9` — supports IDs 0–511
- **Cycle length** `44 phases × 300ms = 13.2s`
- Manchester: bit `1` → `[bright, dark]`, bit `0` → `[dark, bright]`
- ID is transmitted twice per cycle for error checking

Detection uses actual frame timestamps + known `PHASE_MS` as ground truth — immune to variable camera fps.

---

## Tuning

Key parameters in `blink_detector.py`:

| Parameter | Default | Notes |
|-----------|---------|-------|
| `grid_step` | `30px` | Distance between sample points |
| `min_recent_std` | adaptive | Variance gate — auto-tuned each frame to scene noise floor |
| `recent_n` | `24` | Samples in recent window (~1.6s at 15fps) |
| `sample_radius` | `30px` | Patch radius around each grid point |
| `history_seconds` | `30.0` | Rolling brightness history per point |
| `decode_interval` | `0.2s` | Time between decode attempts per point |
| `brightness_pct` | `80` | Percentile used when sampling a patch (suppresses noise) |

The variance gate adapts every frame: `gate = p75(all point stds) × 5`, EMA-smoothed with α=0.02. This tracks exposure changes automatically — no manual tuning needed when switching cameras or lighting conditions.

---

## Planned

- Two-camera setup for Brighton Dome stalls coverage
