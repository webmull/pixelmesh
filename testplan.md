# PixelMesh V2 — Test Plan

## 1. Detection Range & Size
- [ ] **1.1** Single phone at 5m, 10m, 15m, 20m — confirm detection time and confidence at each distance
- [ ] **1.2** Phone at angle (tilted 45°, facing away at 30°) — screen gets dimmer and narrower
- [ ] **1.3** Phone behind semi-transparent obstruction (e.g. gauze, hands partially blocking)

## 2. Lighting Conditions
- [ ] **2.1** Stage wash lighting (bright, coloured ambient) — adaptive gate must not suppress blink signal
- [ ] **2.2** Moving stage lights sweeping across camera FOV — diff finder must not flood ever_active
- [ ] **2.3** Mixed: stage lit front rows + dark back rows — gate adapts without wrecking either end

## 3. Multi-Phone Decode Accuracy
- [ ] **3.1** Two phones side by side within one grid step (8px at camera distance) — no patch bleed
- [ ] **3.2** 10 phones simultaneously — all 10 IDs decoded correctly, no cross-contamination
- [ ] **3.3** Phone that was detected then turns off — stops receiving effects (connected-client filter)

## 4. Decode Correctness
- [ ] **4.1** Full rotation of IDs 0–15 — every ID decodes to itself, no bit-flip errors
- [ ] **4.2** Backward scan decoder — phone entering frame mid-cycle still decodes via backward scan
- [ ] **4.3** phase_ambig on bit 0 — phone starting mid-stream still decodes correctly

## 5. False Positives
- [ ] **5.1** Camera pointed at a TV/screen — pulsing content must not decode as a valid ID
- [ ] **5.2** Reflections (floor, glass) — decoded ID not in blink_map must be silently dropped
- [ ] **5.3** Connected-client filter — any decoded ID absent from blink_map is suppressed

## 6. Recovery & Resilience
- [ ] **6.1** Phone briefly lost then reappears (someone walks in front) — resumes without 30s reset
- [ ] **6.2** Camera disconnects and reconnects — system recovers without restart
- [ ] **6.3** 60+ minute continuous run — memory stable, ever_active not growing unbounded

## 7. Scale & Performance (Brighton-relevant)
- [ ] **7.1** 50 phones simultaneously — fps stays above 30, decode latency under 45s
- [ ] **7.2** Two-camera handoff — phone in front-camera zone vs back-camera zone, no duplicate effects

## 8. Effects Sync
- [ ] **8.1** Strobe at fixed BPM with 20+ phones — all flash within one frame of each other (clock sync)
- [ ] **8.2** Sweep bar — u-position ordering correct, no phones placed incorrectly

---

**Must-pass before Brighton Dome:** range at 15m+ (1.1), multi-phone accuracy at 10+ (3.2), false positive filter (5.3), 60-minute stability run (6.3).
