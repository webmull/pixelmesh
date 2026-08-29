# (c) Adam Davis - adamdavis.co.uk
"""
pixelmesh — server

Differences from V1:
- Devices are assigned a small integer blink_id (0-255) instead of a tag image ID.
- Positions come from the controller's blink detection, not AprilTag calibration.
- No tag-image or projection endpoints.
- Adds /admin/positions  (controller posts detected blink_id → u,v)
- Adds /admin/blink_map  (controller reads blink_id → device_uuid mapping)
- Adds /admin/detect     (controller signals detection on/off; server tells clients)
"""

import asyncio
import bisect
import hashlib
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

import game
from blink_encoder import NUM_BITS

_BASE_DIR = os.path.dirname(__file__)
_PUBLIC_DIR = os.path.join(_BASE_DIR, "public")

_ADMIN_TOKEN = os.environ.get("PIXELMESH_ADMIN_TOKEN", "")
if not _ADMIN_TOKEN:
    # Admin routes are otherwise unauthenticated on 0.0.0.0:8000.  run.sh
    # always sets the token before launch; refuse to start without it so
    # nobody accidentally exposes /admin/* by running uvicorn directly.
    print("PIXELMESH_ADMIN_TOKEN is empty — refusing to start (run via run.sh)",
          file=sys.stderr)
    sys.exit(1)


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        # Versioned assets (app.js?v=hash) are immutable — cache for a year.
        # Everything else (app.html) must never be cached so clients always get
        # the latest build_id and auto-reload logic fires correctly.
        qs = scope.get("query_string", b"").decode()
        if qs.startswith("v="):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"]        = "no-cache"
            response.headers["Expires"]       = "0"
        return response


# Block patterns commonly probed by bots
_BLOCKED = (
    ".git", ".env", ".htaccess", "wp-", "phpmy", "admin.php",
    "config.php", "setup.php", ".aws", ".ssh", "passwd",
)

class BlockBotsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path.lower()
        if any(b in path for b in _BLOCKED):
            return Response(status_code=404)
        return await call_next(request)


# Admin routes exempt from the token check.
#
# /admin/show_stats is read-only and safe to serve to anyone.
#
# /admin/overlays is a WRITE and is therefore a deliberate, temporary
# compromise (see docs/TODO.md - "secure /admin/overlays"). The talk deck has
# to turn overlays on when it reaches the camera slide, and a static HTML file
# cannot hold a token that run.sh regenerates every launch. It carries its own
# restriction instead: the handler serves only requests that originated on
# this machine. Overlays are cosmetic - the worst this allows is markers
# flickering on the feed. Detection and recording stay behind the token,
# because those can stop a show.
_ADMIN_PUBLIC = {"/admin/show_stats", "/admin/overlays", "/admin/end",
                 "/admin/recording", "/admin/recording/latest"}

# Admin routes a browser on another origin may call.  Being in here only makes
# the browser willing to send the request and read the reply; it does NOT
# exempt the route from the token check.  /admin/mode is deliberately in this
# set but NOT in _ADMIN_PUBLIC: it can stop detection mid-show, so it stays
# authenticated.  A caller must send X-Admin-Token.
_ADMIN_CORS = _ADMIN_PUBLIC | {"/admin/mode"}


def _is_local_request(request: Request) -> bool:
    """True only for a request that originated on this machine.

    Loopback alone does not prove it. ngrok forwards the public
    pixelmesh.show to 127.0.0.1:8000, so tunnelled traffic also arrives from
    a loopback address - and the traffic policy forwards every path, so
    /admin/* is reachable from the internet. The forwarding headers are what
    separate the two, and ngrok always sets them.
    """
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        return False
    forwarded = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                 "ngrok-skip-browser-warning")
    return not any(h in request.headers for h in forwarded)

_CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Admin-Token",
    "Access-Control-Max-Age":       "600",
    # Chrome's Private Network Access: a page on a public or opaque origin
    # (the deck runs from file://) preflighting a request to a local address
    # is refused unless the response opts in. Harmless everywhere else -
    # browsers that do not implement PNA ignore it.
    "Access-Control-Allow-Private-Network": "true",
}


class AdminTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        is_public = path in _ADMIN_PUBLIC
        allow_cors = path in _ADMIN_CORS

        # A CORS preflight carries no credentials by specification - the
        # browser strips X-Admin-Token from the OPTIONS probe and only sends
        # it on the real request.  Answering the preflight before the token
        # check is therefore required, not a hole: OPTIONS reaches no handler
        # and returns no data.
        if allow_cors and request.method == "OPTIONS":
            return Response(status_code=204, headers=dict(_CORS_HEADERS))

        if path.startswith("/admin/") and not is_public:
            if request.headers.get("X-Admin-Token") != _ADMIN_TOKEN:
                # The CORS headers go on the 403 too.  Without them the
                # browser reports an opaque "CORS error" and hides the status,
                # so a caller with a bad token cannot tell auth failure from a
                # misconfigured server.
                return Response(
                    status_code=403,
                    headers=dict(_CORS_HEADERS) if allow_cors else None,
                )

        response = await call_next(request)
        # The talk deck polls /admin/show_stats from a different origin (it is
        # opened as a file:// or localhost page), so the browser needs this
        # header before it will let that page read the response.  Scoped to
        # _ADMIN_CORS - nothing else under /admin/ becomes cross-origin
        # readable.
        if allow_cors:
            response.headers.update(_CORS_HEADERS)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(reap_dead_clients())
    asyncio.create_task(heart_broadcast_loop())
    yield
    await broadcast({"type": "shutdown"})


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(AdminTokenMiddleware)
app.add_middleware(BlockBotsMiddleware)
app.mount("/public", NoCacheStaticFiles(directory=_PUBLIC_DIR), name="public")

_DEBUG_DIR = os.path.join(_BASE_DIR, "debug")
if os.path.isdir(_DEBUG_DIR):
    # Serve debug run files (videos, frames) — range requests handled by StaticFiles
    app.mount("/debug-files", StaticFiles(directory=_DEBUG_DIR), name="debug_files")

app.include_router(game.router)

# ------------------------------------------------------------------ #
# Mode constants                                                       #
# ------------------------------------------------------------------ #
MODE_WAITING   = "WAITING"        # clients show idle screen
MODE_DETECTION = "DETECTION"     # clients blink their ID
MODE_SHOWTIME  = "SHOWTIME"      # clients render effects
MODE_ENDED     = "ENDED"         # show over, clients show the closing card

mode = MODE_WAITING

# Hash of client-facing static files — changes when code is deployed.
# Clients reload automatically when this differs from what they loaded with.
def _build_id() -> str:
    h = hashlib.md5()
    # Only hash app.js — changing app.js bumps BUILD_ID, triggering client reloads
    for fname in ("app.js",):
        try:
            with open(os.path.join(_PUBLIC_DIR, fname), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:10]

BUILD_ID = _build_id()

# Source mtime at import, so the process can tell you it is behind the files.
# app.js and app.html re-render on change (see _app_bundle), but server.py does
# not - routes, permissions and payload shapes are frozen at import. That gap
# cost three separate debugging rounds in one afternoon: the deck was refused
# by a route that was public on disk, phones were served markup from before a
# card existed, and a found time was missing because the field had not been
# added yet. In every case the code was right and the process was old, with
# nothing on screen to say so.
_SERVER_MTIME_AT_IMPORT = os.path.getmtime(__file__) if os.path.exists(__file__) else 0.0


