# Operations

Debug capture, logs, the show URL and offline page, load testing, crowd simulation and the
performance sentinel. Plus the tuning knobs and what they cost.

## Tuning and performance

**Two resolutions, on purpose.** The canvas every overlay is drawn onto, and therefore what the
MJPEG feed carries, is native `1920x1080` (`PREVIEW_WIDTH/HEIGHT` in `state.py`). The
operator's preview texture inside the controller window is `1280x720`
(`TEXTURE_WIDTH/HEIGHT` in `controller.py`). The audience sees native; the panel one person is
looking at does not need to be.

That split exists because DearPyGui uploads the preview as float32 RGBA on every render frame:
33 MB at native against 15 MB at 720p, or 2.0 GB/s versus 0.9 at 60 render fps. The symptom was
counter-intuitive, in that **fps fell when the window was made smaller**, because a smaller
window rasterises faster, so the render loop spins faster, so it pushes more of those textures
per second and starves the capture thread. Maximising slowed the render loop down and handed
the bandwidth back.

Capture-thread budget per frame at native, measured on a real crowd frame:

| Stage | ms |
|-------|-----|
| `build_canvas` | 0.37 |
| Gamma and contrast | 2.64 |
| Spotlight glow | 0.05 |
| `frame_to_texture` (including downscale) | 3.26 |
| **Total** | **6.32** of 16.7 available at 60 fps |

Before the spotlight and texture fixes that total was 10.24 ms. If it ever needs to come down
further, putting `PREVIEW_WIDTH/HEIGHT` back to `1280, 720` gives up native feed quality and is
the one-line revert.

Key parameters in `blink_detector.py`. The last two are overridden at startup in
`controller.py`, so the effective value is listed:

| Parameter | Value | Notes |
|-----------|-------|-------|
| `grid_step` | 8 px | Distance between sample points. At step 8 the farthest any pixel can sit from the nearest grid centre is about 5.7 px, so a phone just 3 px wide always overlaps a patch. Covers phones at 25 to 30 m at 1080p |
| `sample_radius` | 2 px | Patch radius, a 4×4 = 16 px patch. Reduced from 4 after the 05 May post-show analysis: the 8×8 patch frequently spanned phone plus dark background for back-row or partly-occluded phones, dragging the percentile to background-dark in *both* phases so std collapsed below the gate floor. At one missed phone (raw pixel std 0.37) the r=4 patch read 0.03 and the r=2 patch read 0.35 |
| `brightness_pct` | 10 | Percentile picked from the patch, paired with `sample_radius` so `k = int(flat_size × pct/100) = 1`, always the second-darkest pixel. At r=4 (64 px) p3 also gave k=1, but at r=2 (16 px) p10 is needed; p3 there gives k=0, which reads the absolute darkest pixel and is hypersensitive to sub-pixel noise |
| `min_recent_std` | adaptive | Starts at 0.10, then auto-tuned to `EMA(p90(all stds)) × 3.5`, clamped to 0.05 and 0.15. Asymmetric EMA (α=0.4 up, α=0.05 down), so a brightness spike raises the gate within 2 to 3 frames |
| `recent_n` | 18 | Samples in the recent window, about 1.2 s at 15 fps. Reduced from the 24 default for a ~25% cheaper `np.std` with no decode impact at typical frame rates |
| `history_seconds` | 15.0 | Rolling brightness history per point. Reduced from the 30 s default, halving list size and `add_sample` trim cost while staying well above the 10.0 s minimum for a full decode cycle |
| `decode_interval` | 0.2 s | Time between decode attempts per point, for undiscovered phones only |
| `roi_*_frac` | 0.0 | Fraction of the frame excluded from the detection grid on each edge. Controlled via the sidebar sliders |

**Tested on Apple M1 Pro, 16 GB RAM.** The display thread runs at ~60 fps and the detection
thread at ~50 fps. The bottleneck at 200+ phones is not compute. It is the 10.0 s warmup each
phone must complete before its first decode attempt.

