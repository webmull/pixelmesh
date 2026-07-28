# (c) Adam Davis - adamdavis.co.uk
import os
from concurrent.futures import ThreadPoolExecutor

import requests

from log import log

SERVER_BASE = "http://localhost:8000"

session = requests.Session()
_token = os.environ.get("PIXELMESH_ADMIN_TOKEN", "")
if _token:
    session.headers["X-Admin-Token"] = _token

# Shared pool for fire-and-forget admin POSTs.  Each call previously spawned a
# fresh Thread; under detection that meant up to a few dozen short-lived threads
# per second.  4 workers is plenty for the localhost-only admin traffic.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="net")


def feed_ws_connect():
    """Open the binary camera-feed WebSocket to the server.  Sync client:
    the controller's stream worker is a plain thread.  Raises on failure -
    the worker retries later and falls back to per-frame HTTP POSTs."""
    from websockets.sync.client import connect
    ws_base = SERVER_BASE.replace("http://", "ws://", 1)
    headers = {"X-Admin-Token": _token} if _token else {}
    return connect(f"{ws_base}/admin/feed_ws",
                   additional_headers=headers,
                   open_timeout=1.0, close_timeout=0.5, max_size=None)


def post_bytes(path: str, data: bytes, timeout=0.3) -> bool:
    """Fire a raw binary POST (camera feed frames). Quiet on failure -
    the stream worker retries with the next frame anyway."""
    try:
        r = session.post(f"{SERVER_BASE}{path}", data=data, timeout=timeout,
                         headers={"Content-Type": "application/octet-stream"})
        return r.status_code // 100 == 2
    except Exception:
        return False


def post_json(path: str, payload: dict, timeout=0.5) -> bool:
    try:
        r = session.post(f"{SERVER_BASE}{path}", json=payload, timeout=timeout)
        if r.status_code // 100 != 2:
            log.warning(f"[net] POST {path} → {r.status_code}")
            return False
        return True
    except Exception as e:
        log.warning(f"[net] POST {path} failed: {e}")
        return False


def post_json_async(path: str, payload: dict):
    _executor.submit(post_json, path, payload, 0.3)


def fetch_json(path: str, timeout=0.5) -> dict | None:
    try:
        r = session.get(f"{SERVER_BASE}{path}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
        log.warning(f"[net] GET {path} → {r.status_code}")
    except Exception as e:
        log.warning(f"[net] GET {path} failed: {e}")
    return None


def fetch_client_count(state):
    try:
        r = session.get(f"{SERVER_BASE}/admin/clients", timeout=0.5)
        if r.status_code == 200:
            with state.lock:
                state.client_count = r.json().get("clients", 0)
    except Exception as e:
        log.debug(f"[net] client count fetch failed: {e}")
