# (c) Adam Davis - adamdavis.co.uk
"""
pixelmesh — server

Differences from V1:
- Devices are assigned a small integer blink_id (0-511) instead of a tag image ID.
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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

import game

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


# Public read-only admin routes — exempt from token check.
_ADMIN_PUBLIC = {"/admin/show_stats"}


class AdminTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (request.url.path.startswith("/admin/")
                and request.url.path not in _ADMIN_PUBLIC):
            if request.headers.get("X-Admin-Token") != _ADMIN_TOKEN:
                return Response(status_code=403)
        return await call_next(request)


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

available_blinks = list(range(512))           # pool of unassigned blink IDs

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
                    "build_id":  BUILD_ID,
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
                await ws.send_json({"type": "server_hello", "build_id": BUILD_ID})

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
    global detection_active
    detecting = payload.get("detecting", True)
    detection_active = detecting
    if detecting:
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
    """Snapshot of session-level counters for the post-show report."""
    return {
        "like_count":       like_count,
        "total_connected":  len(blink_assignments),
        "detected":         len(positions),
    }


@app.post("/admin/reset")
async def reset():
    global current_effect_state, detection_active, sync_active
    current_effect_state = None
    detection_active = False
    sync_active = False
    sync_stats.clear()
    positions.clear()
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
    global _feed_frame
    _feed_frame = await request.body()
    _feed_event.set()     # pulse: wake current waiters,
    _feed_event.clear()   # new waiters block until the next frame
    return Response(status_code=204)


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


@app.get("/internal/feed/v1")
async def stream():
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

# Rendered once at import — BUILD_ID is fixed per process, so the result is
# constant.  Avoids a blocking open()+regex on the event loop on every page hit
# (hundreds of phones load "/" simultaneously at show start).
_APP_HTML = _render_html("app.html", BUILD_ID)

def _serve_app_html():
    return HTMLResponse(content=_APP_HTML, headers=_NO_CACHE)


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
