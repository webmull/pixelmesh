# (c) Adam Davis - adamdavis.co.uk
"""
pixelmesh — Elgato Camera Hub watchdog

Connects to the undocumented JSON-RPC WebSocket API on localhost:1834.
Polls auto-exposure every 5 seconds and re-disables it if Camera Hub
has flipped it back on. Exposes set_property() for live ISO control.

Runs as a daemon thread — silent if Camera Hub is not open.
"""

import base64
import json
import os
import socket
import threading
import time

from log import log

_PORT             = 1834
_POLL_SECS        = 5.0
_RETRY_SECS       = 30.0
_PROP_AE          = 7
_PROP_GAIN        = 11
_PROP_SHUTTER     = 12
_DEFAULT_GAIN     = 53       # ≈ ISO 624
_DEFAULT_SHUTTER  = 15600    # µs

# Public state — read by controller for UI
connected:  bool = False
ae_on:      bool = False     # True = auto-exposure is currently enabled (bad)
iso_gain:   int  = _DEFAULT_GAIN

# Callback invoked on the watchdog thread when state changes.
# Controller sets this to push updates via ui_queue.
on_state_change = None       # callable() or None

_lock    = threading.Lock()
# Separate lock guarding the full send+recv cycle so concurrent set_property
# calls from UI / MIDI / watchdog threads don't interleave bytes on the socket
# or cross-read responses.
_rpc_lock = threading.Lock()
_rpc_id   = 0      # monotonically increasing JSON-RPC id
_sock    = None
_device  = None


# ------------------------------------------------------------------ #
# WebSocket helpers (no external deps)
# ------------------------------------------------------------------ #

def _ws_connect() -> socket.socket | None:
    key = base64.b64encode(os.urandom(16)).decode()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(("127.0.0.1", _PORT))
        s.send(
            f"GET / HTTP/1.1\r\n"
            f"Host: localhost:{_PORT}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
            .encode()
        )
        s.recv(4096)   # consume HTTP upgrade response
        return s
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return None


def _ws_send(s: socket.socket, payload: bytes):
    mask = os.urandom(4)
    masked = bytes([b ^ mask[i % 4] for i, b in enumerate(payload)])
    length = len(payload)
    if length < 126:
        header = bytes([0x81, 0x80 | length]) + mask
    else:
        header = bytes([0x81, 0xFE, length >> 8, length & 0xFF]) + mask
    s.send(header + masked)


def _recv_exact(s: socket.socket, n: int) -> bytes | None:
    """Read exactly n bytes, or None if the peer closes.  Guards against short
    reads on the 2-/8-byte extended-length fields of a fragmented TCP segment."""
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _ws_send_pong(s: socket.socket, payload: bytes = b""):
    mask = os.urandom(4)
    masked = bytes([b ^ mask[i % 4] for i, b in enumerate(payload)])
    s.send(bytes([0x8A, 0x80 | len(payload)]) + mask + masked)   # FIN + pong, masked


def _ws_recv(s: socket.socket) -> dict | None:
    """Read one JSON message, transparently handling WebSocket control frames.
    Returns the parsed dict, or None only on genuine connection close/loss —
    a ping/pong or non-JSON frame must NOT be reported as a lost connection, or
    the watchdog drops the socket and leaves auto-exposure unguarded for 30s."""
    try:
        while True:
            header = _recv_exact(s, 2)
            if header is None:
                return None
            opcode = header[0] & 0x0F
            b1     = header[1] & 0x7F
            if b1 < 126:
                length = b1
            elif b1 == 126:
                ext = _recv_exact(s, 2)
                length = int.from_bytes(ext, "big") if ext else 0
            else:
                ext = _recv_exact(s, 8)
                length = int.from_bytes(ext, "big") if ext else 0
            data = _recv_exact(s, length) if length else b""
            if data is None:
                return None

            if opcode == 0x8:        # close → treat as connection lost
                return None
            if opcode == 0x9:        # ping → pong and keep reading for the reply
                _ws_send_pong(s, data)
                continue
            if opcode == 0xA:        # pong → ignore, keep reading
                continue
            # Data frame (0x0/0x1/0x2).
            try:
                return json.loads(data)
            except Exception:
                # Non-JSON data frame — not a disconnect; keep waiting for the
                # actual JSON-RPC reply instead of tearing down the socket.
                continue
    except Exception:
        return None


