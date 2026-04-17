# PixelMesh V2

Audience device coordination using screen-blink detection. Each connected device is assigned a unique ID and identified by the camera via a Manchester-encoded blinking pattern — no QR codes, no GPS, no app install required.

Built for live events. Designed for Brighton Dome.

---

## How it works

1. A device opens `https://local.pixelmesh.live` in their browser
2. The server assigns it a unique **blink ID** (0–511)
3. The device's screen blinks a Manchester-encoded pattern at 300ms per phase
4. A camera pointed at the audience captures the screen
5. The controller decodes each blinking screen and maps it to a position (u, v) in the room
6. Once detected, devices switch to **showtime mode** and render effects in sync

---

## Camera setup (Elgato Facecam 4K — do this before every run)

The camera **must** be set to manual exposure before starting the controller.

**Why auto-exposure breaks detection:** The blink signal is a screen switching between full-white and full-black at 300ms per phase. Auto-exposure sees a bright frame, reduces gain to compensate; sees a dark frame, increases gain. It tracks and cancels the blink. The resulting signal has a brightness range of ~0.28 instead of ~0.99 — the decoder sees a near-flat signal, misidentifies random noise as guard runs, and produces `empty_win` failures on every decode attempt. It is not a subtle degradation; detection stops working entirely.

The code attempts to lock exposure via AVFoundation at startup, but the Elgato's firmware ignores the lock (`duration=0/0` in the log). The only reliable fix is the Elgato app.

1. Open **Elgato Camera Hub**
2. Disable **Auto Exposure**
3. Set **ISO to 624**
4. Leave shutter speed at whatever gives a stable 60fps in your venue lighting

If signal range drops below 0.5 during a session the log will warn: `WARNING: low signal range=X.XX`. Causes: auto-exposure compressing amplitude, low phone screen brightness, or the ambient light sensor dimming the screen. Detection still works but takes longer — expect 25–35s instead of 13–15s.

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

| File | Contents |
|------|----------|
| `/tmp/pixelmesh-server.log` | FastAPI / uvicorn output — HTTP requests, WebSocket connects/disconnects, device assignments, effect broadcasts, errors |
| `/tmp/pixelmesh-ngrok.log` | ngrok tunnel output — connection status, forwarding address, request logs |
| `/tmp/pixelmesh-controller.log` | Controller stdout/stderr — startup errors, Dear PyGui exceptions |
| `debug/pixelmesh.log` | Blink detection diagnostics — camera open parameters, exposure lock status, per-frame gate/std stats, top active grid points, decode failures, effect triggers, UI errors. Appended across restarts. |
| `calibration_logs/YYYYMMDD_HHMMSS.log` | One file per detection session. Records the time-to-detect and confidence for each blink ID found. Useful for tuning and venue verification. |
| `recordings/YYYYMMDD_HHMMSS.mp4` | Plain H.264 video of the annotated camera view, started/stopped with `V`. Not committed to git. |

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
| `1`–`7` | Trigger effects (wave, gradient, binary wave, pulse, rainbow, colour flood, aurora) |
| `R` | Reset server |
| `Tab` | Toggle sidebar |
| `B` | Blackout camera feed |
| `G` | Start/stop debug capture |
| `V` | Start/stop video recording (saved to `recordings/`) |
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

Effects are launched from the controller sidebar (keys 1–7) or the sidebar buttons. Each effect stores its own parameters — opening the `…` dialog next to an effect shows only that effect's relevant controls. Changing a parameter in the dialog immediately re-fires the effect with the new value.

Per-effect parameters:

| Effect | Parameters |
|--------|-----------|
| Wave | Colour A, Speed, Direction |
| Gradient | Colour A, Speed, Direction |
| Binary Wave | Colour A, Speed, Direction |
| Pulse | Colour A, BPM |
| Rainbow | Speed, Direction |
| Colour Flood | Colour A, Colour B, Split, Speed, Direction |
| Aurora | Speed |

| Key | Effect | Notes |
|-----|--------|-------|
| `1` | Wave | Sine wave travelling across the audience |
| `2` | Gradient | Scrolling brightness gradient |
| `3` | Binary Wave | Hard on/off wave |
| `4` | Pulse | Whole audience pulses to BPM |
| `5` | Rainbow | Full spectrum hue sweep across the audience |
| `6` | Colour Flood | Two colours flooding in from opposite sides, meeting at Split |
| `7` | Aurora | Teal-purple curtain bands drifting across the room |

Clicking an effect button fires it with the current settings for that effect. Keys 1–7 do the same. Each effect has a `…` button in the sidebar that opens a settings dialog for that effect's parameters. Only one settings dialog is open at a time. Adjusting a parameter inside the dialog immediately re-fires the active effect.

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

