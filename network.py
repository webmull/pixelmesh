# (c) Adam Davis — adamdavis.co.uk
import os
import threading
import requests

SERVER_BASE = "http://localhost:8000"

session = requests.Session()
_token = os.environ.get("PIXELMESH_ADMIN_TOKEN", "")
if _token:
    session.headers["X-Admin-Token"] = _token


def post_json(path: str, payload: dict, timeout=0.5) -> bool:
    try:
        r = session.post(f"{SERVER_BASE}{path}", json=payload, timeout=timeout)
        return r.status_code // 100 == 2
    except Exception:
        return False


def post_json_async(path: str, payload: dict):
    threading.Thread(
        target=post_json,
        args=(path, payload, 0.3),
        daemon=True
    ).start()


def fetch_json(path: str, timeout=0.5) -> dict | None:
    try:
        r = session.get(f"{SERVER_BASE}{path}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def fetch_client_count(state):
    try:
        r = session.get(f"{SERVER_BASE}/admin/clients", timeout=0.5)
        if r.status_code == 200:
            with state.lock:
                state.client_count = r.json().get("clients", 0)
    except Exception:
        pass
