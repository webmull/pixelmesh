# (c) Adam Davis - adamdavis.co.uk
"""
PixelMesh V2 — Bug Game

Owns game state, FastAPI routes, and controller UI for the bug-tap reaction game.

Server side:
    game.server_init(blink_to_device, connections, positions, blink_assignments)
    app.include_router(game.router)
    # In the WS handler, call game.handle_tap(device_id, reaction_ms)

Controller side:
    game.init(state, set_status, post_json, fetch_json, detection_order)
    # In setup_ui(), call game.build_sidebar_buttons(indent, pad)
    #                     game.build_window()
"""

import asyncio
import random
import threading
import time

import dearpygui.dearpygui as dpg
from fastapi import APIRouter

router = APIRouter()

# ------------------------------------------------------------------ #
# Wired up by server_init() / init()                                  #
# ------------------------------------------------------------------ #

_blink_to_device   = None   # callable: blink_id → device_uuid | None
_connections       = None   # dict: device_uuid → WebSocket
_positions         = None   # dict: device_uuid → {"u", "v"}
_blink_assignments = None   # dict: device_uuid → blink_id
_broadcast         = None   # coroutine: broadcast(msg) to all phones

_state            = None
_set_status       = None
_post_json        = None
_fetch_json       = None
_detection_order  = None   # ref to controller's _detection_order dict


def server_init(blink_to_device, connections, positions, blink_assignments, broadcast):
    global _blink_to_device, _connections, _positions, _blink_assignments, _broadcast
    _blink_to_device   = blink_to_device
    _connections       = connections
    _positions         = positions
    _blink_assignments = blink_assignments
    _broadcast         = broadcast


def init(state, set_status, post_json, fetch_json, detection_order):
    global _state, _set_status, _post_json, _fetch_json, _detection_order
    _state           = state
    _set_status      = set_status
    _post_json       = post_json
    _fetch_json      = fetch_json
    _detection_order = detection_order


# ------------------------------------------------------------------ #
# Game state                                                           #
# ------------------------------------------------------------------ #

game_active:  bool             = False
game_order:   list[int]        = []
game_results: dict[int, float] = {}   # blink_id → reaction_ms
game_slot_ms: int              = 5000
_current_idx: int              = -1   # index of the phone currently active
_timeout_task                  = None  # asyncio.Task for the current slot timeout


# ------------------------------------------------------------------ #
# Server — internal sequencing                                         #
# ------------------------------------------------------------------ #

async def _advance(idx: int):
    """Show the bug on phone at game_order[idx], or end the game if past the last."""
    global _current_idx, _timeout_task, game_active

    # Cancel any running timeout from the previous slot
    if _timeout_task and not _timeout_task.done():
        _timeout_task.cancel()
    _timeout_task = None

    if idx >= len(game_order):
        game_active = False
        await _broadcast_winner()
        return

    _current_idx = idx
    blink_id  = game_order[idx]
    device_id = _blink_to_device(int(blink_id))

    # Skip phones that aren't connected — advance immediately
    if not device_id or device_id not in _connections:
        await _advance(idx + 1)
        return

    # Random delay so the player can't anticipate the bug
    delay_ms = random.randint(500, 2000)
    show_at  = int(time.time() * 1000) + delay_ms

    ws = _connections[device_id]
    try:
        await ws.send_json({
            "type":    "game_show",
            "show_at": show_at,
            "slot_ms": game_slot_ms,
        })
    except Exception:
        # Phone disconnected between the check and the send — skip it
        await _advance(idx + 1)
        return

    await _broadcast_progress()

    # Advance automatically if the phone doesn't tap within delay + slot_ms
    _timeout_task = asyncio.create_task(_slot_timeout(idx, delay_ms))


async def _slot_timeout(idx: int, delay_ms: int = 0):
    """Fire after delay + slot_ms to move to the next phone if no tap arrived."""
    await asyncio.sleep((delay_ms + game_slot_ms) / 1000)
    if game_active and _current_idx == idx:
        await _advance(idx + 1)


async def _broadcast_progress():
    if _broadcast is None:
        return
    tapped = len(game_results)
    total  = len(game_order)
    await _broadcast({"type": "game_progress", "tapped": tapped, "total": total})


async def _broadcast_winner():
    if _broadcast is None or not game_results:
        await _broadcast({"type": "game_end"}) if _broadcast else None
        return
    winner_bid = min(game_results, key=game_results.get)
    await _broadcast({
        "type":        "game_winner",
        "blink_id":    winner_bid,
        "reaction_ms": round(game_results[winner_bid]),
    })


# ------------------------------------------------------------------ #
# Server — routes                                                      #
# ------------------------------------------------------------------ #

