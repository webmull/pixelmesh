# TODO — code review backlog (29 Jul 2026)

Findings from a full multi-agent review, verified against source. Ordered by severity.
Items marked **frozen zone** touch `blink_detector.py` / `blink_encoder.py` and need explicit
sign-off. `file:line` are from the review; re-check before editing.

## Critical — before Brighton

- [ ] **Public exposure of internal/admin surface.** `AdminTokenMiddleware` only guards
  `/admin/*`; `/internal/feed/v1`, `/internal/feed/ws`, `/internal/dashboard`,
  `/internal/debug`, and the `/debug-files/*` mount are unauthenticated, and
  `ngrok.cloud-policy.yml` forwards every path to the agent. While the stack is up, anyone with
  the URL can watch the audience camera, pull debug videos + calibration logs, and open
  unlimited MJPEG streams (also a cheap DoS). Fix: gate `/internal/*` + `/debug-files` behind
  the admin token, or path-allowlist the cloud policy to `/`, `/app`, `/ws`, `/admin/show_stats`.
  (`server.py:105,812,890,936,978`)
- [ ] **Secure `/admin/overlays`.** Added 16 Aug 2026 as a deliberate stopgap so the talk deck
  can turn overlays on when it reaches the camera slide. It is a WRITE route exempt from the
  admin token (`_ADMIN_PUBLIC`), because a static HTML file cannot hold a token that `run.sh`
  regenerates every launch. Not wide open: `_is_local_request()` serves only loopback clients
  with no ngrok forwarding headers, so tunnelled traffic gets a 403. But any page open in a
  browser on the show laptop can still poke it, since CORS is `*` and a preflight succeeds for
  any origin. Blast radius is cosmetic — device markers flicker on the feed; it cannot stop
  detection or recording. Fix properly by either serving the deck from the pixelmesh server so
  it is same-origin and can be handed a token, or having `run.sh` write a per-session token
  where the deck can read it. The path-allowlist in the item above would also block it at the
  ngrok edge, which is worth doing regardless. (`server.py` `_ADMIN_PUBLIC`, `set_overlays`)
- [ ] **Video recording freezes the app on a slow/full disk.** `video_recorder.record()` writes
  full raw frames to ffmpeg stdin synchronously on the render thread, no writer thread; a wedged
  (not dead) ffmpeg blocks the pipe forever, freezing camera + projection + detection. Only
  `BrokenPipeError` is handled. Fix: give it a writer thread + bounded queue like
  `debug_capture`, drop frames when full. (`video_recorder.py:75`)
- [ ] **Audience reconnect race blacks out / stalls phones.** `app.js` WS handlers act on the
  shared `ws` with no staleness guard; a background+return during backoff spawns overlapping
  `connect()` chains that close each other's sockets (repeated black flashes), and one
  interleaving stalls a phone forever (`_livenessCheck` returns early on a `CONNECTING` socket).
  Fix: tag each socket, ignore events from non-current sockets, don't close an OPEN healthy one.
  (`app.js:449,480,494-498,519-556`)

## High

- [ ] **Detection silently wedges (no `except` in the worker).** `_detection_worker` loop is
  `try/finally` with no `except`; `detector.reset()` runs from UI/MIDI threads while the worker
  may be mid-`process_frame`. Double-tap `D` or pedal reset during detection → stale-index
  raise → thread dies, UI still says "Detecting", nothing found until restart. Fix: wrap the
  frame body in try/except, log + continue. (`controller.py:2623-2720`)
- [ ] **Mid-show camera unplug is unrecoverable + silent.** `holder["cap"]` is never reset to
  `None` and `camera_scan_worker` exits once the Elgato opens; after a USB glitch `cap.read()`
  returns False forever, preview freezes, no status, no rescan, exposure monitor still logs "fps
  OK". The "camera disappeared" cleanup is dead code. Fix: on repeated read-fail set
  `holder["cap"]=None` + restart the scan worker. (`controller.py:2300-2329`)
- [ ] **Recorder stop-vs-record race can exit the controller.** Pedal `vid_rec.stop()` (MIDI
  thread) closes stdin + nulls `_proc` while `record()` (render thread) writes; the resulting
  `ValueError`/`AttributeError` isn't caught and unwinds the main loop → app quits. Fix: lock
  record/stop, or catch `(ValueError, AttributeError)` in `record()`. (`video_recorder.py:74-93`,
  `controller.py:2235-2242`)
