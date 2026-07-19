# I asked 47 strangers to hold up their phones. A single camera found 44 of them in four minutes — and the misses taught me more than the hits.

`[[ MARKER — EMBED: london_opener_hero.gif (hero, top of article) ]]`
> *The actual room: the London audience holding up phones in a near-dark venue — first blinking white as the camera finds them, then lighting up in colour as pixelmesh drives each one as its own pixel. Loops.*

Last week I stood in front of a room at the **Ministry of Testing London** meetup and tried something I'd been building towards for months. Instead of showing the audience a demo, I turned the audience into the demo.

There was nothing to install. No printed markers. Just a URL on the screen, a room full of people holding up their phones, and a single camera trying to work out who was who.

I asked everyone to open the URL and hold up their phone. Four and a half minutes later, that one camera had worked out *which phone belonged to which person and roughly where they were sitting* — accurate enough to drive each phone as an individual pixel in a giant, low-resolution display made of the audience itself.

You've probably seen the clip doing the rounds this week: a QR code on a World Cup big screen, fans scan it, and a whole stadium's phones flash in unison into a light show. That's the dazzling, easy version of this idea — a crowd as a **strobe**. What I've been chasing is the harder one: a crowd as a **screen**, where the system actually knows which phone is which, and where each one is sitting.

That harder version is **pixelmesh**. Last week's room was the **biggest crowd I've pointed it at so far** — which, as you'll see, is exactly when the interesting failure modes show up. And because it was that kind of crowd — a real mix of people, with plenty of testers among them — I did the other thing you're not supposed to do: I showed them the data, including the parts that didn't go to plan.

`[[ MARKER — EMBED: spatial_calibration.gif ]]`
> *The whole run, compressed: a single camera locating all 44 phones one-by-one over 4½ minutes. Each dot is a real phone at its real position in the frame, appearing in the real order it was found — lighter dots decoded fast, darker dots took longer. Loops.*

`[[ MARKER — EMBED: 07_spatial_labelled.png (static) ]]`
> *The same map, frozen and labelled — every detected phone by its blink-ID, so you can see exactly who the camera found and how fast (light = fast, dark = slow). The clean animation above shows it happening; this still lets you read it.*

## The headline numbers

Here's what the detection pipeline actually saw:

- **47** phones connected by the end of the run.
- **44** detected by the camera — a **94% detection rate**.
- **3** missed entirely.
- **3** false positives caught and *rejected* before they could become "detections."
- **0** misidentifications. Every phone the system accepted was a real phone.
- Total detection time: **263.9 seconds**, at which point it auto-stopped because the unfound count had gone flat.

Those numbers look pretty good on their own. But they aren't the interesting part. The interesting part is everything that almost worked.

## Lesson 1: your success metric has a moving denominator

Midway through the run, my heads-up display showed **55 phones connected**. By the end, **47**.

That's not a bug. Eight people opened the URL, were assigned an ID, and then closed the tab or backgrounded their browser long enough that the server's 90-second heartbeat reaper quietly removed them from the active set.

So "94% detected" is an honest number — but it's measuring the people who *stuck around*. Measured against everyone who originally clicked, the camera found about **80%**.

That was probably my biggest takeaway from the evening. Finding phones was a computer vision problem. Keeping people engaged long enough to find them was a UX problem — a waiting screen that wasn't compelling enough to hold attention for four minutes.

**They are different problems, with different fixes, and they need different alarms.** You only see the gap if you instrument both the machine *and* the human funnel. Most dashboards only watch the machine.

## Lesson 2: "maximum brightness" is a request, not a guarantee

After the talk, someone told me their Android phone looked *dim* during the demo — even with the brightness slider pinned to the top.

That's almost certainly **ABL — Automatic Brightness Limiting**. Modern OLED panels (Pixel, Samsung, OnePlus and friends) ship a hardware safeguard that throttles the screen when it shows a high-average-brightness pattern for any sustained period. A phone flashing solid white every 300 milliseconds is close to the worst-case input you can hand that firmware.

The brightness slider is a *request*. The panel's thermal-and-burn-in heuristic gets the final say — and it doesn't ask permission. That hardware floor almost certainly explains some of my slowest detections and possibly two of the three misses.

It reminded me why nothing beats putting something in front of real people. I could never recreate 47 different phones, manufacturers, firmware versions and battery states at home. **The audience found a problem I'd never have discovered on my own.**

## The easy half and the hard half

There are two problems hiding inside "turn an audience into a display," and they are nowhere near equally hard.

The first is **time**: every phone has to change colour at the same instant, or the picture smears into mush. This *sounds* like the hard one. It isn't. Every phone simply renders against a shared clock — the server stamps a wall-clock time on each instruction ("go white at 20:14:33.500") and each phone just waits for that moment. Clock sync is a solved problem. You write it once and never think about it again.

That also explains why the World Cup clip that's been doing the rounds recently is so clever. [Here's what's actually happening](https://www.reddit.com/r/Damnthatsinteresting/comments/1ug7whz/during_a_world_cup_football_match_a_qr_code/): tens of thousands of fans scan the QR code, it opens a web page on each phone, and a central server fires every flashlight against one shared clock. No app, no install — synchronisation at enormous scale. It's genuinely brilliant, and it is *precisely* this easy half.

