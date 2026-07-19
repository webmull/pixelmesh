# 47 phones, 44 detections, 3 mysteries — a recap of last night's talk at Ministry of Testing London

Last night I gave a talk at the **Ministry of Testing London Chapter Meetup** on pixelmesh — a system I've been building that turns a live audience into a synchronised pixel display. No app install. No QR scan. No printed tag. Just a phone, a URL, and a single camera pointed at the crowd.

I did the obvious thing: I asked the room to be the demo.

47 of you scanned the URL on the screen and held up your phones. What follows is what the detection pipeline actually saw — written up from the debug bundle (`indigo-thorn-78`, the friendly name my run logger assigned) so you can see the data behind the live demo.

## The headline numbers

- **47** phones connected over the run.
- **44** detected by the camera. That's a **94% detection rate**.
- **3** missed — phones #12, #14, and #32. More on those below.
- **3** false positives rejected by the validator before they ever became "detections" — IDs 39, 47, and 216. Phantom #216 is the giveaway: only ~50 phones in the room, so an ID that high is pure decoder noise. The validity check caught all three.
- **0** misidentifications. Every accepted decode was a real phone.
- Detection ran for **263.9 seconds** — about 4½ minutes — at which point the system auto-stopped because the unfound count had stabilised.

## The detection performance

Median time-to-detect was **39 seconds** — three full 13-second blink cycles past the unavoidable signal warmup. Fastest was 13.5 s (phone #51 — that person had clearly already been blinking when I hit "go", so it decoded on its first available cycle). Slowest detected was **162.7 s** — twelve cycles of patient signal extraction before the variance gate let it through.

Only **5 of 44** (about 11%) decoded inside the first cycle past warmup. The detection curve is long-tailed: a shallow ramp through the first minute and then a slow accumulation as borderline signals finally clear the gate. **9 phones** decoded at perfect 1.000 confidence; 2 were genuinely borderline (#19 at 0.458, #36 at 0.561). Both still landed within the phantom-suppression radius, so we trusted them.

## The mystery: audience attrition

Here's the bit I want testers to notice.

At one point during the run, my Heads-Up Display showed **55 phones connected**. By the end, it was 47. That's not a bug — it's eight people who opened the URL, got allocated an ID, and then **closed the tab or backgrounded the browser long enough that the server's 90-second heartbeat reaper removed them from the active set**.

In other words: the "94% detected" number is honest, but it's measuring a moving denominator. **80% of the people who originally clicked were still in the room and on the URL when detection ended.** That gap matters — and it's the kind of thing you only see if you instrument both the system and the human funnel.

A testing-flavoured way to put it: my detector metric (a CV problem) and my audience-engagement metric (a UX problem) are different SLOs, and they need different alarms.

## The brightness story — Android, an OLED panel, and ABL

One observation from the room I want to single out, because it surprised me: at least one person in the audience told me afterwards that their **Android phone looked dim during the blink phase even though their brightness slider was all the way up**.

That's almost certainly **ABL — Automatic Brightness Limiting** on the OLED panel. Modern Android handsets (Pixels, Samsung's S series, OnePlus, OPPO) ship a hardware-level safeguard that throttles the panel when it displays a high average-picture-level pattern for any sustained time. **Solid white at 300 ms per phase is exactly the worst-case input for ABL.** The brightness slider is just a *request* to the panel — the firmware vetoes it when its burn-in / thermal heuristic fires.

The brightness slider also has no authority over Adaptive Brightness, over Battery Saver caps, or over Always-On Display power limits — and at least one of those is on by default on every Android I've checked.

Operationally, this almost certainly explains some of the long-tail decodes (signal compressed by panel throttling, takes more cycles to clear the gate) and quite possibly two of the three misses. **The "Hardware Floor" is real**, and "max brightness" doesn't reach it.

This is what I love about a real audience: the synthetic tests in my flat never hit ABL because I'm holding one phone, not 47.

## What the pipeline actually does (for the engineers)

For those who came up to ask afterwards how the detection itself works:

- Each phone Manchester-encodes its **9-bit ID** by flashing white/black at **300 ms per phase**. Manchester is **self-clocking** (every bit has a transition) and **DC-balanced** (mean brightness is constant), so the camera's auto-gain can't selectively cancel one phone's signal over another.
- The decoder doesn't try to find phones first. It samples a **fixed 32,400-point grid** across the 1920×1080 camera frame (every 8 px) and watches each point's brightness over time. Phones reveal themselves wherever the variance is rhythmic.
- An adaptive variance gate — `EMA(p90 of all stds) × 3.5` — picks out the candidates. About **0.75%** of the grid is "interesting" at typical moments; peak last night was 586 active points, with **27 phones being tracked simultaneously** in the busiest frame.
- Decoding runs in the **time domain, not the frame domain** — fps fluctuates but the phones blink on real wall-clock time. That's the single biggest difference from a typical CV pipeline.
- The whole detection pipeline is **~1,300 lines of Python** and **zero ML models**.

## Thanks

Ministry of Testing London — thanks for having me. Genuinely energising to talk to a room full of people who think about edge cases, observability, and the gap between "the system works" and "the system actually works under real conditions".

If you were in the room and your phone was **#12, #14, or #32** — I owe you an apology. If you were in the room and you saw your phone light up when I dragged my cursor across the audience — you helped me ship Spotlight Follow at 4 am the morning before.

Talk slides, the stats pack, and the post-event detail are on my desk. Happy to share with anyone who wants to dig deeper.

— Adam