- [ ] **Pedal stomp can freeze the GUI (off-thread DPG).** `reset_server()` on the MIDI thread
  calls `game.set_game_btn_highlight()` → `dpg.bind_item_theme()` off the main thread — the
  documented "05 May" freeze pattern. Fix: route the highlight change through `ui_queue`.
  (`controller.py:2262→1696`, `game.py:348-357`)
- [ ] **Elgato AE watchdog thread dies silently.** Unguarded body; a malformed Camera Hub reply
  (`int(result["value"])` on None, non-dict device entry) kills the thread with `connected` left
  True — AE guard + ISO control gone, sidebar still shows connected. Fix: wrap the poll loop
  body. (`elgato.py:187,254-316`)
- [ ] **MIDI loop thread dies silently.** Unguarded `open_port(idx)` (shifted index raises) and
  bare controller callback dispatch; any raise kills pedal input for the show with `connected`
  stuck. Fix: guard the loop + dispatch. (`midi.py:162,205-227`)
- [ ] **Missed phones hard-reload after the red flash.** Liveness "assign lost" branch reloads
  every undetected phone 1-4s after the missed-flash → idle transition. Fix: don't reload on an
  OPEN socket / exclude the deliberate idle-after-missed path. (`app.js:212-214`)
- [ ] **Clock-sync EMA seeded from 0 → ~1min off-beat after every reconnect.** First pong applies
  only 25% of true offset while `synced` flips true immediately. Fix: `clockOffset = best.offset`
  on first sample when `!synced`. (`app.js:617-618`)
- [ ] **MJPEG stream gzipped level-9 on the event loop.** starlette 1.3.1 compresses streaming
  responses; every JPEG per viewer deflates in the send path, stealing loop time from the
  broadcast. Fix: exclude the feed from GZipMiddleware. (`server.py:894-897`)
- [ ] **`run.sh` reload can kill the projector / misjudge liveness.**
  `lsof -ti tcp:8000 | xargs kill -9` kills whatever is *connected* to :8000 (projection
  browser holding the feed); `pgrep/pkill -f controller.py` substring-matches `vim controller.py`
  or a second checkout. Fix: kill by listener PID only; tighten pgrep patterns.
  (`run.sh:66-67,115-118`)
- [ ] **Zombie phone sockets after a broadcast timeout.** A send-timeout drops a phone but the
  wedged transport also times out the fire-and-forget close; the ping handler only refreshes
  `last_seen` without re-adding to `connections`, so the phone pings forever, receives nothing,
  and app.js only reloads when `view==="idle"`. Fix: re-add on ping, or reap on receive-silence.
  (`server.py:227-231,350,490-492`)

## Medium

- [ ] **`/admin/reset` doesn't stop a running race** — `game_active` stays true, race loop keeps
  broadcasting; phones drop to WAITING but stage keeps animating. (`server.py:674-684`)
- [ ] **Stop Game after a natural finish does nothing** — early-returns without broadcasting the
  calm wave, phones stuck on the winner card. (`game.py:186-187`)
- [ ] **`debug_capture.stop_run()` can leak its writer thread** — `put_nowait` swallows Full; a
  leaked saver races the unlocked `frame_idx += 1` → duplicate/overwritten frame indices.
  (`debug_capture.py:130-141,264-266`)
- [ ] **Pedal detection-toggle silently swallowed** by `_ui_syncing` if it lands mid-UI-drain →
  fresh-run reset wipes positions but detection never starts. (`controller.py:1088-1091`)
- [ ] **`_detection_timings` not cleared on detection start** (only on Reset) → back-to-back
  reports carry stale entries. (`controller.py:1103-1111`)
- [ ] **Dashboard freezes on a dead feed** — relies only on `img.onerror`, which a stalled-open
  or post-frame stream death doesn't fire. Add staleness detection + reconnect.
  (`dashboard.html:88-93`)
- [ ] **ID-pool exhaustion → hello storms** — 512 IDs + 30min retention + fresh UUIDs in private
  browsing; late joiners get error+close and reconnect-loop until IDs recycle.
  (`server.py:159,402-406`)
- [ ] **permessage-deflate on the feed WS** deflates incompressible JPEG per viewer on the event
  loop (uvicorn default). Disable for the feed. (`server.py:812-830`)
- [ ] **`/admin/positions` unguarded parse** — one bad batch entry raises mid-loop, drops the
  rest of the batch + skips the `phones_located` broadcast. (`server.py:601-607`)
- [ ] **Rejoin during a dark moment parks on the located card** — `assigned` exits effects view,
  server only replays a live effect. (`app.js:596-600`, `server.py:431-432`)