def server_is_stale() -> bool:
    """True when server.py has changed on disk since this process started."""
    try:
        return os.path.getmtime(__file__) > _SERVER_MTIME_AT_IMPORT + 0.5
    except OSError:
        return False


# Last-broadcast effect, replayed to clients that connect mid-session.
current_effect_state: dict | None = None
like_count: int    = 0
like_enabled: bool = True
_heart_dirty: bool  = False   # pending broadcast from tap accumulation
_count_dirty: bool  = False   # pending crowd_count broadcast (debounced join/leave)


# Whether the controller has actively started detection (distinct from mode).
detection_active = False

# Whether the controller has enabled clock sync.
sync_active = False

# How long each phone took to be found, in ms, measured from the moment
# detection started. Server-side rather than client-side on purpose: the phone
# is the wrong place to keep it. A reload drops in-memory state, so refreshing
# the closing card lost the number while the phone id and the map - both of
# which come from here - survived. It is also the same clock the controller's
# calibration log uses, so the two agree.
detection_started_at: float | None = None
found_ms: dict[str, int] = {}

# Taken when /admin/end fires, and the reason both exist:
#
#   show_roster - who was actually in the show. The closing card is a souvenir
#     of something you were part of, and the connect handler cannot tell a
#     returning participant from a stranger who opened the link afterwards
#     without it. Membership, not found_ms: a phone that was in the room and
#     never detected has no found_ms either, and those people are precisely who
#     the em dash on the card was written for.
#
#   show_totals - the numbers as they stood at the end. total_connected is
#     len(blink_assignments), which keeps growing as latecomers connect and are
#     assigned ids, so the talk deck's closing slide would count upward while it
#     was on screen. Frozen here, it cannot.
#
# Both are cleared when a new run starts, so a second show is a clean slate.
show_roster: set[str] = set()
show_totals: dict[str, int | None] = {}


# ------------------------------------------------------------------ #
# Mode control (POST /admin/mode)                                      #
# ------------------------------------------------------------------ #
# Remote on/off switches for things the CONTROLLER owns.  The server holds no
# camera and no recorder, so it cannot act on these itself - it records the
# request and the controller picks it up on its next poll and does the work.
#
# Requests, not desired state.  Each mode carries a sequence number that only
# increments, and the controller applies a mode only when it sees a seq it has
# not applied yet.  A plain desired-state flag would fight the operator: stop
# detection with the pedal and one second later the controller would read
# "detection: true" still sitting here and switch it straight back on.
#
# To add a mode: add a name here and a branch in controller._apply_mode.
MODES = ("detection", "recording", "overlays")

# name -> {"enabled": bool, "seq": int}.  seq 0 means "never requested", which
# is what lets the controller adopt the current values on connect without
# firing them.
mode_requests: dict[str, dict] = {m: {"enabled": False, "seq": 0} for m in MODES}

# What the controller reports is ACTUALLY true, echoed back after it applies a
# request.  None until it first reports.  Kept separate from the request
# because the two legitimately disagree: a request can be refused (no camera),
# and the operator can change a mode locally without any request at all.
mode_actual: dict[str, bool | None] = {m: None for m in MODES}

# ------------------------------------------------------------------ #
# State                                                                #
# ------------------------------------------------------------------ #
connections:       dict[str, WebSocket] = {}   # device_uuid → ws (phones)
spectators:        dict[str, WebSocket] = {}   # device_uuid → ws (stage page, etc.)
blink_assignments: dict[str, int]       = {}   # device_uuid → blink_id
blink_reverse:     dict[int, str]       = {}   # blink_id    → device_uuid
positions:         dict[str, dict]      = {}   # device_uuid → {"u", "v"}
last_seen:         dict[str, float]     = {}   # device_uuid → timestamp
sync_stats:        dict[str, dict]      = {}   # device_uuid → {rtt_ms, offset_ms, samples, ts}

# Derived from NUM_BITS, not written out: the encoder, the client renderer and
# this pool must agree on how many IDs exist. A literal here silently outlives a
# change to NUM_BITS and hands out ids the encoder cannot represent.
available_blinks = list(range(2 ** NUM_BITS))  # pool of unassigned blink IDs

HEARTBEAT_TIMEOUT = 90     # seconds of silence before the socket is closed
IDENTITY_TIMEOUT  = 1800   # seconds of silence before blink_id + position are recycled

# A phone that can't accept one frame in this long is effectively gone; drop
# it rather than let it stall anyone else. Its identity survives (_drop_connection)
# so it resumes cleanly on reconnect.
SEND_TIMEOUT = 1.0



# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def blink_to_device(blink_id: int) -> str | None:
    return blink_reverse.get(blink_id)


_broadcasting_count = False


async def _close_quietly(ws):
    try:
        await asyncio.wait_for(ws.close(), SEND_TIMEOUT)
    except Exception:
        pass


async def _timed_send(ws, text: str) -> bool:
    try:
        await asyncio.wait_for(ws.send_text(text), SEND_TIMEOUT)
        return True
    except Exception:
        return False


async def _timed_send_json(ws, obj: dict) -> bool:
    try:
        await asyncio.wait_for(ws.send_json(obj), SEND_TIMEOUT)
        return True
    except Exception:
        return False


async def broadcast(message: dict):
    global _broadcasting_count
    # Serialize once, not once per client.  send_json re-ran json.dumps for every
    # socket (~16 ms/broadcast at 300 phones); send_text reuses the same bytes.
    text = json.dumps(message, separators=(",", ":"))
    # Snapshot with list(): sends await, and a concurrent hello/disconnect/
    # reap mutating `connections` mid-iteration would raise "dict changed size
    # during iteration".  Because heart_broadcast_loop / reap_dead_clients call
    # broadcast without their own guard, that error would kill those tasks for
    # the rest of the show.
    #
    # Fan out concurrently with a per-send timeout. The old sequential
    # `await send` meant one stalled phone (backgrounded iOS, TCP zero-window
    # — socket open but not ACKing) blocked every phone after it in dict
    # order for up to ~40s until the ws keepalive gave up, freezing all
    # output for the whole audience. A timed-out socket may still be open
    # TCP-wise, so close it in the background: the phone's onclose fires and
    # it reconnects, instead of listening forever on a socket the server no
    # longer tracks.
    conns = list(connections.items())
    sent_ok = await asyncio.gather(*(_timed_send(ws, text) for _, ws in conns))
    dead = []
    for (device_id, ws), ok in zip(conns, sent_ok):
        if not ok:
            dead.append(device_id)
            _drop_connection(device_id)
            asyncio.create_task(_close_quietly(ws))
    # Same payload goes to spectators (stage page, future read-only viewers).
    # Failures just drop them — they reconnect on their own.
    specs = list(spectators.items())
    spec_ok = await asyncio.gather(*(_timed_send(ws, text) for _, ws in specs))
    for (sid, ws), ok in zip(specs, spec_ok):
        if not ok:
            spectators.pop(sid, None)
            asyncio.create_task(_close_quietly(ws))
    # Guard against re-entry: broadcast_count → broadcast → broadcast_count
    # loops once if more sockets die mid-flight.  The flag lets a single
    # follow-up pass clean up, then bails so we never recurse indefinitely.
    if dead and not _broadcasting_count:
        _broadcasting_count = True
        try:
            await broadcast_count()
        finally:
            _broadcasting_count = False