The detection thread processes frames sequentially. Decode attempts are budget-capped at 12 per frame (highest-std candidates first) to bound worst-case frame time. `decode_phases_verbose` releases the GIL during numpy window scans, so the display thread is not blocked during decode.

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
| `state.py` | Shared state between threads |
| `log.py` | File logger (`debug/pixelmesh.log`) |
| `debug_capture.py` | Frame capture for offline analysis |
| `public/app.js` | Client-side blink renderer + effect engine |
| `public/sim.js` | Browser simulator (N fake clients) |

---

## Signal encoding

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

**Detection warmup**: the decoder requires a brightness history spanning at least one full cycle (13.2s) before it can attempt a decode. A phone that connects at T=0 will not be detected until T=13.2s at the earliest — and only if it has been in frame and blinking the whole time. In practice, expect 15–20s from connection to first detection per phone. This is a hard floor set by the protocol: each Manchester bit needs to be observed across its full phase window, which requires history long enough to contain at least one complete guard + data sequence. It cannot be reduced without shortening `PHASE_MS` (which reduces noise tolerance) or `NUM_BITS` (which reduces the ID space). At a live event with 300 phones joining over a few minutes, the staggered arrival means most phones will be detected within 20s of connecting — not 300 × 13.2s sequentially.

**Minimum camera fps**: the decoder needs at least ~10 fps to reliably sample 300 ms phases (≥3 samples/phase). The camera is locked to manual exposure via AVFoundation on open to prevent it from slowing to 2–4 fps in dark rooms.

**Adaptive normalisation**: `hi` used for brightness normalisation is taken from the most recent one-cycle window (13.2 s) rather than the all-time max. This prevents phone screen auto-dimming (ambient light sensor can reduce brightness by 4–5×) from pushing "bright" phases below the detection threshold and creating a spurious all-dark run that blocks the decoder.

**Burst frame guard**: cameras sometimes deliver several frames in rapid succession with nearly identical timestamps. The guard detection accepts a dark run if either its time span is sufficient OR its sample count meets `NUM_GUARD` — so burst deliveries are decoded correctly regardless of timestamp spread.

---

## Camera exposure

The Elgato Facecam 4K ignores both `CAP_PROP_AUTO_EXPOSURE` via OpenCV and `AVCaptureExposureModeLocked` via AVFoundation — the firmware runs its own internal AE regardless. The code attempts both locks at startup (and re-locks if fps drops below 8) but the Elgato reports `duration=0/0` meaning the lock is acknowledged but not applied.

**The only reliable fix is the Elgato Camera Hub app** — see Camera Setup above.

For other cameras that do respect AVFoundation: `AVCaptureExposureModeLocked` is applied at startup and re-applied by a background monitor thread if fps drops below 8fps. The OpenCV `CAP_PROP_AUTO_EXPOSURE=0` and `CAP_PROP_EXPOSURE=-6` calls are also issued as a fallback.

---

## Tuning

Key parameters in `blink_detector.py`:

| Parameter | Default | Notes |
|-----------|---------|-------|
| `grid_step` | `8px` | Distance between sample points. At step=8 the farthest any pixel can be from the nearest grid centre is ~5.7px — a phone just 3px wide always overlaps a patch. Covers phones at 25–30m at 1080p. |
| `sample_radius` | `4px` | Patch radius — 8×8=64px per point. Chosen specifically to keep the 25,920×64 sampling matrix at 1.66MB, which fits inside L2/L3 cache on M1. r=6 (3.7MB) spills to RAM and makes `np.partition` 10× slower — a hardware cache boundary, not an algorithmic difference. Coverage is equivalent: a phone pixel at the worst-case grid-corner position still lands in an adjacent point's r=4 patch. |
| `brightness_pct` | `3` | Percentile used when sampling a patch. The ~2.8th percentile (k=1 of 64) catches even a single dark phone pixel during the dark phase. |
| `min_recent_std` | adaptive | Variance gate — auto-tuned each frame to scene noise floor. Starts at 0.10, adapts to `EMA(p90(all stds)) × 3.5`, clamped 0.05–0.15. |
| `recent_n` | `24` | Samples in recent window (~0.4s at 60fps) |
| `history_seconds` | `30.0` | Rolling brightness history per point (≥ 2 full cycles) |
| `decode_interval` | `0.2s` | Time between decode attempts per point (only applies to undiscovered phones) |

The variance gate adapts every frame: `gate = EMA(p90(all stds)) × 3.5`, α=0.05, clamped to 0.05–0.15. p90 (not p75) is used to prevent the gate converging to the 0.05 floor in quiet scenes — with p75 the EMA drifts to near-zero after ~60 frames, flooding `above_gate` from ~20 to ~800 points. The 0.05 floor ensures gate stays above sensor noise (all real phone blink signals observed have std ≥ 0.08). Gate is reset to 0.10 on each `detector.reset()` so sessions don't inherit a drifted value.

Decoded IDs persist for the entire detection session and are **never re-decoded** — once a phone is found, it is skipped entirely so the full decode budget is available for undiscovered phones. IDs are only reset when detection is toggled off and back on (or `R` key).

