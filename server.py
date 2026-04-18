# (c) Adam Davis — adamdavis.co.uk
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

import bisect
import os
import time
import hashlib
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response as StarletteResponse

_ADMIN_TOKEN = os.environ.get("PIXELMESH_ADMIN_TOKEN", "")


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"]        = "no-cache"
        response.headers["Expires"]       = "0"
        return response
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

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


class AdminTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _ADMIN_TOKEN and request.url.path.startswith("/admin/"):
            if request.headers.get("X-Admin-Token") != _ADMIN_TOKEN:
                return Response(status_code=403)
        return await call_next(request)

app = FastAPI()
app.add_middleware(AdminTokenMiddleware)
app.add_middleware(BlockBotsMiddleware)
app.mount("/public", NoCacheStaticFiles(directory="public"), name="public")

# ------------------------------------------------------------------ #
# Mode constants                                                       #
# ------------------------------------------------------------------ #
MODE_DETECTION = "DETECTION"     # clients blink their ID
MODE_SHOWTIME  = "SHOWTIME"      # clients render effects

mode = MODE_DETECTION

# Hash of client-facing static files — changes when code is deployed.
# Clients reload automatically when this differs from what they loaded with.
def _build_id() -> str:
    h = hashlib.md5()
    base = os.path.join(os.path.dirname(__file__), "public")
    # Only hash app.js — app.html is modified by run.sh cache-busting on every start
    for fname in ("app.js",):
        try:
            with open(os.path.join(base, fname), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:10]

BUILD_ID = _build_id()

# Last-broadcast effect, replayed to clients that connect mid-session.
current_effect_state: dict | None = None

# Whether the controller has actively started detection (distinct from mode).
detection_active = False

# Whether the controller has enabled clock sync.
sync_active = False

# ------------------------------------------------------------------ #
# State                                                                #
# ------------------------------------------------------------------ #
connections:       dict[str, WebSocket] = {}   # device_uuid → ws
blink_assignments: dict[str, int]       = {}   # device_uuid → blink_id
blink_reverse:     dict[int, str]       = {}   # blink_id    → device_uuid
positions:         dict[str, dict]      = {}   # device_uuid → {"u", "v"}
last_seen:         dict[str, float]     = {}   # device_uuid → timestamp
sync_stats:        dict[str, dict]      = {}   # device_uuid → {rtt_ms, offset_ms, samples, ts}

available_blinks = list(range(512))           # pool of unassigned blink IDs

HEARTBEAT_TIMEOUT = 90   # seconds


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def blink_to_device(blink_id: int) -> str | None:
    return blink_reverse.get(blink_id)


async def broadcast(message: dict):
    dead = []
    for device_id, ws in connections.items():
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(device_id)
    for device_id in dead:
        await cleanup_device(device_id)


async def set_mode(new_mode: str):
    global mode
    mode = new_mode
    await broadcast({"type": "mode", "mode": mode})


async def cleanup_device(device_id: str):
    ws = connections.pop(device_id, None)
    last_seen.pop(device_id, None)
    bid = blink_assignments.pop(device_id, None)
    if bid is not None:
        blink_reverse.pop(bid, None)
        bisect.insort(available_blinks, bid)
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

async def reap_dead_clients():
    while True:
        await asyncio.sleep(5)
        now = time.time()
        dead = [d for d, ts in list(last_seen.items()) if now - ts > HEARTBEAT_TIMEOUT]
        for device_id in dead:
            print(f"[reaper] removing stale device {device_id[:8]}")
            await cleanup_device(device_id)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(reap_dead_clients())


@app.on_event("shutdown")
async def shutdown_event():
    await broadcast({"type": "shutdown"})


# ------------------------------------------------------------------ #
# WebSocket                                                            #
# ------------------------------------------------------------------ #

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    device_id = None

    try:
        while True:
            data = await ws.receive_json()

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

                # Sync current mode / effect so reconnecting clients aren't lost
                if mode == MODE_SHOWTIME and current_effect_state:
                    await ws.send_json(current_effect_state)
                else:
                    await ws.send_json({"type": "mode", "mode": mode})
                    if detection_active:
                        await ws.send_json({"type": "detection_started"})

                if sync_active:
                    await ws.send_json({"type": "sync_start"})

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

            elif data.get("type") == "ping":
                if device_id:
                    last_seen[device_id] = time.time()

    except WebSocketDisconnect:
        if device_id:
            await cleanup_device(device_id)


# ------------------------------------------------------------------ #
# Admin — info                                                         #
# ------------------------------------------------------------------ #

@app.get("/admin/clients")
async def get_client_count():
    return {"clients": len(connections)}


@app.get("/admin/blink_map")
async def blink_map():
    """Return mapping blink_id → device_uuid (for controller reference)."""
    return {"map": {str(bid): dev for dev, bid in blink_assignments.items()}}


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
        await broadcast({"type": "detection_started"})
    else:
        await broadcast({"type": "detection_ended"})
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

    for bid_str, pos in incoming.items():
        blink_id = int(bid_str)
        device_id = blink_to_device(blink_id)
        if device_id is None:
            continue

        positions[device_id] = {"u": pos["u"], "v": pos["v"]}

        ws = connections.get(device_id)
        if ws:
            try:
                await ws.send_json({
                    "type": "update_position",
                    "u":    pos["u"],
                    "v":    pos["v"],
                })
            except Exception:
                pass

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


@app.post("/admin/reset")
async def reset():
    global current_effect_state, detection_active, sync_active
    current_effect_state = None
    detection_active = False
    sync_active = False
    sync_stats.clear()
    await set_mode(MODE_DETECTION)
    await broadcast({"type": "reset"})
    return {"ok": True}


# ------------------------------------------------------------------ #
# Effects                                                              #
# ------------------------------------------------------------------ #

async def start_effect(effect_name: str, params: dict):
    global current_effect_state
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


@app.post("/admin/proof/wave")
async def wave():
    await start_effect("wave", {"speed": 0.4, "spatial_freq": 1.5})
    return {"ok": True}


@app.post("/admin/proof/gradient")
async def gradient():
    await start_effect("gradient", {"speed": 0.2})
    return {"ok": True}


@app.post("/admin/proof/binary")
async def binary_wave():
    await start_effect("binary_wave", {"speed": 0.2, "spatial_freq": 1.5})
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

_STREAM_PATH     = "/tmp/pixelmesh_stream.jpg"
_STREAM_INTERVAL = 1.0 / 30   # 30fps per connection

async def _mjpeg_generator():
    """Yield MJPEG frames from the shared JPEG file written by controller."""
    last_sent = 0.0
    while True:
        now = time.time()
        wait = _STREAM_INTERVAL - (now - last_sent)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            with open(_STREAM_PATH, "rb") as f:
                frame = f.read()
        except FileNotFoundError:
            await asyncio.sleep(0.5)
            continue
        last_sent = time.time()
        yield (
            b"--frame
"
            b"Content-Type: image/jpeg

" +
            frame +
            b"
"
        )

@app.get("/internal/feed/v1")
async def stream():
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse("public/app.html", headers=_NO_CACHE)


@app.get("/app")
async def app_page():
    return FileResponse("public/app.html", headers=_NO_CACHE)


@app.get("/internal/dashboard")
async def dashboard():
    return FileResponse("dashboard.html", headers=_NO_CACHE)


@app.get("/internal/sim")
async def sim():
    return FileResponse("public/sim.html", headers=_NO_CACHE)
