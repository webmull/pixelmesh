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
_enable_sync       = None   # coroutine: ensure clock sync is active
_stop_effects      = None   # coroutine: clear effect state and go to WAITING

_state            = None
_set_status       = None
_post_json        = None
_fetch_json       = None
_detection_order  = None   # ref to controller's _detection_order dict


def server_init(blink_to_device, connections, positions, blink_assignments, broadcast, enable_sync, stop_effects):
    global _blink_to_device, _connections, _positions, _blink_assignments, _broadcast, _enable_sync, _stop_effects
    _blink_to_device   = blink_to_device
    _connections       = connections
    _positions         = positions
    _blink_assignments = blink_assignments
    _broadcast         = broadcast
    _enable_sync       = enable_sync
    _stop_effects      = stop_effects


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

GAME_DURATION_MS = 20_000   # total round length — game always ends after this

game_active:  bool             = False
game_order:   list[int]        = []
game_results: dict[int, float] = {}   # blink_id → reaction_ms
game_slot_ms: int              = 1400  # window for each phone to tap once bug appears

_shown_set:     set            = set()  # blink_ids whose bug has been sent
_phone_tasks:   list           = []     # per-phone delay tasks
_end_task                      = None   # wall-clock game-end task
_countdown_task                = None   # pre-game countdown task


# ------------------------------------------------------------------ #
# Server — internal helpers                                            #
# ------------------------------------------------------------------ #

async def _show_bug(blink_id: int, delay_ms: int):
    """Wait delay_ms then send game_show to this phone."""
    await asyncio.sleep(delay_ms / 1000)
    if not game_active:
        return
    device_id = _blink_to_device(int(blink_id))
    if not device_id or device_id not in _connections:
        return
    show_at = int(time.time() * 1000)
    ws = _connections[device_id]
    try:
        await ws.send_json({
            "type":    "game_show",
            "show_at": show_at,
            "slot_ms": game_slot_ms,
        })
        _shown_set.add(blink_id)
    except Exception:
        pass


async def _game_end_timer():
    """End the game after GAME_DURATION_MS from when it started."""
    await asyncio.sleep(GAME_DURATION_MS / 1000)
    global game_active
    if game_active:
        game_active = False
        await _broadcast_winner()


async def _start_parallel_game():
    """Schedule each phone's bug at a random time within the game window."""
    global _phone_tasks, _end_task, _shown_set
    _shown_set    = set()
    _phone_tasks  = []

    # Each phone gets a random delay so their bug appears at an unpredictable moment.
    # Leave at least slot_ms of window after the bug appears so every phone gets a
    # fair chance to tap before the 20-second round ends.
    max_delay_ms = max(0, GAME_DURATION_MS - game_slot_ms - 500)

    for blink_id in game_order:
        delay_ms = random.randint(0, max_delay_ms)
        task = asyncio.create_task(_show_bug(blink_id, delay_ms))
        _phone_tasks.append(task)

    _end_task = asyncio.create_task(_game_end_timer())
    await _broadcast_progress()


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
    min_ms  = round(min(game_results.values()))
    winners = [bid for bid, ms in game_results.items() if round(ms) == min_ms]
    if len(winners) > 1:
        await _broadcast({
            "type":        "game_winner",
            "draw":        True,
            "blink_ids":   winners,
            "reaction_ms": min_ms,
        })
    else:
        await _broadcast({
            "type":        "game_winner",
            "blink_id":    winners[0],
            "reaction_ms": min_ms,
        })


# ------------------------------------------------------------------ #
# Server — routes                                                      #
# ------------------------------------------------------------------ #

@router.post("/admin/game/start")
async def game_start_endpoint(payload: dict):
    global game_active, game_order, game_results, game_slot_ms, _countdown_task
    # Cancel any in-progress game/countdown
    if _countdown_task and not _countdown_task.done():
        _countdown_task.cancel()
    for t in _phone_tasks:
        if not t.done(): t.cancel()
    if _end_task and not _end_task.done():
        _end_task.cancel()

    game_order   = payload.get("order", [])
    game_slot_ms = int(payload.get("slot_ms", 1400))
    game_active  = True
    game_results = {}
    _countdown_task = asyncio.create_task(_countdown_then_start())
    return {"ok": True}


async def _countdown_then_start():
    if _stop_effects:
        await _stop_effects()
    if _enable_sync:
        await _enable_sync()
    if _broadcast:
        await _broadcast({
            "type":     "game_countdown",
            "start_at": int(time.time() * 1000),
        })
    await asyncio.sleep(3)
    if game_active:
        await _start_parallel_game()


@router.get("/admin/game/results")
async def game_results_endpoint():
    rows = sorted(
        [{"blink_id": bid, "reaction_ms": ms} for bid, ms in game_results.items()],
        key=lambda r: r["reaction_ms"],
    )
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    no_tap = [bid for bid in game_order if bid not in game_results]
    return {"results": rows, "no_tap": no_tap, "active": game_active, "total": len(game_order)}


@router.post("/admin/game/stop")
async def game_stop():
    global game_active, _countdown_task, _end_task
    game_active = False
    if _countdown_task and not _countdown_task.done():
        _countdown_task.cancel()
    _countdown_task = None
    for t in _phone_tasks:
        if not t.done(): t.cancel()
    _phone_tasks.clear()
    if _end_task and not _end_task.done():
        _end_task.cancel()
    _end_task = None
    if _broadcast:
        await _broadcast({"type": "game_end"})
    return {"ok": True}


async def handle_tap(device_id: str, reaction_ms: float):
    """Called (awaited) from the WS handler when a game_tap message arrives."""
    if not game_active:
        return
    blink_id = _blink_assignments.get(device_id)
    if blink_id is None:
        return
    # Only accept taps from phones whose bug has actually appeared
    if blink_id not in _shown_set:
        return
    if blink_id in game_results:
        return
    game_results[blink_id] = float(reaction_ms)
    await _broadcast_progress()


# ------------------------------------------------------------------ #
# Controller — start / poll / leaderboard                             #
# ------------------------------------------------------------------ #

def start_bug_game():
    if not _detection_order:
        _set_status("No detected phones — run detection first")
        return

    ordered = sorted(_detection_order.keys(), key=lambda bid: _detection_order[bid])

    ok = _post_json("/admin/game/start", {"order": ordered, "slot_ms": game_slot_ms})
    if ok:
        _set_status(f"Bug game started — {len(ordered)} phones · {GAME_DURATION_MS // 1000}s round")
        dpg.configure_item("game_leaderboard_window", show=True)
        _start_poll()
    else:
        _set_status("Game start failed")


def _start_poll():
    def _worker():
        for _ in range(GAME_DURATION_MS // 1000 + 10):
            time.sleep(1)
            data = _fetch_json("/admin/game/results")
            if data is None:
                continue
            results = data.get("results", [])
            total   = data.get("total", 0)
            no_tap  = data.get("no_tap", [])
            _update_leaderboard(results, total, no_tap)
            if not data.get("active", True):
                break
    threading.Thread(target=_worker, daemon=True).start()


def _update_leaderboard(results, total, no_tap=None):
    rows = []
    for r in results:
        rows.append(f"#{r['rank']}  Phone {r['blink_id'] + 1}   {r['reaction_ms']:.0f} ms")
    if no_tap:
        if rows:
            rows.append("─" * 26)
        for bid in no_tap:
            rows.append(f"   Phone {bid + 1}   no tap")
    while len(rows) < 12:
        rows.append("")
    try:
        dpg.set_value("game_result_text",  "\n".join(rows[:12]))
        dpg.set_value("game_status_text",  f"{len(results)}/{total} tapped")
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