| Optimisation | Impact |
|---|---|
| `Queue(maxsize=1)` frame drop | Display loop never blocked by detector |
| 50 ms wall-clock decode budget | Prevents per-frame overrun regardless of phone count |
| Early-exit decoder at confidence ≥ 0.95 | About 7× speedup per decode with a clean signal |
| No re-decode of found phones | Zero cost per frame once located |
| `sample_radius=2`, a 0.4 MB matrix | 25920×16 samples, more L2/L3-friendly than r=4 |
| `np.std` over a circular buffer | Single vectorised call, GIL released |
| Precomputed flat patch indices | One numpy gather per frame, no per-point slicing |
| Gated history recording | `add_sample` only called for active or gate-crossing points |
| Pre-allocated texture buffer | Eliminates a 14 MB/frame allocation |
| Batched like broadcasts | A ~3/s cap prevents O(clients²) WebSocket storms |
| WS feed with per-viewer stale-drop | One socket per hop, no per-frame HTTP; slow viewers skip to the newest frame instead of queueing |
| Cached HUD text rasterisation | Verdana glyphs render once per distinct label, so steady state is a tiny numpy blend |

---

## Operations

### Debug capture

Press **G** to start or stop a debug run. Each run is saved to a friendly-named folder under
`debug/` (for example `autumn-fox-42`). Runs are kept indefinitely, so prune manually if disk
space matters.

**A debug run does not stop when detection stops.** It stays up until you press `G` again or
quit the app, and `run.mp4` takes every frame regardless of whether detection is running. Only
the per-frame JPEGs under `frames/` are gated on it. At 1920×1080 that is roughly
**1.1 GB/hour**, plus a continuous 1080p ffmpeg encode competing with the show on the same
machine. It used to auto-start with detection (`_DEBUG_AUTO_ON_DETECT`), which meant one
overnight session quietly wrote 2.6 GB across 7 hours with detection off the whole time. It is
off by default now, so start a run deliberately when it is worth capturing.

```
debug/autumn-fox-42/
  run.mp4             ← annotated camera view (H.264, real-time encoded)
  calibration.log     ← time-to-detect and confidence per blink ID
  summary.json        ← per-frame detection summary
  frames/
    0000_raw.jpg      ← downscaled camera frame
    0000_gray.jpg     ← grayscale used for detection
    0000_contrast.jpg ← per-point variance heatmap
    0000_overlay.jpg  ← annotated overlay (throttled)
    0000.json         ← grid point brightness data
```

### Logs

| File | Contents |
|------|----------|
| `/tmp/pixelmesh-server.log` | FastAPI and uvicorn: HTTP, WebSocket, assignments, broadcasts, errors |
| `/tmp/pixelmesh-ngrok.log` | ngrok tunnel: connection status, forwarding address |
| `/tmp/pixelmesh-controller.log` | Controller stdout and stderr: startup errors, Dear PyGui exceptions |
| `debug/pixelmesh.log` | Blink detection diagnostics: gate and std stats, decode failures, effect triggers. Appended across restarts |
| `debug/calibration_logs/YYYYMMDD_HHMMSS.log` | One file per detection session: time-to-detect and confidence per blink ID |
| `debug/reports/YYYYMMDD_HHMMSS.txt` | Post-show report, generated automatically on every reset |
| `debug/recordings/YYYYMMDD_HHMMSS.mp4` | Video recording (hotkey `V`). Not committed to git |

### Show URL and offline page

`pixelmesh.show` is an always-on **cloud endpoint** at ngrok's edge, not a direct tunnel. The
agent (started by `run.sh` from `ngrok.pixelmesh.yml`) binds the internal endpoint
`https://pixelmesh-agent.internal`, and the cloud endpoint's traffic policy forwards to it when
the agent is up. When it isn't, the edge serves a holding page: wordmark, "The show has not yet
begun", and a 10 s countdown that quietly `fetch`-probes the origin, reloading only when the
response stops carrying the `x-pixelmesh-holding` marker header, meaning the show is actually
up. No reload flash while waiting, no laptop involvement while offline, and no
`ERR_NGROK_3200`.

