# pixelmesh - Claude instructions

Project rules for AI assistants working in this repo. The reasoning behind most of these
lives in [CONTRIBUTING.md](CONTRIBUTING.md), which is the version written for humans.

## Detection code is frozen

**Never modify these files without explicit permission:**

- `blink_detector.py`
- `blink_encoder.py`

They hold a heavily optimised detection pipeline: vectorised patch sampling, pre-allocated
buffers, `_ever_active` gating, a decode budget, a diff-based phone finder. Every constant was
set by watching it fail in a real room, so a change that is obviously cleaner is usually
slower or quietly stops finding phones at the back. Unsolicited changes, including
"improvements", refactors, bug fixes and formatting, are not allowed.

If a task requires touching these files, stop and ask first.

The blink protocol is shared truth across `blink_encoder.py`, the phone client renderer and
the server's ID pool. All three move together or none of them do.

## Brand name is always lowercase

The product is written "pixelmesh". Never "PixelMesh", "Pixelmesh" or "PIXELMESH", in copy,
docs, headings, commit messages or UI strings, including at the start of a sentence.

## UI strings are ASCII-only

The Dear PyGui default font has no glyphs beyond ASCII, and `cv2.putText` on the HUD has the
same limitation. Em dashes, arrows, ellipses and characters like the approximately and
greater-or-equal signs all render as `?`.

Any string reaching the UI (`set_status`, `_set_status`, `dpg.add_text`, labels, `_iso_hint`,
HUD text) must be plain ASCII: `-` not an em dash, `->` not an arrow, `...` not an ellipsis,
`~` not the approximately sign. Log messages and code comments are exempt.

## Never push without permission

Commit freely. Never `git push` unless it has been explicitly asked for or approved in the
current conversation.

## Commit authorship

Do not add `Co-Authored-By` trailers, session links or any other AI attribution to commit
messages, in this repo or any of its history.