def _rpc(s: socket.socket, method: str, params: dict = {}) -> dict | None:
    global _rpc_id
    with _rpc_lock:
        _rpc_id += 1
        req_id = _rpc_id
        msg = json.dumps({
            "jsonrpc": "2.0", "id": req_id,
            "method": method, "params": params,
        }).encode()
        try:
            _ws_send(s, msg)
            # Camera Hub can push unsolicited event frames, and a stale reply
            # can sit queued from an earlier request. Match replies by id —
            # otherwise one misplaced frame shifts every later reply onto the
            # wrong request and the watchdog misreads AE state forever.
            for _ in range(10):
                resp = _ws_recv(s)
                if resp is None:
                    return None
                if str(resp.get("id")) == str(req_id):
                    return resp
            return None   # stream is garbage — caller reconnects, which resyncs
        except Exception:
            return None


# ------------------------------------------------------------------ #
# Internal helpers
# ------------------------------------------------------------------ #

def _discover_device(s: socket.socket) -> str | None:
    resp = _rpc(s, "getAvailableDevices")
    if not resp:
        return None
    result = resp.get("result") or {}
    devices = (result.get("devices") if isinstance(result, dict) else result) or []
    if not devices:
        return None
    return devices[0].get("deviceID")


def _apply_initial_settings(s: socket.socket, device: str):
    # Re-apply the live gain, not the default — a mid-show reconnect must not
    # silently revert a manually-tuned ISO. First connect is unchanged since
    # iso_gain starts at _DEFAULT_GAIN.
    with _lock:
        gain = iso_gain
    _rpc(s, "setWebcamProperty", {"deviceID": device, "propertyID": _PROP_AE,      "value": 0})
    _rpc(s, "setWebcamProperty", {"deviceID": device, "propertyID": _PROP_GAIN,    "value": gain})
    _rpc(s, "setWebcamProperty", {"deviceID": device, "propertyID": _PROP_SHUTTER, "value": _DEFAULT_SHUTTER})


def _notify():
    cb = on_state_change
    if cb:
        try:
            cb()
        except Exception:
            pass


def _set_connected(val: bool):
    global connected
    with _lock:
        connected = val
    _notify()


# ------------------------------------------------------------------ #
# Public API — called from controller (any thread)
# ------------------------------------------------------------------ #

def set_property(prop_id: int, value: int):
    """Push a property change to Camera Hub. No-op if not connected."""
    global iso_gain, ae_on
    with _lock:
        s      = _sock
        device = _device
    if s is None or device is None:
        return
    try:
        _rpc(s, "setWebcamProperty", {"deviceID": device, "propertyID": prop_id, "value": value})
        if prop_id == _PROP_GAIN:
            with _lock:
                iso_gain = value
        if prop_id == _PROP_AE:
            with _lock:
                ae_on = bool(value)
        _notify()
    except Exception as e:
        log.info(f"[elgato] set_property failed: {e}")


def set_ae(enabled: bool):
    set_property(_PROP_AE, 1 if enabled else 0)


def set_iso(gain: int):
    set_property(_PROP_GAIN, gain)


# ------------------------------------------------------------------ #
# Watchdog thread
# ------------------------------------------------------------------ #

def _watchdog():
    global _sock, _device, connected, ae_on, iso_gain

    while True:
        # --- Connect ---
        s = _ws_connect()
        if s is None:
            _set_connected(False)
            time.sleep(_RETRY_SECS)
            continue

        device = _discover_device(s)
        if device is None:
            s.close()
            _set_connected(False)
            time.sleep(_RETRY_SECS)
            continue

        with _lock:
            _sock   = s
            _device = device
        _apply_initial_settings(s, device)
        with _lock:
            ae_on = False
        _set_connected(True)
        log.info(f"[elgato] connected — device={device} gain={iso_gain}")

        # --- Poll loop ---
        while True:
            time.sleep(_POLL_SECS)
            resp = _rpc(s, "getWebcamProperty",
                        {"deviceID": device, "propertyID": _PROP_AE})
            if resp is None:
                log.info("[elgato] connection lost — retrying")
                break

            result = resp.get("result") if isinstance(resp.get("result"), dict) else {}
            if result.get("propertyID", _PROP_AE) != _PROP_AE:
                continue   # reply names a different property — don't read it as AE
            current_ae = int(result.get("value", 0))
            with _lock:
                was_on = ae_on
                ae_on  = bool(current_ae)

            if current_ae and not was_on:
                log.info("[elgato] AE re-enabled by Camera Hub — forcing off")
                _rpc(s, "setWebcamProperty",
                     {"deviceID": device, "propertyID": _PROP_AE, "value": 0})
                with _lock:
                    ae_on = False
                _notify()
            elif bool(current_ae) != was_on:
                _notify()

        with _lock:
            _sock   = None
            _device = None
        try:
            s.close()
        except Exception:
            pass
        _set_connected(False)
        time.sleep(_RETRY_SECS)


def start():
    t = threading.Thread(target=_watchdog, name="elgato-watchdog", daemon=True)
    t.start()
