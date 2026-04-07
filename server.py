"""
PixelMesh V2 — Server

Differences from V1:
- Devices are assigned a small integer blink_id (0-31) instead of a tag image ID.
- Positions come from the controller's blink detection, not AprilTag calibration.
- No tag-image or projection endpoints.
- Adds /admin/positions  (controller posts detected blink_id → u,v)
- Adds /admin/blink_map  (controller reads blink_id → device_uuid mapping)
- Adds /admin/detect     (controller signals detection on/off; server tells clients)
"""

import time
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response as StarletteResponse


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"]        = "no-cache"
        response.headers["Expires"]       = "0"
        return response
from fastapi.responses import FileResponse, Response
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

app = FastAPI()
app.add_middleware(BlockBotsMiddleware)
app.mount("/public", NoCacheStaticFiles(directory="public"), name="public")

# ------------------------------------------------------------------ #
# Mode constants                                                       #
# ------------------------------------------------------------------ #
MODE_DETECTION = "DETECTION"     # clients blink their ID
MODE_SHOWTIME  = "SHOWTIME"      # clients render effects

mode = MODE_DETECTION

# ------------------------------------------------------------------ #
# State                                                                #
# ------------------------------------------------------------------ #
connections:       dict[str, WebSocket] = {}   # device_uuid → ws
blink_assignments: dict[str, int]       = {}   # device_uuid → blink_id
positions:         dict[str, dict]      = {}   # device_uuid → {"u", "v"}
last_seen:         dict[str, float]     = {}   # device_uuid → timestamp

available_blinks = list(range(32))            # pool of unassigned blink IDs

HEARTBEAT_TIMEOUT = 30   # seconds


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def blink_to_device(blink_id: int) -> str | None:
    """Reverse lookup: blink_id → device_uuid."""
    for dev, bid in blink_assignments.items():
        if bid == blink_id:
            return dev
    return None


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
        available_blinks.append(bid)
    positions.pop(device_id, None)
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
                else:
                    blink_id = blink_assignments[device_id]

                pos = positions.get(device_id, {"u": 0.0, "v": 0.0})

                await ws.send_json({
                    "type":     "assigned",
                    "blink_id": blink_id,
                    "u":        pos["u"],
                    "v":        pos["v"],
                })

                # Tell the client which mode we're currently in
                await ws.send_json({"type": "mode", "mode": mode})

            elif data.get("type") == "sync_ping":
                if device_id:
                    last_seen[device_id] = time.time()
                await ws.send_json({
                    "type":        "sync_pong",
                    "client_time": data["client_time"],
                    "server_time": int(time.time() * 1000),
                })

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

@app.post("/admin/detect")
async def detect(payload: dict):
    """Controller signals detection start/stop."""
    detecting = payload.get("detecting", True)
    if detecting:
        await set_mode(MODE_DETECTION)
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

@app.post("/admin/reset")
async def reset():
    await set_mode(MODE_DETECTION)
    await broadcast({"type": "reset"})
    return {"ok": True}


# ------------------------------------------------------------------ #
# Effects                                                              #
# ------------------------------------------------------------------ #

async def start_effect(effect_name: str, params: dict):
    await set_mode(MODE_SHOWTIME)
    await broadcast({
        "type":       "effect",
        "effect":     effect_name,
        "start_time": int(time.time() * 1000),
        **params,
    })


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


@app.post("/admin/proof/click")
async def click(payload: dict):
    await start_effect("click_ripple", {
        "origin_u": payload.get("u", 0.5),
        "origin_v": payload.get("v", 0.5),
        "speed":    1.0,
    })
    return {"ok": True}


@app.post("/admin/proof/sweep_bar")
async def sweep_bar():
    now = int(time.time() * 1000)

    # Sort devices by u-position
    devices = []
    for device_id in connections:
        pos = positions.get(device_id)
        if pos:
            devices.append((device_id, pos["u"]))
    devices.sort(key=lambda x: x[1])

    device_ids = [d for d, _ in devices]

    await broadcast({
        "type":         "effect",
        "effect":       "sweep_bar",
        "start_time":   now,
        "device_order": device_ids,
        "dwell":        0.18,
    })
    return {"ok": True}


# ------------------------------------------------------------------ #
# Static                                                               #
# ------------------------------------------------------------------ #

_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

@app.get("/")
async def index():
    return FileResponse("public/app.html", headers=_NO_CACHE)


@app.get("/sim")
async def sim():
    return FileResponse("public/sim.html", headers=_NO_CACHE)