async def set_mode(new_mode: str):
    global mode
    mode = new_mode
    await broadcast({"type": "mode", "mode": mode})


async def broadcast_count():
    await broadcast({"type": "crowd_count", "count": len(connections)})


async def _enable_sync():
    global sync_active
    if not sync_active:
        sync_active = True
        await broadcast({"type": "sync_start"})

async def _stop_effects():
    global current_effect_state
    current_effect_state = None
    await set_mode(MODE_WAITING)

game.server_init(
    blink_to_device   = lambda bid: blink_reverse.get(bid),
    connections       = connections,
    positions         = positions,
    blink_assignments = blink_assignments,
    broadcast         = broadcast,
    enable_sync       = _enable_sync,
    stop_effects      = _stop_effects,
    start_effect      = lambda name, params: start_effect(name, params),
)


def _drop_connection(device_id: str):
    """Remove a device from active connections WITHOUT clearing its identity.

    Used on WebSocket disconnect so that a brief reconnect (iOS background,
    network blip) reuses the same blink_id and stored position instead of
    getting a fresh assignment and re-entering detection.

    last_seen is intentionally preserved so the heartbeat reaper can still
    do a full cleanup after HEARTBEAT_TIMEOUT seconds of silence.
    """
    connections.pop(device_id, None)


async def cleanup_device(device_id: str):
    """Full teardown — used by the reaper for permanently gone devices."""
    global _count_dirty
    ws = connections.pop(device_id, None)
    last_seen.pop(device_id, None)
    bid = blink_assignments.pop(device_id, None)
    if bid is not None:
        blink_reverse.pop(bid, None)
        bisect.insort(available_blinks, bid)
    _count_dirty = True   # debounced — reaper can drop many at once
    positions.pop(device_id, None)
    sync_stats.pop(device_id, None)
    if ws:
        try:
            await ws.close()
        except Exception:
            pass


# ------------------------------------------------------------------ #
# Heartbeat reaper                                                     #
# ------------------------------------------------------------------ #

async def heart_broadcast_loop():
    """Batch heart + crowd-count broadcasts — a few per second regardless of tap
    or join/leave rate.  The try/except means a stray broadcast error (e.g. a
    socket dying mid-send) can never terminate this long-lived task."""
    global _heart_dirty, _count_dirty
    while True:
        await asyncio.sleep(0.3)
        try:
            if _heart_dirty:
                _heart_dirty = False
                await broadcast({"type": "like_count", "count": like_count})
            if _count_dirty:
                _count_dirty = False
                await broadcast_count()
        except Exception as e:
            print(f"[heart] broadcast loop error: {e}")


async def reap_dead_clients():
    global _count_dirty
    while True:
        await asyncio.sleep(5)
        try:
            now = time.time()
            for device_id, ts in list(last_seen.items()):
                silent = now - ts
                if silent > IDENTITY_TIMEOUT:
                    # Genuinely gone — recycle the blink_id and forget the seat.
                    print(f"[reaper] forgetting device {device_id[:8]} (silent {int(silent)}s)")
                    await cleanup_device(device_id)
                elif silent > HEARTBEAT_TIMEOUT and device_id in connections:
                    # Soft reap: close the dead socket but KEEP blink_id and
                    # position. Full teardown here made every phone locked
                    # >90s reconnect with calibrated=false — a permanently
                    # black pixel even though the person hadn't moved seats.
                    print(f"[reaper] closing stale socket {device_id[:8]} (identity kept)")
                    ws = connections.pop(device_id)
                    _count_dirty = True
                    asyncio.create_task(_close_quietly(ws))
        except Exception as e:
            print(f"[reaper] loop error: {e}")


# ------------------------------------------------------------------ #
# WebSocket                                                            #
# ------------------------------------------------------------------ #

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global like_count, _heart_dirty, _count_dirty
    await ws.accept()
    device_id = None
    is_spectator = False

    try:
        while True:
            data = await ws.receive_json()

            # Spectator hello — stage page subscribes to broadcasts without
            # being counted as a phone, getting a blink_id, or affecting any
            # game state.  Stage just receives and renders.
            if data.get("type") == "hello" and data.get("role") == "spectator":
                device_id = data["device_id"]
                spectators[device_id] = ws
                is_spectator = True
                await ws.send_json({
                    "type":      "spectator_hello",
                    "build_id":  _app_bundle()[0],
                    "game": {
                        "active":    game.game_active,
                        "mode":      game.game_mode,
                        "positions": {str(b): p for b, p in game.race_positions.items()},
                    },
                })
                continue

            if data.get("type") == "hello":
                device_id = data["device_id"]
                connections[device_id] = ws
                last_seen[device_id] = time.time()

                if device_id not in blink_assignments:
                    if not available_blinks:
                        await ws.send_json({"type": "error", "msg": "no blink IDs available"})
                        await ws.close()
                        return
                    blink_id = available_blinks.pop(0)
                    blink_assignments[device_id] = blink_id
                    blink_reverse[blink_id] = device_id
                else:
                    blink_id = blink_assignments[device_id]

                known_pos = positions.get(device_id)
                pos = known_pos or {"u": 0.0, "v": 0.0}

                # Build ID first — client reloads immediately if stale
                await ws.send_json({"type": "server_hello",
                                    "build_id": _app_bundle()[0]})

                await ws.send_json({
                    "type":       "assigned",
                    "blink_id":   blink_id,
                    "u":          pos["u"],
                    "v":          pos["v"],
                    "calibrated": known_pos is not None,
                })

                _count_dirty = True   # debounced fan-out; new client sees it below
                await ws.send_json({"type": "crowd_count", "count": len(connections)})
                await ws.send_json({"type": "like_count", "count": like_count})

                # Sync current mode / effect so reconnecting clients aren't lost
                if mode == MODE_SHOWTIME and current_effect_state:
                    await ws.send_json(current_effect_state)
                elif mode == MODE_DETECTION:
                    await ws.send_json({"type": "mode", "mode": mode})
                    if detection_active and device_id not in positions:
                        await ws.send_json({"type": "detection_started"})
                    elif device_id in positions:
                        # Already calibrated — re-confirm position regardless of whether
                        # detection is still active.  Belt-and-suspenders alongside the
                        # calibrated:true flag already sent in the assigned message.
                        pos = positions[device_id]
                        await ws.send_json({"type": "update_position", "u": pos["u"], "v": pos["v"]})
                    elif not detection_active:
                        # Detection ended while this phone was disconnected —
                        # send detection_ended so it exits PS.BLINKING cleanly.
                        await ws.send_json({"type": "detection_ended"})
                elif mode == MODE_ENDED and device_id in show_roster:
                    # The show is over and this phone was in it. show_end is a
                    # one-shot broadcast, so a participant that dropped and came
                    # back would otherwise sit on the waiting screen for the rest
                    # of the night instead of their closing card.
                    #
                    # Gated on the roster, because a stranger opening the link
                    # afterwards was getting the same card: congratulated for a
                    # show they were not at, given a phone number they never used
                    # and an em dash where their time should be. They fall through
                    # to "Get ready" instead, which is already true for them and
                    # already correct if the demo runs again.
                    await ws.send_json({
                        "type":            "show_end",
                        "total_connected": show_totals.get("total_connected",
                                                           len(blink_assignments)),
                        "found_ms":        found_ms.get(device_id),
                    })
                # MODE_WAITING: no message needed — client stays on idle screen

                if sync_active:
                    await ws.send_json({"type": "sync_start"})

                # Send current crowd map so late-joining phones see existing positions
                crowd_map = {
                    blink_assignments[dev]: {"u": p["u"], "v": p["v"]}
                    for dev, p in positions.items()
                    if dev in blink_assignments
                }
                if crowd_map:
                    await ws.send_json({"type": "crowd_map", "positions": crowd_map})

            elif data.get("type") == "sync_ping":
                if device_id:
                    last_seen[device_id] = time.time()
                await ws.send_json({
                    "type":        "sync_pong",
                    "client_time": data["client_time"],
                    "server_time": int(time.time() * 1000),
                })

            elif data.get("type") == "sync_report":
                if device_id:
                    sync_stats[device_id] = {
                        "rtt_ms":    data.get("rtt"),
                        "offset_ms": data.get("offset"),
                        "samples":   data.get("samples", 0),
                        "ts":        time.time(),
                    }

            elif data.get("type") == "like_tap":
                if device_id:
                    last_seen[device_id] = time.time()
                if like_enabled:
                    like_count += 1
                    _heart_dirty = True

            elif data.get("type") == "game_tap":
                if device_id:
                    await game.handle_tap(device_id, data.get("reaction_ms", 0))

            elif data.get("type") == "ping":
                if device_id:
                    last_seen[device_id] = time.time()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        # A malformed frame (bad JSON, missing device_id) must still fall through
        # to cleanup below rather than escape and skip it.
        print(f"[ws] handler error: {e}")
    finally:
        # Runs on every exit path — disconnect, the pool-exhaustion early return,
        # or any exception above.  The identity guards are essential: on a
        # reconnect (iOS background / blip) a second socket may have already
        # replaced this device_id in connections; dropping it unconditionally
        # would evict the live socket and leave a zombie that receives nothing.
        if is_spectator and device_id:
            if spectators.get(device_id) is ws:
                spectators.pop(device_id, None)
        elif device_id:
            if connections.get(device_id) is ws:
                _drop_connection(device_id)
                _count_dirty = True


