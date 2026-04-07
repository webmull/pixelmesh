# PixelMesh V2

Audience device coordination using screen-blink detection. Each connected device is assigned a unique ID and identified by the camera via a Manchester-encoded blinking pattern — no QR codes, no GPS, no app install required.

Built for live events. Designed for Brighton Dome.

---

## How it works

1. A device opens `https://local.pixelmesh.live` in their browser
2. The server assigns it a unique **blink ID** (0–31)
3. The device's screen blinks a Manchester-encoded pattern at 450ms per phase
4. A camera pointed at the audience captures the screen
5. The controller decodes each blinking screen and maps it to a position (u, v) in the room
6. Once detected, devices switch to **showtime mode** and render effects in sync

---

## Requirements

- Python 3.10+
- [ngrok](https://ngrok.com) account with a reserved domain (`local.pixelmesh.live`)
- A wired webcam (USB-C recommended — built-in/Continuity Camera works but degrades signal quality)

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
| `t` | Toggle light / dark mode |
| `q` | Quit |

Logs are written to:
- `/tmp/pixelmesh-server.log`
- `/tmp/pixelmesh-ngrok.log`
- `/tmp/pixelmesh-controller.log`
- `debug/pixelmesh.log` (blink detection diagnostics)

---

## URLs

| URL | Description |
|-----|-------------|
| `https://local.pixelmesh.live` | Client app — share this with audience |
| `https://local.pixelmesh.live/sim` | Simulator — fake clients for testing |
| `http://localhost:8000` | Local access |

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

- **PHASE_MS** `450ms` — duration of each screen phase
- **NUM_BITS** `5` — supports IDs 0–31
- **Cycle length** `30 phases × 450ms = 13.5s`
- Manchester: bit `1` → `[bright, dark]`, bit `0` → `[dark, bright]`
- ID is transmitted twice per cycle for error checking

Detection uses actual frame timestamps + known `PHASE_MS` as ground truth — immune to variable camera fps.

---

## Tuning

Key parameters in `blink_detector.py`:

| Parameter | Default | Notes |
|-----------|---------|-------|
| `grid_step` | `40px` | Distance between sample points |
| `min_recent_std` | `0.18` | Variance gate — below this, point ignored |
| `recent_n` | `24` | Samples in recent window (~1.6s at 15fps) |
| `sample_radius` | `20px` | Patch radius around each grid point |
| `history_seconds` | `35.0` | Rolling brightness history per point |
| `decode_interval` | `0.5s` | Time between decode attempts per point |

---

## Planned

- Scale to 200+ devices (8-bit IDs)
- Reduce `PHASE_MS` once camera delivers true 30fps
- Two-camera setup for Brighton Dome stalls coverage