**Decode backoff**: grid points that fail to decode back off exponentially — retry interval is `min(decode_interval × 2^failures, 5.0s)`. This prevents noisy non-phone regions (reflective surfaces, ambient flicker) from consuming the decode budget every 0.2s indefinitely. The failure counter resets to zero on a successful decode.

**Stream display gate**: the binary stream overlay is only shown once the point has been active for at least 4 seconds AND has fewer than 6 consecutive decode failures. Age alone isn't enough — sustained LEDs and reflections also pass the age gate. Decode failures are the stronger signal: real phones decode within ~26s; noise accumulates failures indefinitely. The failure counter resets to zero on a successful decode so legitimate phones are never suppressed.

**Phantom ID suppression**: some IDs have symmetric Manchester patterns (e.g. ID=0 = all zeros, ID=511 = all ones). A grid point sampling the same phone at a 1-phase offset decodes the bit-complement ID. After building the detected-device list, any two IDs whose centroids are within 120px of each other are deduplicated — the lower-confidence one is dropped. This prevents a single phone from reporting two IDs and avoids ghosting a distant phone at the wrong position.

---

## Performance

The display thread runs at full camera speed (~60fps). The detection thread runs independently at ~50fps on M1. The HUD shows both: `45 fps  det 48 fps  DET`.

**Tested hardware: Apple M1 Pro, 16GB RAM**

The M1 Pro is well-matched to this workload:
- **12MB L2 cache** — the 1.66MB sampling matrix fits entirely in cache; this is why `sample_radius=4` is a hard threshold, not a soft tuning parameter
- **GIL release during numpy window scans** — display thread (60fps camera) and detection thread run genuinely in parallel on separate cores
- **Decode throughput** — a clean early-exit decode takes ~0.3–0.5ms on M1 Pro; within the 50ms budget ~100 phones can be processed per frame. With 300 phones and ~50 undiscovered at any point, undiscovered phones clear within a single frame pass
- **Memory** — rolling 30s brightness history across 25,920 points peaks at ~50MB; no RAM pressure

The bottleneck at 300+ phones is not compute — it is the 13.2s warmup cycle each phone must complete before its first decode attempt. That is a function of the signal protocol (PHASE_MS × CYCLE_LEN) and cannot be reduced in software without shortening phase duration or the cycle length.

Key optimisations:

- **Threaded detection**: `process_frame` runs on a dedicated background thread. Frames are passed via `Queue(maxsize=1)` — if the detector is busy the frame is dropped and the display loop continues immediately.
- **Time-budget decode cap**: decode attempts run until a 50ms wall-clock budget is exhausted (not a fixed count). Fast decodes (clean signal, early-exit) consume less budget and allow more phones per frame; slow/noisy calls don't get more than their fair share.
- **Early-exit decoder**: the 7-threshold decode loop exits as soon as confidence ≥ 0.95 — with ISO 624 fixed exposure the first threshold (0.25) always succeeds, giving ~7× speedup per decode.
- **No re-decode of found phones**: once a phone's ID is known it is skipped entirely, reserving 100% of the decode budget for undiscovered phones. IDs reset only on `detector.reset()`.
- **Cache-friendly patch size**: `sample_radius=4` keeps the 25,920×64 sampling matrix at 1.66MB (fits L2/L3 cache). `r=6` produces a 3.7MB matrix that spills to RAM, making `np.partition` 10× slower with no detection benefit.
- **Vectorised std**: single `np.std(buf, axis=1)` over an `(N, recent_n)` circular buffer — replaces 25K Python `std()` calls per frame.
- **Precomputed flat indices**: `(N, flat_size)` int32 array built once per grid/radius; patch sampling is one numpy gather per frame, no per-point slicing.
- **Vectorised decoder window scans**: numpy boolean indexing replaces Python list comprehensions in the Manchester decoder, releasing the GIL and running ~10–20× faster.
- **Gated history recording**: `add_sample` only called for points with std ≥ 0.003 (avoids 25K Python list appends/frame).
- **Gated try_decode / draw_overlay**: `np.where(stds >= gate)` finds active indices in one pass; Python loops only run over the ~0–50 active points.
- **Pre-allocated texture buffer**: `frame_to_texture` uses a persistent `(H, W, 4)` float32 buffer with in-place `cv2.cvtColor` — eliminates a 14 MB/frame allocation.
- **Conditional heatmap**: the recent-std heatmap (variance visualisation for debug capture) is only built when debug capture is active (`G` key). Skipped on normal detection runs — saves a 1080p zeros allocation + circle loop per frame.
- **Vectorised history recording**: `np.where(stds >= gate)` reduces the history-update loop from ~25,920 Python iterations to ~50 per frame (measured: 18ms/frame saved). Guard-phase samples (phones temporarily below gate during 4 dark phases) are preserved via a `_ever_active` index set that tracks all points that have ever been above gate.

Remaining perf TODOs (in code):
- Pre-allocate gray pad buffer (0.13ms/frame, low risk)
- Skip gamma + contrast when blackout is on (2.24ms/frame, no risk)