`[[ MARKER — EMBED: world_cup_sync.gif ]]`
> *The World Cup clip: tens of thousands of phone-flashlights pulsing in unison across the stands. Spectacular — and pure clock sync. (Original footage via r/Damnthatsinteresting — check reuse rights before publishing.)*

But look closely at what it **can't** do. It can flash the entire crowd in unison, even ripple them in timed waves — yet it has no idea that *your* phone is the one in block 122, row 9. Nobody told it, and nothing measured it; the server is broadcasting the same heartbeat to everyone, blind to where anyone is sitting. That's the ceiling of pure clock sync: synchronised, spectacular, and completely **space-unaware**. It can make the crowd a strobe. It cannot make the crowd a *screen*.

The second is **space**: to use a phone as a pixel, you have to know *where it physically is*. Indoors there's no GPS, no seat numbers, nothing that tells you the phone displaying ID #19 is the one in row 4, seat 11. The only sensor pointed at the room is a single camera — and a camera sees light, not identities. Figuring out which patch of pixels is which phone is the hard half, and it's the entire reason the run takes 4½ *minutes* and not 4½ *seconds*.

`[[ MARKER — EMBED: time_vs_space.gif ]]`
> *Two panels, same 44 phones. Left (time): they all fire in perfect sync the instant you ask — that's the World Cup-stadium trick, and clock sync is free. Right (space): the camera has to find each one, slowly, in order, with no prior idea where anyone is. The asymmetry is the whole point.*

So the bigger the audience, the more this asymmetry bites. Syncing 44 phones in time is no harder than syncing 4. But *locating* 44 phones means 44 faint, competing signals in one camera frame — and that's where a big room earns its long tail.

## How it actually works (for the curious)

The trick turned out to be not looking for phones at all.

Each phone Manchester-encodes its **9-bit ID** by flashing white and black at 300ms per phase. Manchester encoding is *self-clocking* (every bit carries a transition) and *DC-balanced* (average brightness stays constant), so the camera's auto-gain can't accidentally cancel one phone's signal while boosting another's.

The decoder never looks for phones. It lays a fixed grid of **32,400 sample points** across the camera frame and simply watches each point's brightness over time. Phones reveal themselves wherever the flicker is rhythmic and structured. An adaptive variance gate picks out the candidates; at the busiest moment last week it was tracking **27 phones simultaneously**.

`[[ MARKER — EMBED: decoder_view.gif ]]`
> *The decoder's-eye view — the actual camera feed (brightened way up; the room was near-pitch-black) with the raw Manchester bit-strings it's reading off each blinking phone. This is what "reading codes off a dark room" literally looks like. Loops.*

Crucially, it decodes in the **time domain, not the frame domain** — frame rate wobbles, but the phones blink on real wall-clock time, so the decode doesn't care if the camera stutters.

The entire detection pipeline is about **1,300 lines of Python and zero machine-learning models**. Looking back, the code wasn't actually the difficult bit. The difficult bit was finding an approach that turned a messy computer vision problem into something much closer to signal processing.

## What surprised me in the logs

Looking back through the logs afterwards, two things really stood out.

First, **the closest two phones were 16 pixels apart** — in a 1920-pixel-wide frame, that's two adjacent cells on the sample grid, two people sitting shoulder to shoulder. The system still resolved them as separate phones, both confidently. That's the real spatial resolution of turning a crowd into a screen, and it's the answer to the question everyone asks: *can it actually tell individual people apart?* Yes — down to about a hand's width at the back of a room.

Second, **the long tail of slow detections isn't random — it's the back of the room**. Decode time tracks how far away a phone sat (a clear correlation with its position in the frame): the near rows landed in seconds, the far rows took minutes. Only 9 of the 44 decoded within the first two blink cycles; ten of them were still arriving after the 100-second mark. A more distant phone is a smaller, dimmer patch, so its blink needs more cycles to climb above the noise floor — the same signal-strength story as the Android brightness limiting, just caused by distance instead of firmware. The physics is sitting right there in the timestamps.

## The takeaway

I'd call the demo a success. But not because of the 94%. The best part wasn't the number on the slide. It was everything the audience exposed that I'd never seen before — the eight people who drifted off, the Android panel that quietly ignored its own brightness slider, and the three phones I never found.

Build the thing. But spend just as much time instrumenting it. The gap between *"the system works"* and *"the system works under real conditions, with real people, on real hardware you don't control"* is where all the actual learning lives.

Massive thanks to everyone at Ministry of Testing London for being willing guinea pigs. Having a mixed room of people — a good few of them testers — throwing real, varied devices at something like this is about the best test environment you could ask for. And if your phone was #12, #14, or #32 — I owe you an apology, and a louder spec next time.

If you'd like the full stats pack, or just want to talk about audience-scale detection, drop me a message — I'm always happy to go deeper.

Building something is fun. Watching it survive real people is where you actually learn something.

— Adam