# ------------------------------------------------------------------ #
# Admin — info                                                         #
# ------------------------------------------------------------------ #

@app.get("/admin/clients")
async def get_client_count():
    return {"clients": len(connections)}


@app.get("/admin/blink_map")
async def blink_map():
    """Return blink_id → device_uuid for currently connected clients only."""
    return {"map": {
        str(blink_assignments[dev]): dev
        for dev in connections
        if dev in blink_assignments
    }}


# ------------------------------------------------------------------ #
# Admin — detection                                                    #
# ------------------------------------------------------------------ #
# TODO: after detection, verify all admin requests include the
#       controller's client_id so only the detected controller can
#       post positions/effects (prevents rogue devices spoofing admin).
#
# TODO: camera projection mode — for small groups (~45 phones).
#       Controller samples camera frame at each phone's (u,v) position,
#       POSTs batch {blink_id: [r,g,b]} to /admin/colors, server fans
#       out set_color messages to each phone via WebSocket.
#       Runs at camera framerate (~30fps); consider feedback loop /
#       gain correction to handle phone screen overexposure.

@app.post("/admin/detect")
async def detect(payload: dict):
    """Controller signals detection start/stop."""
    global detection_active, detection_started_at
    detecting = payload.get("detecting", True)
    detection_active = detecting
    if detecting:
        # A new run re-measures everyone. Without the clear, a phone found in
        # the first run would keep that time even if the operator reset and ran
        # detection again.
        detection_started_at = time.time()
        found_ms.clear()
        # A second run is a new show: drop the frozen snapshot with the times,
        # or show_stats would keep serving the previous show's totals and the
        # closing card would go to the previous show's roster.
        show_roster.clear()
        show_totals.clear()
        await set_mode(MODE_DETECTION)
        # Only tell clients who don't yet have a known position to blink;
        # already-found clients get their position re-confirmed (they may have
        # drifted out of PS.FOUND, e.g. after a game round reset).
        sends = [
            (device_id, ws,
             {"type": "detection_started"} if device_id not in positions
             else {"type": "update_position", **positions[device_id]})
            for device_id, ws in list(connections.items())
        ]
    else:
        # Phones that were detected: re-confirm their position so any phone
        # stuck in PS.BLINKING due to a lost update_position gets pushed to
        # PS.FOUND instead of PS.MISSED.
        # Phones that were never detected: send detection_ended so they flash red.
        sends = [
            (device_id, ws,
             {"type": "update_position", **positions[device_id]} if device_id in positions
             else {"type": "detection_ended"})
            for device_id, ws in list(connections.items())
        ]
    sent_ok = await asyncio.gather(*(_timed_send_json(ws, m) for _, ws, m in sends))
    for (device_id, ws, _), ok in zip(sends, sent_ok):
        if not ok:
            _drop_connection(device_id)
            asyncio.create_task(_close_quietly(ws))
    return {"ok": True}


# ------------------------------------------------------------------ #
# Admin — positions (posted by controller after blink detection)       #
# ------------------------------------------------------------------ #

@app.post("/admin/positions")
async def update_positions(payload: dict):
    """
    payload: {"positions": {blink_id_str: {"u": float, "v": float, "confidence": float}}}
    Maps detected blink_ids to device_uuids and broadcasts position updates.
    """
    incoming = payload.get("positions", {})

    # Per-device update_position goes only to the located phone; the global
    # crowd-map fanout used to broadcast once per phone, which made N×M sends
    # for big audiences. Collect and broadcast once at the end instead.
    located_batch: dict[str, dict] = {}

    for bid_str, pos in incoming.items():
        blink_id = int(bid_str)
        device_id = blink_to_device(blink_id)
        if device_id is None:
            continue

        # First location only. The controller re-confirms known positions, and
        # counting those would keep pushing the number up for someone who was
        # found immediately.
        if device_id not in found_ms and detection_started_at is not None:
            found_ms[device_id] = max(0, int((time.time() - detection_started_at) * 1000))

        positions[device_id] = {"u": pos["u"], "v": pos["v"]}

        ws = connections.get(device_id)
        if ws:
            if not await _timed_send_json(ws, {
                "type": "update_position",
                "u":    pos["u"],
                "v":    pos["v"],
            }):
                # Dead or stalled socket — drop it now so the phone reconnects
                # immediately rather than waiting for TCP keepalive.
                _drop_connection(device_id)
                asyncio.create_task(_close_quietly(ws))

        located_batch[str(blink_id)] = {"u": pos["u"], "v": pos["v"]}

    if located_batch:
        await broadcast({
            "type":      "phones_located",
            "positions": located_batch,
        })

    return {"ok": True}


# ------------------------------------------------------------------ #
# Admin — reset                                                        #
# ------------------------------------------------------------------ #

@app.get("/admin/sync_stats")
async def get_sync_stats():
    now = time.time()
    rows = []
    for dev, s in sync_stats.items():
        bid = blink_assignments.get(dev, "?")
        rows.append({
            "device_id": dev[:12],
            "blink_id":  bid,
            "rtt_ms":    s["rtt_ms"],
            "offset_ms": s["offset_ms"],
            "samples":   s["samples"],
            "age_s":     round(now - s["ts"], 1),
        })
    rows.sort(key=lambda r: r["blink_id"] if isinstance(r["blink_id"], int) else 999)
    return {"stats": rows}


