# PixelMesh TODO

## Camera Upgrade (Elgato Facecam 4K)
- [ ] Confirm actual delivered fps (measure real frame rate, not CAP_PROP_FPS readback)
- [ ] Disable HDR / lock exposure in Elgato companion app before running
- [ ] Tune `PHASE_MS` down from 450ms now that camera delivers true 30fps (target sub-10s first detection)

## Scale to 200 Devices
- [ ] Increase `NUM_BITS` from 5 → 8 (supports up to 256 IDs)
- [ ] Update `blink_encoder.py` and `sim.js` to match
- [ ] Re-tune `PHASE_MS` alongside bit increase to keep cycle time reasonable

## Adaptive Detection
- [ ] If `above_gate > 0` but `decoded = 0` for more than 2 full cycles, auto-nudge `min_recent_std` down in steps (0.18 → 0.14 → 0.10)
- [ ] If still nothing, lower the `0.08` range floor in `decode_phases`
- [ ] Once detections start coming in, lock the values that worked
- [ ] Test in bright ambient lighting with new camera first to get real range/std baselines before choosing step sizes

## Detection / Showtime
- [ ] When a detection run ends, any connected devices that were never detected should not receive effects — send them back to idle/dark state rather than playing showtime

## Brighton Dome Deployment
- [ ] Plan two-camera setup — one from stage (front half), one from FOH (back half)
- [ ] Test detection at distance — assess grid_step tuning for small apparent phone size
- [ ] Assess stage lighting impact on blink contrast