- [ ] **Unsynced ripple smeared by preSyncOffset** — `serverNow()` still adds the 0-8s desync
  offset when `!synced`, defeating the ripple exemption. (`app.js:457-459,1001`)
- [ ] **Half-open zombie socket after iOS resume undetected** — heartbeat is send-only, no pong
  deadline, `_livenessCheck` exits for any non-idle view. (`app.js:466-473,202`)
- [ ] **stage.js has no CONNECTING watchdog / liveness backstop** — a hung handshake freezes the
  projector with no retry. (`stage.js:18-28`)
- [ ] **Duplicate-watchdog race on `[r]`** — flag removed+recreated inside the old watchdog's
  sleep → two watchdogs, two controllers. (`run.sh:169-171,237-238`)
- [ ] **Elgato 30s reconnect + silent `set_property`** — after a Hub crash, ISO/AE dead 30-60s+
  while sidebar/pedal ISO changes are silently dropped. (`elgato.py:23,227,262-316`)
- [ ] **`midi.history()` unlocked read vs append** — `list(reversed(deque))` can raise "deque
  mutated"; `update_ui_from_state()` is called bare in the render loop → crash. (`midi.py:110-114`,
  `controller.py:2477`)
- [ ] **`_save_report` check-then-spawn not atomic** — auto-stop + Reset in the window → double
  report, or a late worker re-marks `_report_saved_path` and the next run saves nothing.
  (`controller.py:1621-1672`)

## Low

- [ ] Fix the 2 failing tests (`tests/test_server_pool.py`) — **test bug**: they write
  `blink_assignments` directly and bypass the `blink_reverse` index from `a57aa02`. Write both
  dicts (or a helper). Also: file `sys.exit`s at import without `PIXELMESH_ADMIN_TOKEN`.
- [x] `"PixelMesh V2"` casing fixed everywhere (the line-6 title was already lowercase; the h1 and five source headers were not).
- [ ] `state.*` reads outside `state.lock` (GIL-atomic, benign) — tidy for discipline.
  (`controller.py:945-948,981,1262,1390,1496,2413,2742`)
- [ ] `draw_device_overlay` reads `_valid_blink_ids` twice unguarded → possible KeyError → app
  exit with Render Order overlay on. Snapshot the set once. (`controller.py:596-612`)
- [ ] Dead AVFoundation lock-guard code — PyObjC returns a truthy tuple, so lock-fail guards
  never fire. (`controller.py:384,424,433`)
- [ ] `game._start_poll` leaks an immortal daemon thread when `_fetch_json` returns None (server
  hiccup); a Start during an active round stacks a duplicate poller. (`game.py:386-388`)
- [ ] `stage.js` confetti trickle never stops after the final race — emit gate only checks
  `winner != null`. (`stage.js:233-244`)
- [ ] `app.js` `onmessage` JSON parse is unguarded (stage.js wraps its parse) — a non-JSON frame
  drops crowd-wide. (`app.js:514-516`)
- [ ] Bare `create_task` for reaper + heart-broadcast (no ref kept) — GC-collectible in theory.
  (`server.py:90-91`)
- [ ] `FileResponse("dashboard.html")` is cwd-relative — 404s on manual launch outside the repo.
  (`server.py:938`)

## Frozen zone — analysis only, needs sign-off

- [ ] **Encoder 1-bit copy-mismatch always trusts copy 1** → returned ID wrong ~50% when it
  fires, then locked for the session (no re-decode). Highest-value frozen fix.
  (`blink_encoder.py:193-210`)
- [ ] Backward scan accepts single-copy decodes with zero redundancy at ~0.6 conf, locked
  forever. (`blink_encoder.py:263-342`)
- [ ] `_patch_idx` keyed on grid shape + radius but not padded-frame width → a ≤7px camera
  renegotiation silently samples wrong pixels. (`blink_detector.py:208-209,320-326`)
- [ ] `_ever_active` grows toward the full 25,920-point grid under sustained stage-flicker
  (eviction needs points to go quiet) → memory + fps collapse, no recovery until reset.
  (`blink_detector.py:452-472,515-535`)
- [ ] Cluster-dedup `valid_ids` exemption can keep a phantom ID inside another phone's cluster,
  locking a real client to the wrong seat. (`blink_detector.py:657-675`)
- [ ] Decode-budget strict highest-std-first ordering can starve dim/distant phones under bright
  flicker noise. (`blink_detector.py:577-590`)
- [ ] Docstring says 6 guard frames; `NUM_GUARD = 4`. (`blink_encoder.py:7,14-16`)