@app.post("/admin/sync")
async def sync(payload: dict):
    """Controller enables/disables adaptive clock sync on all clients."""
    global sync_active
    sync_active = payload.get("sync", True)
    msg_type = "sync_start" if sync_active else "sync_stop"
    await broadcast({"type": msg_type})
    return {"ok": True}


@app.get("/admin/show_stats")
async def show_stats():
    """Snapshot of what the show is doing, for the post-show report and for
    anything that wants to display live state without holding a WebSocket
    (the talk deck's join slide reads this every few seconds).

    Public by design - the one entry in _ADMIN_PUBLIC - so it is deliberately
    read-only, cheap (four len() calls and two globals, no locks, no
    iteration) and free of anything identifying: counts and names only, never
    a device_uuid.

    Keys are additive. total_connected/detected/like_count predate the rest
    and are consumed elsewhere, so nothing here is renamed or removed.

    No longer strictly "no iteration": the timings below sort found_ms. That is
    at most a few hundred small ints and this is polled every 3s, so it stays
    cheap, but the claim above was worth correcting rather than leaving to rot.
    """
    # Once the show has ended these are settled facts, and the deck's closing
    # slide reads them while it is on screen. Serving them live would let a
    # latecomer's connection tick the count up under an audience.
    if show_totals:
        return {**show_totals,
                "connected_now": len(connections),
                "spectators":    len(spectators),
                "detecting":     detection_active,
                "effect":        (current_effect_state or {}).get("effect"),
                "effect_started": (current_effect_state or {}).get("start_time"),
                "server_stale":  server_is_stale()}

    _t = sorted(found_ms.values())
    return {
        # Session totals. Cumulative - these only ever go up within a run.
        "like_count":       like_count,
        "total_connected":  len(blink_assignments),   # phones that ever joined
        "detected":         len(positions),           # phones the camera placed

        # Live right now. connected_now falls when a phone locks its screen
        # or walks out, which is why it is separate from total_connected.
        "connected_now":    len(connections),
        "spectators":       len(spectators),

        # What the show is doing. effect_started is the ms timestamp
        # start_effect stamped, so a consumer can tell "wave fired again"
        # from "wave is still playing" - the name alone cannot.
        "detecting":        detection_active,
        "effect":           (current_effect_state or {}).get("effect"),
        "effect_started":   (current_effect_state or {}).get("start_time"),

        # Detection timings, milliseconds from the start of the run to each
        # phone being placed. Derived from found_ms, which is already kept per
        # device for the closing card, so nothing new is stored and nothing
        # identifying is exposed - three numbers, no device ever named.
        #
        # The spread is the interesting part and the reason all three are here:
        # fastest is the protocol floor and lands in the same place every show,
        # while slowest is whatever the room did that night.
        "found_fastest_ms": _t[0]              if _t else None,
        "found_median_ms":  _t[len(_t) // 2]   if _t else None,
        "found_slowest_ms": _t[-1]             if _t else None,

        # True when server.py has been edited since this process started, so
        # whatever is running is not what is on disk. Cheap: one stat().
        "server_stale":     server_is_stale(),
    }


def _mode_snapshot() -> dict:
    return {
        "modes": {
            name: {
                "enabled": mode_requests[name]["enabled"],
                "seq":     mode_requests[name]["seq"],
                "actual":  mode_actual[name],
            }
            for name in MODES
        },
        "supported": list(MODES),
    }


@app.get("/admin/mode")
async def get_mode():
    """Current mode requests and what the controller reports is actually true.

    The controller polls this; a browser can read it to render switch states.
    """
    return _mode_snapshot()


@app.post("/admin/mode")
async def set_mode_request(payload: dict):
    """Turn modes on or off remotely.

    Body is a flat map of mode name to boolean.  Send one or several:

        {"detection": true}
        {"recording": false}
        {"detection": true, "recording": true}

    Requires X-Admin-Token, and is CORS-enabled so a browser on another origin
    can call it (see _ADMIN_CORS).

    Unknown names are rejected as a whole rather than partially applied, so a
    typo fails loudly instead of silently doing half of what was asked.
    Booleans only: "true"/1/"on" are refused, because a string is far more
    likely to be a caller bug than an intent to enable something.

    Returns the same shape as GET.  "actual" will still be the OLD value in
    this response - the controller has not polled yet.  Poll GET to confirm
    the change landed; a request that cannot be honoured (recording with no
    camera) leaves actual disagreeing with enabled.
    """
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(400, "body must be a non-empty object of mode -> bool")

    unknown = [k for k in payload if k not in MODES]
    if unknown:
        raise HTTPException(
            400, f"unknown mode(s): {', '.join(sorted(unknown))}. "
                 f"supported: {', '.join(MODES)}")

    bad = [k for k, v in payload.items() if not isinstance(v, bool)]
    if bad:
        raise HTTPException(
            400, f"mode value must be true or false: {', '.join(sorted(bad))}")

    for name, want in payload.items():
        entry = mode_requests[name]
        # Bump the seq even when the value is unchanged.  "Set recording on"
        # when the server already thinks it is on still has to reach the
        # controller - the controller may have stopped it locally, and this is
        # the caller asking for it back.
        entry["enabled"] = want
        entry["seq"] += 1
        print(f"[mode] request {name}={want} (seq {entry['seq']})", flush=True)

    return _mode_snapshot()


@app.post("/admin/overlays")
async def set_overlays(request: Request):
    """Turn the device overlay on or off. Token-free, but local only.

        curl -X POST http://localhost:8000/admin/overlays \\
             -H 'Content-Type: application/json' -d '{"enabled": true}'

    Exists for the talk deck, which turns overlays on when it reaches the
    camera slide and cannot hold a token that changes every launch. It goes
    through the same seq machinery as /admin/mode, so the controller picks it
    up on its next poll and the two cannot disagree about the ordering.

    Anything arriving through the ngrok tunnel is refused - see
    _is_local_request. This is a stopgap; docs/TODO.md tracks doing it
    properly.

    The body is read raw and parsed here rather than declared as `payload:
    dict`, which would make FastAPI demand Content-Type: application/json.
    That matters: a JSON content type is not CORS-"simple", so the browser
    sends a preflight first, and a preflight from a file:// page to localhost
    is what Chrome's Private Network Access rules block. The deck therefore
    posts as text/plain, which needs no preflight at all - the same reason its
    existing show_stats GET has always worked. Accepting any content type is
    what makes that possible.
    """
    if not _is_local_request(request):
        raise HTTPException(403, "this route is only served to local clients")

    try:
        payload = json.loads(await request.body() or b"")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'body must be JSON: {"enabled": true}')

    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        raise HTTPException(400, 'body must be {"enabled": true} or {"enabled": false}')

    want = payload["enabled"]
    entry = mode_requests["overlays"]
    entry["enabled"] = want
    entry["seq"] += 1
    print(f"[mode] request overlays={want} (seq {entry['seq']}) via /admin/overlays",
          flush=True)
    return _mode_snapshot()


_REC_DIR = os.path.join(_BASE_DIR, "debug", "recordings")


