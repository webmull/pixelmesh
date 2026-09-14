# Contributing

Thanks for looking. This is a personal project that runs at real events, so the bar for
changes is less "does it work on my machine" and more "would I trust it in front of two
hundred people who cannot be asked to try again".

That shapes everything below.

## Before you write code

Open an issue first for anything beyond a typo. A lot of what looks like an oversight here is
a decision with a room behind it, and the reasoning usually lives in a comment near the code
rather than in the commit. Ask, and you will get the story.

Good first contributions, in rough order of how welcome they are:

- **Effects.** Self-contained, visual, and they cannot break a show that is already running.
  See [Effects](README.md#effects).
- **Docs.** Especially anything you found confusing while getting it running.
- **Anything platform.** This is macOS-shaped today (see below) and the parts that need not
  be are worth untangling.
- **Tests.** Particularly around show state and reconnection.

## The two frozen files

`blink_detector.py` and `blink_encoder.py` are not open for unsolicited changes. Not because
they are precious, but because they are the result of a long argument with physics that you
cannot see from the source: vectorised patch sampling, pre-allocated buffers, `_ever_active`
gating, a decode budget per frame, a diff-based phone finder. Every constant in there was set
by watching it fail in a room.

A refactor that is obviously cleaner will usually be slower, or will quietly stop finding
phones at the back. If you have a change for either file, open an issue with a measurement:
what you ran it against, detection rate before and after, and the time per frame. Reasoning
alone is not enough, because reasoning is what put the wrong constants there the first time.

The blink protocol is shared truth across `blink_encoder.py`, the phone client renderer and
the server's ID pool. All three move together or none of them do.

## What good looks like

**Tests.** `python3 -m pytest tests/ -q`. They need no camera and no network. If your change
touches show state, add one. The suite exists because a bug in that area is invisible until
it is in front of an audience.

**Comments that say why.** The house style is to explain the decision, not the mechanism. If
a line looks strange and is deliberate, the next person needs to know what happens when they
"fix" it. Several comments in this repo are the scar tissue of a live failure, and they have
saved the same failure twice.

**No em dashes in UI strings.** The Dear PyGui font is ASCII-only, so anything reaching
`set_status`, a label, or the HUD renders `?` for anything cleverer than a hyphen. Log lines
and code comments are exempt.

**pixelmesh is lowercase.** Always, including at the start of a sentence.

## What you are agreeing to

Contributions are accepted under the [MIT Licence](LICENSE), same as the rest.