The policy, including the embedded holding-page HTML, is versioned at `ngrok.cloud-policy.yml`.
The dashboard serves whatever was pasted last, so re-paste after editing the file (dashboard,
Universal Gateway, Endpoints, `pixelmesh.show`, Traffic Policy).

**How the pieces fit:**

| Piece | Where | Role |
|---|---|---|
| Reserved domain `pixelmesh.show` | ngrok dashboard, Domains | DNS for the show URL (CNAME at the registrar per ngrok's instructions) |
| Cloud endpoint on `pixelmesh.show` | Dashboard, Endpoints | Always-on edge listener that runs the traffic policy |
| Traffic policy | Pasted from `ngrok.cloud-policy.yml` | `forward-internal` to the agent; holding page when the forward fails **or** returns a 5xx (agent up, app closed) |
| Internal endpoint `pixelmesh-agent.internal` | Claimed by the agent at start | Private rendezvous between edge and laptop, not publicly reachable |
| Agent authtoken | `~/Library/Application Support/ngrok/ngrok.yml` | Default agent config; `run.sh` passes it alongside the project config |
| Tunnel definition | `ngrok.pixelmesh.yml` | Binds the internal endpoint to `localhost:8000`, inspection off, compression on |

**One-time setup on a new ngrok account:**

1. Reserve `pixelmesh.show` under **Universal Gateway, Domains** and point the registrar's DNS
   at ngrok per the instructions shown.
2. **Endpoints, New, Cloud Endpoint**, and bind `https://pixelmesh.show`.
3. Paste the contents of `ngrok.cloud-policy.yml` into the endpoint's Traffic Policy and save.
4. Put the account authtoken in the default agent config (`ngrok config add-authtoken ...`).
   There is nothing to configure for the internal endpoint, since the agent claims it on start.

**Region.** The domain's "Region and IP resolution" is pinned to **Europe** in the dashboard
(Jul 2026). It defaults to global latency-aware DNS, but `pixelmesh.show` reaches ngrok via a
Namecheap ALIAS record, and ALIAS flattening resolves from Namecheap's US servers. "Global"
therefore answered with US PoPs and every audience message crossed the Atlantic twice (RTT p50
273 ms at 500 clients, 55 ms once pinned to EU). If the domain setup ever changes, re-verify
with `python3 tools/load_test.py --host pixelmesh.show --wss`.

**Verifying the three states** (from `run.sh`, where `s` starts everything):

- Nothing running, and `pixelmesh.show` shows the holding page served at the edge.
- Full stack running, and it shows the audience app.
- Tunnel up but server stopped, and it shows the holding page again via the 5xx catch.

### Load testing

`tools/load_test.py` simulates a crowd against a running server with the real protocol per
client (hello, assign, heartbeats, sync-ping RTT probes) plus a mid-hold like storm, and
reports percentiles with pass/fail verdicts. Locally: `python3 tools/load_test.py --clients 500`.
Through the real edge, add `--host pixelmesh.show --wss`. Baselines (Jul 2026, M1 Pro):
500/500 clients, zero drops, RTT p50 4 ms local and 55 ms through the EU edge, server at 12%
CPU.

### Simulating a crowd

`./sim.sh` spawns fake audience phones as real browsers, which is the difference between it and
`tools/load_test.py`: load_test speaks the protocol, sim runs the actual client. Each phone is
its own Chrome instance with its own `--user-data-dir`, so it gets its own localStorage
`device_id` and the server counts it as a genuinely distinct client rather than another tab.
Windows open in app mode (no tab strip, no URL bar) and tile across every display, using
`NSScreen.visibleFrame` so nothing hides under the menu bar or the Dock.

```
./sim.sh                # 2 phones against pixelmesh.show
./sim.sh 20             # 20 phones
./sim.sh 6 --local      # against http://127.0.0.1:8000
./sim.sh --kill         # stop the crowd
./sim.sh 40 --fill      # tile edge to edge: load and UI work only, not detection
./sim.sh 20 --jitter    # phones drift a few px, like hands that are not still
./sim.sh 20 --jitter 14 # ...with a wider wobble
```

`--jitter` random-walks each window around its home position, because a crowd nailed to the
pixel grid is a best case the real show never gets, and it leaves the diff-based phone finder
and the centroid tracking untested. Default drift is 8px, which is about 3 camera px at typical
framing, roughly a held phone's tremor. Movement goes over the Chrome DevTools protocol rather
than System Events, so it needs no Accessibility permission: each phone gets a debugging port
and `tools/sim_cdp.py` drives `Browser.setWindowBounds` on it. Drift is clamped to the slack
each window has inside its own layout cell, so a wobbling phone can never wander into its
neighbour. Ports are only opened when the flag is passed.

Ctrl-C tears the crowd down. Profiles persist under `.sim-profiles/`, so a sim phone keeps its
`device_id` and blink id between runs, and `--fresh` wipes them for a new crowd.

**Total phone coverage is capped at 8% of screen area, and that cap is the whole trick.** The
detector's noise gate is 3.5x the 90th percentile of grid-point variance, capped at 0.15, and
that percentile is meant to be measuring the dark room behind the audience. Tile the windows
edge to edge and the phones become most of the camera frame, so the gate starts measuring
phones instead, climbs to its ceiling, and locks out every phone that cannot clear it. The
crowd raises the bar against itself. Measured on 28 Aug 2026: 50 tiled phones detected 20, with
the gate pinned at its 0.150 ceiling for 37% of frames. The same machine with the coverage cap
detected 20 of 20 in 28.9 seconds, median 12.2 s, gate at its ceiling for 10% of frames.

This is a property of screen area, not phone count. A real audience of 54 phones detected 44,
with the gate at its 0.050 floor, because real phones are small bright rectangles separated by
people.

Chrome will not make a window narrower than about 86px, so past roughly 20 phones the windows
cannot shrink far enough to stay inside the budget on their own. The lit area has no such floor:
`#card-blink` is its own fixed-position element, so the window sits at Chrome's minimum while
the blinking patch inside it is inset to whatever the budget allows, black all around. That is
also closer to the real thing. sim.sh says when it kicks in:

```
note: 40 phones need 63px of lit area but Chrome will not make a window under 86px, so the
blink patch is inset to 73% inside a black window to stay within the coverage budget
```

Measured at 40 phones on two 1080p displays: 86x140 windows with a 64x104 lit patch, 6.4% lit
coverage against 11.6% if the windows had been left alone. The ceiling on this machine is 276
phones, at which point the patch drops below the 24px the camera can usefully resolve and
sim.sh refuses rather than producing an invalid test. `--fill` still tiles edge to edge for
load and UI work.

The same reasoning applies to the ROI: the sampling grid is built inside it, so cropping tight
around a dense block of phones shrinks the denominator and walks toward the same saturation.
Leaving some dark room inside the ROI is what keeps the gate at its floor.

### Performance sentinel

The display loop self-monitors frame pacing and logs a `[perf]` warning only when the average
exceeds 45 ms over a 5 s window, where healthy is 24 to 40 ms. That is the signature that
caught the July `save_frame` stall. Silence means pacing is fine.

### Client auto-reload

The server hashes `app.js` at startup into a `BUILD_ID`, stamped into both the page's script
tag and `server_hello`. The client compares the server's build against the one embedded in its
own page, rather than localStorage history, which used to reload already-current pages once per
deploy. A fresh page therefore always matches and connects once, while a stale parked page
reloads exactly once.

- Same code plus a server restart gives the same hash, so no reload
- New `app.js` plus a server restart gives a new hash, and parked clients reload within seconds

---
