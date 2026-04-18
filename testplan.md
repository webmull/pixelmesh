# PixelMesh V2 — Test Plan

## 1. Detection Range & Size
- [ ] Single phone at 5m, 10m, 15m, 20m — confirm detection time and confidence at each distance
- [ ] Phone at angle (tilted 45°, facing away at 30°) — screen gets dimmer and narrower
- [ ] Phone behind semi-transparent obstruction (e.g. gauze, hands partially blocking)

## 2. Lighting Conditions
- [ ] Stage wash lighting (bright, coloured ambient) — adaptive gate must not suppress blink signal
- [ ] Moving stage lights sweeping across camera FOV — diff finder must not flood ever_active
- [ ] Mixed: stage lit front rows + dark back rows — gate adapts without wrecking either end

## 3. Multi-Phone Decode Accuracy
- [ ] Two phones side by side within one grid step (8px at camera distance) — no patch bleed
- [ ] 10 phones simultaneously — all 10 IDs decoded correctly, no cross-contamination
- [ ] Phone that was detected then turns off — stops receiving effects (connected-client filter)

## 4. Decode Correctness
- [ ] Full rotation of IDs 0–15 — every ID decodes to itself, no bit-flip errors
- [ ] Backward scan decoder — phone entering frame mid-cycle still decodes via backward scan
- [ ] phase_ambig on bit 0 — phone starting mid-stream still decodes correctly

## 5. False Positives
- [ ] Camera pointed at a TV/screen — pulsing content must not decode as a valid ID
- [ ] Reflections (floor, glass) — decoded ID not in blink_map must be silently dropped
- [ ] Connected-client filter — any decoded ID absent from blink_map is suppressed

## 6. Recovery & Resilience
- [ ] Phone briefly lost then reappears (someone walks in front) — resumes without 30s reset
- [ ] Camera disconnects and reconnects — system recovers without restart
- [ ] 60+ minute continuous run — memory stable, ever_active not growing unbounded

## 7. Scale & Performance (Brighton-relevant)
- [ ] 50 phones simultaneously — fps stays above 30, decode latency under 45s
- [ ] Two-camera handoff — phone in front-camera zone vs back-camera zone, no duplicate effects

## 8. Effects Sync
- [ ] Strobe at fixed BPM with 20+ phones — all flash within one frame of each other (clock sync)
- [ ] Sweep bar — u-position ordering correct, no phones placed incorrectly

---

**Must-pass before Brighton Dome:** range at 15m+, multi-phone accuracy at 10+, false positive filter, 60-minute stability run.
