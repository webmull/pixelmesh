# PixelMesh TODO

## Stress Testing
- [ ] **Simulate N phones** — spawn N fake clients from one browser tab with configurable IDs to test effects without real devices (extend existing `/internal/sim`)
- [ ] **Slow phone simulation** — artificially delay one phone's detection to test partial detection scenarios
- [ ] **Disconnect/reconnect stress** — rapid reconnect loop to test session handling and ID reassignment

## Controller UI
- [ ] **Effect preview pane** — animated 180×90px thumbnail at the bottom of the sidebar showing the selected effect rendered on a fake 6×12 crowd grid; effect math ported from app.js to numpy, updates at ~10fps, switches automatically when effect or parameters change

## Adaptive Detection
- [ ] If `above_gate > 0` but `decoded = 0` for more than 2 full cycles, auto-nudge `min_recent_std` down in steps
- [ ] If still nothing, lower the `0.08` range floor in `decode_phases`
- [ ] Once detections start coming in, lock the values that worked

## Effects
- [ ] **Ripple** — circular wave from a click point (origin_u, origin_v); controller clicks preview to place origin; each device phases on `dist = sqrt((u-ou)²+(v-ov)²)`
- [ ] **Sparkle/Twinkle** — each device twinkles independently at a frequency seeded by blink_id; no coordination needed, scales well to 512
- [ ] **Color cycle (rainbow)** — HSV hue distributed across u-position, slowly rotating; striking with many devices spread across a room
- [ ] **Strobe** — hard sync flash at exact BPM across all devices; clock sync makes this tight
- [ ] **Radial sweep (radar)** — line rotates around a centre point, lighting devices as it crosses their angle
- [ ] **Heartbeat** — double-pulse (lub-dub) rhythm instead of single sine
- [ ] **Breathing** — very slow sine fade in/out; good for transitions and idle state
- [ ] **Stadium wave** — looping continuous sweep_bar (sinusoidal, repeating left-to-right); sweep_bar currently fires once
- [ ] **Confetti** — random devices flash random colours; seeded by `blink_id + floor(serverNow/interval)` so deterministic and synced without per-device messages

## Web Camera Preview (MJPEG Stream)
- [x] Share latest processed canvas frame from controller (JPEG-encoded, shared variable)
- [x] Add `/stream` MJPEG endpoint to server.py (`multipart/x-mixed-replace`) — live at `/internal/feed/v1`
- [x] Solve cross-process frame sharing — tmpfs JPEG file written by controller, polled by server
- [x] Embed in admin/dashboard page via `<img src="/stream">`
- [x] Cap stream to 10–15fps server-side (skip frames if last send < 80ms ago)

## Detection Tuning
- [ ] Reintroduce `roi_top_frac` at a lower value (e.g. 0.05–0.10) once room layout is stable — prevents ceiling lights eating decode budget, but 0.20 was too aggressive and cut off the above-door phone at y≈44 (8% from top)

## Debug / Developer Experience
- [x] Give debug capture folders a friendly random name (e.g. `autumn-fox-42`) instead of a timestamp so runs can be referred to specifically in conversation
- [x] Keep only the last 15 debug run folders — auto-delete oldest on startup

## Brighton Dome Deployment
- [ ] Test detection at distance — assess grid_step tuning for small apparent phone size
- [ ] Assess stage lighting impact on blink contrast