@app.post("/admin/recording")
async def set_recording_request(request: Request):
    """Start or stop the recording. Token-free, but LOCAL ONLY.

        curl -X POST http://localhost:8000/admin/recording \\
             -H 'Content-Type: application/json' -d '{"enabled": true}'

    Same compromise, and the same shape, as /admin/overlays: the recorder lives
    in controller.py, so this only *requests* a state and the controller applies
    it on its next poll through the seq machinery. That is what stops this and
    /admin/mode disagreeing about ordering.

    Body is read raw rather than declared as `payload: dict`, so no JSON
    content type is demanded and no CORS preflight is triggered - see the long
    note on /admin/overlays for why that matters from a file:// page.

    Idempotent at the far end: asking for a state it is already in does
    nothing, so this will not restart a recording and orphan the file being
    written.
    """
    if not _is_local_request(request):
        raise HTTPException(403, "this route is only served to local clients")

    try:
        payload = json.loads(await request.body() or b"")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'body must be JSON: {"enabled": true}')

    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        raise HTTPException(400, 'body must be {"enabled": true} or {"enabled": false}')

    want = payload["enabled"]
    entry = mode_requests["recording"]
    entry["enabled"] = want
    entry["seq"] += 1
    print(f"[mode] request recording={want} (seq {entry['seq']}) via /admin/recording",
          flush=True)
    return _mode_snapshot()


def _is_finalised(path: str, window: int = 262_144) -> bool:
    """True when an mp4 has been closed properly and can actually be played.

    Cheap and structural rather than shelling out to ffprobe per candidate:
    look for the moov atom, which only exists once ffmpeg has written the
    index. With +faststart it ends up near the front, so the head is checked
    first; the tail is checked too so a file written without faststart still
    counts. Reads at most 512KB regardless of how big the recording is.
    """
    try:
        size = os.path.getsize(path)
        if size == 0:
            return False
        with open(path, "rb") as fh:
            if b"moov" in fh.read(window):
                return True
            if size > window:
                fh.seek(-window, os.SEEK_END)
                return b"moov" in fh.read()
    except OSError:
        return False
    return False


@app.get("/admin/recording/latest")
async def latest_recording(request: Request):
    """The most recent finished recording, for the talk deck to play back.

    Token-free but LOCAL ONLY, and the locality check matters more here than
    on the others: this is footage of a room full of people, not a counter.
    Nothing about it should be reachable through the tunnel.

    The latest FINISHED one, which is not the same as the latest. An mp4 being
    written has no moov atom yet - ffmpeg writes the index when it closes, and
    +faststart then moves it to the front - so the file that is growing right
    now is 65MB of frames that nothing can play. Serving it was the whole
    reason this slide came up black: the endpoint was answering 200 with a
    video the browser silently refused.

    So candidates are walked newest first and the first finalised one wins.
    Size alone is not the test - an in-progress recording is the biggest file
    in the directory - and neither is age, because the show being played back
    finished seconds ago.

    FileResponse rather than reading it in: it sets Content-Length and honours
    Range, so the browser can start playing before the whole file has arrived
    and can loop without re-downloading.
    """
    if not _is_local_request(request):
        raise HTTPException(403, "this route is only served to local clients")

    try:
        files = [os.path.join(_REC_DIR, f) for f in os.listdir(_REC_DIR)
                 if f.endswith(".mp4")]
    except FileNotFoundError:
        raise HTTPException(404, "no recordings directory")

    for path in sorted(files, key=os.path.getmtime, reverse=True):
        if _is_finalised(path):
            return FileResponse(path, media_type="video/mp4",
                                headers={"Cache-Control": "no-store"})
    raise HTTPException(404, "no finished recordings yet")


@app.post("/admin/mode/ack")
async def ack_mode(payload: dict):
    """Controller reports what is actually true after applying a request.

    Not for general use - it is how GET /admin/mode can answer "did it work?".
    Token-gated and not CORS-enabled: nothing in a browser should claim to be
    the controller.
    """
    for name, val in (payload or {}).items():
        if name in MODES and isinstance(val, bool):
            mode_actual[name] = val
    return _mode_snapshot()


@app.post("/admin/reset")
async def reset():
    global current_effect_state, detection_active, sync_active
    current_effect_state = None
    detection_active = False
    sync_active = False
    sync_stats.clear()
    positions.clear()
    found_ms.clear()
    show_roster.clear()
    show_totals.clear()
    # Deliberately does NOT clear mode_requests.  Those seq counters are the
    # controller's "have I applied this yet?" cursor, and it holds its own copy
    # in memory.  Zeroing them here would leave the controller's cursor ahead
    # of the server's, so every later request would look stale and silently do
    # nothing until the seq climbed back past it.
    await set_mode(MODE_WAITING)
    await broadcast({"type": "reset"})
    return {"ok": True}


# ------------------------------------------------------------------ #
# Hearts                                                               #
# ------------------------------------------------------------------ #

@app.post("/admin/heart/reset")
async def heart_reset():
    global like_count
    like_count = 0
    await broadcast({"type": "like_count", "count": 0})
    return {"ok": True}

@app.post("/admin/heart/toggle")
async def heart_toggle():
    global like_enabled
    like_enabled = not like_enabled
    return {"ok": True, "enabled": like_enabled}

# ------------------------------------------------------------------ #
# Effects                                                              #
# ------------------------------------------------------------------ #

async def start_effect(effect_name: str, params: dict):
    global current_effect_state
    if game.game_active:
        await game.game_stop()
    await set_mode(MODE_SHOWTIME)
    msg = {
        "type":       "effect",
        "effect":     effect_name,
        "start_time": int(time.time() * 1000),
        **params,
    }
    current_effect_state = msg
    await broadcast(msg)


@app.post("/admin/effect/fire")
async def effect_fire(payload: dict):
    name = payload.get("name", "wave")
    params = {k: v for k, v in payload.items() if k != "name"}
    await start_effect(name, params)
    return {"ok": True}