@router.post("/admin/game/start")
async def game_start_endpoint(payload: dict):
    global game_active, game_order, game_results, game_slot_ms, _current_idx
    game_order   = payload.get("order", [])
    game_slot_ms = int(payload.get("slot_ms", 5000))
    game_active  = True
    game_results = {}
    _current_idx = -1
    await _advance(0)
    return {"ok": True}


@router.get("/admin/game/results")
async def game_results_endpoint():
    rows = sorted(
        [{"blink_id": bid, "reaction_ms": ms} for bid, ms in game_results.items()],
        key=lambda r: r["reaction_ms"],
    )
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return {"results": rows, "active": game_active, "total": len(game_order)}


@router.post("/admin/game/stop")
async def game_stop():
    global game_active, _timeout_task
    game_active = False
    if _timeout_task and not _timeout_task.done():
        _timeout_task.cancel()
    _timeout_task = None
    if _broadcast:
        await _broadcast({"type": "game_end"})
    return {"ok": True}


async def handle_tap(device_id: str, reaction_ms: float):
    """Called (awaited) from the WS handler when a game_tap message arrives."""
    global game_active
    if not game_active:
        return
    blink_id = _blink_assignments.get(device_id)
    if blink_id is None:
        return
    # Only accept a tap from the currently active phone
    if _current_idx < 0 or _current_idx >= len(game_order):
        return
    if game_order[_current_idx] != blink_id:
        return
    if blink_id in game_results:
        return
    game_results[blink_id] = float(reaction_ms)
    await _advance(_current_idx + 1)


# ------------------------------------------------------------------ #
# Controller — start / poll / leaderboard                             #
# ------------------------------------------------------------------ #

def start_bug_game():
    if not _detection_order:
        _set_status("No detected phones — run detection first")
        return

    # Only include phones that have a confirmed position
    with _state.lock:
        has_pos = set(_state.calibrated_positions.keys())

    ordered_all = sorted(_detection_order.keys(), key=lambda bid: _detection_order[bid])
    ordered = [bid for bid in ordered_all if bid in has_pos]
    if not ordered:
        _set_status("No positioned phones — complete detection first")
        return

    ok = _post_json("/admin/game/start", {"order": ordered, "slot_ms": 5000})
    if ok:
        _set_status(f"Bug game started — {len(ordered)} phones")
        dpg.configure_item("game_leaderboard_window", show=True)
        _start_poll()
    else:
        _set_status("Game start failed")


def _start_poll():
    def _worker():
        for _ in range(60):   # poll for up to 60 s
            time.sleep(1)
            data = _fetch_json("/admin/game/results")
            if data is None:
                continue
            results = data.get("results", [])
            total   = data.get("total", 0)
            _update_leaderboard(results, total)
            if not data.get("active", True) or (total > 0 and len(results) >= total):
                break
    threading.Thread(target=_worker, daemon=True).start()


def _update_leaderboard(results, total):
    rows = []
    for r in results:
        rows.append(f"#{r['rank']}  Phone {r['blink_id'] + 1}   {r['reaction_ms']:.0f} ms")
    # Pad to keep the window height stable
    while len(rows) < 12:
        rows.append("")
    try:
        dpg.set_value("game_result_text",  "\n".join(rows[:12]))
        dpg.set_value("game_status_text",  f"{len(results)}/{total} phones tapped")
    except Exception:
        pass


# ------------------------------------------------------------------ #
# Controller — UI                                                      #
# ------------------------------------------------------------------ #

def build_sidebar_buttons(indent: int, pad: int):
    """Add the BUG GAME section to the sidebar. Call inside a dpg layout block."""
    dpg.add_spacer(height=4)
    dpg.add_text("BUG GAME", color=(160, 160, 160), indent=indent)
    dpg.add_separator()
    dpg.add_button(label="Start Bug Game",
                   callback=start_bug_game,
                   indent=indent, width=-(pad + 1))
    dpg.add_button(label="Leaderboard",
                   callback=lambda: dpg.configure_item(
                       "game_leaderboard_window",
                       show=not dpg.is_item_shown("game_leaderboard_window"),
                   ),
                   indent=indent, width=-(pad + 1))


def build_window():
    """Create the leaderboard floating window. Call after the main viewport is set up."""
    with dpg.window(tag="game_leaderboard_window", label="Bug Game — Leaderboard",
                    width=340, height=380, pos=(400, 100), show=False,
                    no_collapse=False):
        dpg.add_text("", tag="game_status_text", color=(160, 160, 160))
        dpg.add_separator()
        dpg.add_text("", tag="game_result_text", color=(220, 220, 220))
