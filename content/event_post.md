# 47 people held up their phones. The camera found 44 of them in 4½ minutes.

Last week, our audience opened a URL on their phones and held them up. No install. No QR scan. No printed tag on the back of the case. Within four minutes, a single camera pointed at the crowd had figured out exactly which phone belonged to which person and where they were sitting in the room — accurate enough to choreograph each one as its own pixel in a giant low-resolution display.

This is PixelMesh, the audience-as-pixels system I've been building. Here's what actually happened in the data from that detection run (run ID `indigo-thorn-78`, for the engineers).

## The numbers

- **47** phones connected over the run.
- **44** detected by the camera — a **94%** detection rate.
- **3** missed (IDs 12, 14, 32 — most likely brightness-limited; iOS Low Power Mode is a quiet killer).
- **3** false positives caught and rejected (the variance gate is good, the validity check is the second line of defence).
- **0** misidentifications. Every accepted decode was a real phone.

Median time to find a phone was **39 seconds** — three full 13-second blink cycles past the unavoidable warmup. The slowest detected was 162 seconds — a phone that probably had to fight ambient-light dimming for twelve cycles before the signal lifted above the noise floor.

## The bit that surprised me

When I started the run, **55 phones had assigned IDs**. By the time detection ended, **47 were still alive**. Eight people opened the URL, got allocated an ID, and then closed the tab — long enough that the server's heartbeat reaper aged them out. Audience attrition is a real number in a system like this, and it's bigger than I expected. "94% detected" was actually only 80% of everyone who originally clicked.

It's a useful reminder that the technical metric and the audience metric are different things. Detection is a signal-processing problem. Holding people's attention is a UX problem.

## How it works (briefly)

Each phone Manchester-encodes its 9-bit ID by flashing white/black at 300ms per phase. Manchester is self-clocking (every bit has a transition) and DC-balanced (mean brightness is constant), so the camera's auto-gain can't selectively cancel one phone's signal over another. The detector samples a fixed 32,400-point grid across the camera frame and watches for points where the brightness varies rhythmically in the right pattern. No phone-finding, no object tracking — just signal processing on a 2D grid. The whole detection pipeline is ~1,300 lines of Python and 0 machine-learning models.

## What's next

I'm giving a talk this week on how PixelMesh actually does the detection — what happens between a phone displaying a blink and the system saying "you are phone #19, you are sitting in row 4 seat 11, here is the wave effect you'll render at exactly 20:14:33.500." More on that soon.

If you were one of the 47 in the audience last week — thank you. If you were one of the 3 who never got found, I owe you an apology and a louder spec next time. And if you were one of the 8 who closed the tab early, fair enough; I'll write a better waiting screen.