@app.post("/admin/end")
async def end_show(request: Request):
    """End the show: kill any effect and put every phone on the closing card.

    Token-free but LOCAL ONLY, same compromise as /admin/overlays and for the
    same reason: the talk deck fires this as it leaves the camera slide, and a
    static HTML file cannot hold a token run.sh regenerates every launch.

    This one is a bigger exemption than overlays - overlays flicker, this ends
    the show for the whole room - so the locality check is the only thing
    standing in front of it. Anything arriving through the ngrok tunnel is
    refused. docs/TODO.md tracks doing this properly.

    One call rather than "stop effects, then send the card", because the gap
    between two calls is a gap the room can see - a phone that has gone dark
    and then lights up again reads as a glitch, not an ending.

    The payload is deliberately thin. Everything personal on that card -
    which phone you were, how long you took to find, where you sat - is
    already on the device; only the room total has to come from here.
    """
    if not _is_local_request(request):
        raise HTTPException(403, "this route is only served to local clients")

    global current_effect_state
    current_effect_state = None
    await set_mode(MODE_ENDED)
    await broadcast({"type": "effect_stop"})

    # The closing card is the end of the thing worth filming, so stop the
    # recording with it. Requested rather than done: the recorder lives in
    # controller.py, a separate process, and this is the same seq machinery
    # /admin/mode uses, so the controller applies it on its next poll and the
    # two cannot disagree about ordering. Harmless when nothing is recording -
    # set_recording is idempotent at the other end.
    # Unconditionally, and that matters. mode_requests only records what has
    # been asked for through the mode API; the pedal starts recording by
    # calling set_recording directly in the controller and never touches it.
    # Guarding on entry["enabled"] therefore skipped the stop for every
    # recording the show actually started - which is all of them. The two are
    # kept separate precisely because they disagree, so this asks every time
    # and lets the far end, which is idempotent, decide there is nothing to do.
    entry = mode_requests["recording"]
    entry["enabled"] = False
    entry["seq"] += 1
    print(f"[mode] request recording=False (seq {entry['seq']}) via /admin/end",
          flush=True)

    # Per connection, not a broadcast. found_ms is per device, and a broadcast
    # would hand every phone somebody else's time - the same reason the rest of
    # this payload carries no identifiers.
    total = len(blink_assignments)

    # Freeze before the broadcast, so anyone connecting mid-fan-out is already
    # measured against the finished show rather than a moving one.
    global show_roster, show_totals
    show_roster = set(blink_assignments)
    _t = sorted(found_ms.values())
    show_totals = {
        "like_count":       like_count,
        "total_connected":  total,
        "detected":         len(positions),
        "found_fastest_ms": _t[0]            if _t else None,
        "found_median_ms":  _t[len(_t) // 2] if _t else None,
        "found_slowest_ms": _t[-1]           if _t else None,
    }

    for device_id, ws in list(connections.items()):
        await _timed_send_json(ws, {
            "type":            "show_end",
            "total_connected": total,
            "found_ms":        found_ms.get(device_id),
        })
    print(f"[end] show ended, {total} phones", flush=True)
    return {"ok": True, "total_connected": total}


@app.post("/admin/effect/stop")
async def effect_stop():
    """Clear any currently-broadcast effect.  Audience clients null out
    currentEffect on receipt so phones go dark instead of rendering the
    last frame indefinitely."""
    global current_effect_state
    current_effect_state = None
    await set_mode(MODE_WAITING)
    await broadcast({"type": "effect_stop"})
    return {"ok": True}


@app.post("/admin/proof/wave")
async def wave():
    await start_effect("wave", {"speed": 0.4, "spatial_freq": 1.5})
    return {"ok": True}


@app.post("/admin/proof/gradient")
async def gradient():
    await start_effect("gradient", {"speed": 0.2})
    return {"ok": True}


@app.post("/admin/proof/pulse")
async def pulse():
    await start_effect("pulse", {"bpm": 100})
    return {"ok": True}





# ------------------------------------------------------------------ #
# Static                                                               #
# ------------------------------------------------------------------ #

_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

# Live camera feed: the controller POSTs each encoded frame to
# /admin/feed_frame; viewers get frames pushed the moment they arrive.
# In-memory + event-driven replaces the old shared-file-at-30fps design
# (two stacked 33ms poll cadences and ~8GB/h of /tmp SSD writes).
_feed_frame: bytes | None = None
_feed_event = asyncio.Event()


@app.post("/admin/feed_frame")
async def feed_frame(request: Request):
    # Legacy/fallback ingest - the controller normally pushes frames over
    # /admin/feed_ws and only POSTs here if the WebSocket is unavailable.
    global _feed_frame
    _feed_frame = await request.body()
    _feed_event.set()     # pulse: wake current waiters,
    _feed_event.clear()   # new waiters block until the next frame
    return Response(status_code=204)


@app.websocket("/admin/feed_ws")
async def feed_ws_in(ws: WebSocket):
    """Binary frame ingest from the controller: one persistent socket
    instead of an HTTP POST per frame.  AdminTokenMiddleware is HTTP-only,
    so the token check happens here."""
    if ws.headers.get("x-admin-token") != _ADMIN_TOKEN:
        await ws.close(code=4401)
        return
    await ws.accept()
    global _feed_frame
    try:
        while True:
            _feed_frame = await ws.receive_bytes()
            _feed_event.set()
            _feed_event.clear()
    except WebSocketDisconnect:
        pass


@app.websocket("/internal/feed/ws")
async def feed_ws_out(ws: WebSocket):
    """Push the newest frame to a viewer as it arrives.  A slow viewer
    blocks in send_bytes and misses pulses, so it naturally skips to the
    latest frame instead of building a queue.  The 1s idle re-send keeps
    connections warm while the show is quiet, same as the MJPEG path."""
    await ws.accept()
    try:
        while True:
            if _feed_frame is None:
                await asyncio.sleep(0.2)
                continue
            await ws.send_bytes(_feed_frame)
            try:
                await asyncio.wait_for(_feed_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass


async def _mjpeg_generator():
    """Push each frame to the viewer as it arrives (up to camera rate)."""
    while True:
        if _feed_frame is None:
            await asyncio.sleep(0.2)
            continue
        frame = _feed_frame
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" +
            frame +
            b"\r\n"
        )
        try:
            # Re-send the last frame after 1s of silence so proxies and
            # browsers don't time the stream out while the show is idle.
            await asyncio.wait_for(_feed_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


# Canvas viewer for humans hitting the feed URL directly.  Binary frames
# arrive over the feed WebSocket (no multipart parsing, no per-frame HTTP
# overhead) and only the NEWEST frame is drawn - stale ones are dropped
# by construction, so the view cannot lag.  Auto-reconnects on close.
_FEED_VIEWER_HTML = """<!doctype html><title>pixelmesh feed</title>
<style>html,body{margin:0;height:100%;background:#000;display:grid;
place-items:center}canvas{max-width:100%;max-height:100%}</style>
<canvas id="c"></canvas>
<script>
const c = document.getElementById('c'), ctx = c.getContext('2d');
let latest = null, drawing = false;
async function draw() {
  if (drawing || !latest) return;
  drawing = true;
  const bytes = latest; latest = null;
  try {
    const bm = await createImageBitmap(new Blob([bytes], {type: 'image/jpeg'}));
    if (c.width !== bm.width) { c.width = bm.width; c.height = bm.height; }
    ctx.drawImage(bm, 0, 0);
    bm.close();
  } catch (e) {}
  drawing = false;
  if (latest) requestAnimationFrame(draw);
}
function connect() {
  const ws = new WebSocket(
    (location.protocol === 'https:' ? 'wss://' : 'ws://')
    + location.host + '/internal/feed/ws');
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (ev) => { latest = ev.data; requestAnimationFrame(draw); };
  ws.onclose = () => setTimeout(connect, 1000);
}
connect();
</script>"""


@app.get("/internal/feed/v1")
async def stream(request: Request):
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(content=_FEED_VIEWER_HTML, headers=_NO_CACHE)
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/health")
async def health():
    return {"ok": True}


_APP_HTML_VERSION_RE = re.compile(r'(?:__CACHE_BUST__|[a-f0-9]{32})')

def _render_html(filename: str, build_id: str | None = None) -> str | None:
    """Read a public/ HTML file and stamp the cache-bust token.  Returns None if
    the file is absent, so a missing page never crashes startup."""
    try:
        with open(os.path.join(_PUBLIC_DIR, filename), "r") as f:
            html = f.read()
    except OSError:
        return None
    return _APP_HTML_VERSION_RE.sub(build_id, html) if build_id else html

def _src_mtime(name: str) -> float:
    try:
        return os.stat(os.path.join(_PUBLIC_DIR, name)).st_mtime
    except OSError:
        return 0.0


# app.html is still rendered once and reused - a blocking open()+regex on the
# event loop for every page hit is not acceptable when hundreds of phones load
# "/" at show start. What changed is that the cache is now keyed on the mtimes
# of the files it was built from, so it re-renders when they actually change.
#
# The previous import-time snapshot was a trap. Editing app.js on a running
# server left BUILD_ID frozen, while StaticFiles kept serving the NEW bytes at
# app.js?v=<old id> - a URL marked immutable for a year. Phones pinned a
# mid-edit build permanently. Worse, "/" kept serving the markup as it was at
# startup, so a phone could run new script against old HTML, hit a card that
# did not exist yet, and sit on "Connecting..." forever.
#
# Cost is two stat() calls per page load, against a read+hash+regex avoided.
_APP_CACHE: dict = {"key": None, "build_id": BUILD_ID, "html": None}


def _app_bundle() -> tuple[str, str | None]:
    """(build_id, rendered html), re-derived only when a source file changes."""
    key = (_src_mtime("app.js"), _src_mtime("app.html"))
    if _APP_CACHE["key"] != key:
        bid = _build_id()
        _APP_CACHE.update(key=key, build_id=bid,
                          html=_render_html("app.html", bid))
    return _APP_CACHE["build_id"], _APP_CACHE["html"]


def _serve_app_html():
    return HTMLResponse(content=_app_bundle()[1], headers=_NO_CACHE)


@app.get("/")
async def index():
    return _serve_app_html()


@app.get("/app")
async def app_page():
    return _serve_app_html()


@app.get("/internal/dashboard")
async def dashboard():
    return FileResponse("dashboard.html", headers=_NO_CACHE)


def _stage_build_id() -> str:
    """Hash of stage.js so projector reloads only when the stage page
    changes, independent of audience-client app.js updates."""
    h = hashlib.md5()
    try:
        with open(os.path.join(_PUBLIC_DIR, "stage.js"), "rb") as f:
            h.update(f.read())
    except OSError:
        pass
    return h.hexdigest()[:10]


STAGE_BUILD_ID = _stage_build_id()
_STAGE_HTML = _render_html("stage.html", STAGE_BUILD_ID)
_EVENT_HTML = _render_html("telemetry.html")   # no build id — static page


@app.get("/stage")
async def stage():
    """Full-screen projector page — shows the avatar race track.
    Connects to /ws as a spectator (no blink_id assigned)."""
    return HTMLResponse(content=_STAGE_HTML, headers=_NO_CACHE)


@app.get("/event")
@app.get("/telemetry")
async def event_telemetry_page():
    """Public post-show telemetry page — what the camera actually saw
    at the last live event.  Static page, hosted from public/, references
    pre-rendered SVG charts under /public/stats/.  Aliased at /telemetry
    for engineers and /event for the rest of the world."""
    if _EVENT_HTML is None:
        return HTMLResponse(content="telemetry page not built yet",
                            status_code=404, headers=_NO_CACHE)
    return HTMLResponse(content=_EVENT_HTML, headers=_NO_CACHE)


@app.get("/internal/debug")
async def debug_runs_page():
    import json

    runs = []
    if os.path.isdir(_DEBUG_DIR):
        names = sorted(
            (n for n in os.listdir(_DEBUG_DIR)
             if os.path.isdir(os.path.join(_DEBUG_DIR, n))
             and n not in ("calibration_logs", "recordings", "reports")),
            key=lambda n: os.path.getmtime(os.path.join(_DEBUG_DIR, n)),
            reverse=True,
        )
        for name in names:
            run_dir   = os.path.join(_DEBUG_DIR, name)
            has_video = os.path.isfile(os.path.join(run_dir, "run.mp4"))

            cal_log = ""
            cal_path = os.path.join(run_dir, "calibration.log")
            if os.path.isfile(cal_path):
                try:
                    with open(cal_path) as f:
                        cal_log = f.read().strip()
                except Exception:
                    pass

            frame_count = None
            summary_path = os.path.join(run_dir, "summary.json")
            if os.path.isfile(summary_path):
                try:
                    with open(summary_path) as f:
                        frame_count = json.load(f).get("frames")
                except Exception:
                    pass

            runs.append({
                "name":        name,
                "has_video":   has_video,
                "cal_log":     cal_log,
                "frame_count": frame_count,
                "video_url":   f"/debug-files/{name}/run.mp4",
            })

    return HTMLResponse(_debug_runs_html(runs), headers=_NO_CACHE)


def _debug_runs_html(runs: list) -> str:
    from html import escape

    cards = ""
    for r in runs:
        video_block = ""
        if r["has_video"]:
            video_block = f"""
        <video controls preload="none" poster="">
          <source src="{r['video_url']}" type="video/mp4">
        </video>"""
        else:
            video_block = '<div class="no-video">no video</div>'

        meta = ""
        if r["frame_count"] is not None:
            meta += f'<span>{r["frame_count"]} frames</span>'

        cal_block = ""
        if r["cal_log"]:
            cal_block = f'<pre>{escape(r["cal_log"])}</pre>'

        cards += f"""
    <div class="card">
      <div class="card-head">
        <span class="run-name">{escape(r["name"])}</span>
        <span class="meta">{meta}</span>
      </div>
      {video_block}
      {cal_block}
    </div>"""

    empty = '<p class="empty">No debug runs yet. Press G in the controller to start a capture.</p>' if not runs else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pixelmesh — debug runs</title>
<meta name="theme-color" content="#0d0d0d">
<style>
*, *::before, *::after {{ box-sizing: border-box; }}
html, body {{
  max-width: 100%;
  overflow-x: hidden;
}}
body {{
  margin: 0;
  background: #0d0d0d;
  color: #ccc;
  font: 14px/1.5 -apple-system, system-ui, sans-serif;
  padding: 20px 16px 80px;
}}
h1 {{
  color: #fff;
  font-size: 20px;
  font-weight: 700;
  margin: 0 0 4px;
  letter-spacing: -0.3px;
}}
.subtitle {{
  color: rgba(255,255,255,0.3);
  font-size: 12px;
  margin-bottom: 24px;
}}
.subtitle a {{ color: rgba(255,255,255,0.35); }}
.grid {{
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
}}
@media (min-width: 900px) {{
  body {{ padding: 28px 24px 80px; }}
  .grid {{ grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); }}
}}
.card {{
  background: #1a1a1a;
  border: 1px solid #2a2a2a;
  border-radius: 12px;
  overflow: hidden;
}}
.card-head {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 13px 14px 11px;
  border-bottom: 1px solid #222;
}}
.run-name {{
  color: #fff;
  font-weight: 600;
  font-size: 14px;
  letter-spacing: -0.2px;
  word-break: break-all;
}}
.meta {{
  color: rgba(255,255,255,0.3);
  font-size: 11px;
  white-space: nowrap;
}}
video {{
  display: block;
  width: 100%;
  background: #000;
  max-height: 50vw;
}}
@media (min-width: 900px) {{
  video {{ max-height: 320px; }}
}}
.no-video {{
  padding: 36px;
  text-align: center;
  color: rgba(255,255,255,0.2);
  font-size: 12px;
  background: #111;
}}
pre {{
  margin: 0;
  padding: 12px 14px;
  font: 11px/1.7 "SF Mono", "Fira Mono", ui-monospace, monospace;
  color: rgba(255,255,255,0.45);
  border-top: 1px solid #222;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-wrap: anywhere;
  overflow-x: auto;
  max-width: 100%;
  -webkit-overflow-scrolling: touch;
}}
.empty {{
  color: rgba(255,255,255,0.3);
  font-size: 13px;
  margin-top: 60px;
  text-align: center;
  line-height: 1.8;
}}
</style>
</head>
<body>
<h1>debug runs</h1>
<div class="subtitle">{len(runs)} run{'s' if len(runs) != 1 else ''} · newest first · <a href="/internal/dashboard" style="color:rgba(255,255,255,0.3)">dashboard</a></div>
{empty}
<div class="grid">{cards}</div>
</body>
</html>"""
