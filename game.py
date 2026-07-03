# (c) Adam Davis - adamdavis.co.uk
"""
PixelMesh V2 — Audience games (dual-mode)

Two games sit behind the same /admin/game/* endpoints, dispatched by
game_mode set on round start:

  Game 1 — "bug"  : reaction-time tap.  Each phone gets a randomly-timed
                    bug to tap; fastest wins, results compared across phones.

  Game 2 — "race" : avatar race.  Each phone gets a procedurally-generated
                    character on the stage projection and races left→right
                    by tapping.  First to 100% wins; the rest of the field
                    keeps running so everyone sees where they finished.

When the operator manually stops a round the server fires a calm default
wave via the start_effect callback so phones aren't stuck on the game
card.  A natural game finish leaves the winner screen up until the
operator chooses the next move.

Server side:
    game.server_init(blink_to_device, connections, positions, blink_assignments,
                     broadcast, enable_sync, stop_effects, start_effect)
    app.include_router(game.router)
    # In the WS handler, call game.handle_tap(device_id, reaction_ms)

Controller side:
    game.init(state, set_status, post_json, fetch_json, render_order)
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
# Wiring                                                              #
# ------------------------------------------------------------------ #

_blink_to_device   = None
_connections       = None
_positions         = None
_blink_assignments = None
_broadcast         = None
_enable_sync       = None
_stop_effects      = None
_start_effect      = None

_state            = None
_set_status       = None
_post_json        = None
_fetch_json       = None
_render_order     = None
_ui_queue         = None    # controller's DPG queue — poll worker routes UI ops here


def server_init(blink_to_device, connections, positions, blink_assignments,
                broadcast, enable_sync, stop_effects, start_effect=None):
    global _blink_to_device, _connections, _positions, _blink_assignments
    global _broadcast, _enable_sync, _stop_effects, _start_effect
    _blink_to_device   = blink_to_device
    _connections       = connections
    _positions         = positions
    _blink_assignments = blink_assignments
    _broadcast         = broadcast
    _enable_sync       = enable_sync
    _stop_effects      = stop_effects
    _start_effect      = start_effect


def init(state, set_status, post_json, fetch_json, render_order, ui_queue=None):
    global _state, _set_status, _post_json, _fetch_json, _render_order, _ui_queue
    _state        = state
    _set_status   = set_status
    _post_json    = post_json
    _fetch_json   = fetch_json
    _render_order = render_order
    _ui_queue     = ui_queue


# ------------------------------------------------------------------ #
# Shared state                                                         #
# ------------------------------------------------------------------ #

GAME_MODE_BUG  = "bug"
GAME_MODE_RACE = "race"

game_mode:   str  = GAME_MODE_RACE
game_active: bool = False

# Back-compat shims so report.py and the controller's _save_report keep
# working — these reflect bug-game results only.
game_results: dict[int, float] = {}
game_order:   list[int]        = []


# ------------------------------------------------------------------ #
# Bug-game state + tunables                                            #
# ------------------------------------------------------------------ #

BUG_GAME_DURATION_MS = 20_000   # total round length
bug_slot_ms:    int            = 1400   # window for each phone to tap once its bug appears

_bug_shown_set:    set         = set()  # blink_ids whose bug has been sent
_bug_missed_set:   set         = set()  # blink_ids whose slot expired without a tap
_bug_phone_tasks:  list        = []
_bug_end_task                  = None
_bug_countdown_task            = None


# ------------------------------------------------------------------ #
# Race-game state + tunables                                           #
# ------------------------------------------------------------------ #

# Target taps per phone to cross the finish line.  Higher = longer round,
# more chance to read the leader board on stage.
RACE_TAPS_PER_PLAYER     = 40
RACE_PER_TAP_INTERVAL    = 0.08   # ~12 taps/s ceiling per phone
RACE_PROGRESS_INTERVAL   = 0.10   # 10 Hz broadcast

race_positions:  dict[int, float] = {}        # blink_id → 0.0–1.0
race_taps_total: dict[int, int]   = {}        # blink_id → cumulative taps
race_winner:     int | None       = None
race_start_at:   float            = 0.0
race_end_at:     float            = 0.0

_race_last_tap_at:   dict[str, float] = {}
_race_progress_task: asyncio.Task | None = None


# ------------------------------------------------------------------ #
# Shared endpoints — dispatch by mode                                  #
# ------------------------------------------------------------------ #

@router.post("/admin/game/start")
async def game_start(payload: dict):
    """Start a round.  payload selects the mode:
        {"mode": "race", "blink_ids": [<int>, ...]}
        {"mode": "bug",  "order": [<blink_id>, ...], "slot_ms": int}
    Default mode if absent is "race"."""
    global game_mode
    mode = payload.get("mode", GAME_MODE_RACE)
    if mode == GAME_MODE_BUG:
        game_mode = GAME_MODE_BUG
        return await _start_bug_round(payload)
    game_mode = GAME_MODE_RACE
    return await _start_race_round(payload)


@router.post("/admin/game/stop")
async def game_stop():
    """Abort the current round (whichever mode)."""
    if game_mode == GAME_MODE_BUG:
        return await _stop_bug_round()
    return await _stop_race_round()


@router.get("/admin/game/state")
async def game_state():
    """Snapshot used by the controller's poll loop.  Shape varies by mode
    so the controller poll knows which sidebar fields to update."""
    if game_mode == GAME_MODE_BUG:
        return _bug_state_snapshot()
    return _race_state_snapshot()


@router.get("/admin/game/results")
async def game_results_endpoint():
    """Legacy bug-game results endpoint — only useful in bug mode."""
    if game_mode != GAME_MODE_BUG:
        return {"results": [], "no_tap": [], "active": False, "total": 0}
    rows = sorted(
        [{"blink_id": bid, "reaction_ms": ms} for bid, ms in game_results.items()],
        key=lambda r: r["reaction_ms"],
    )
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    no_tap = [bid for bid in game_order if bid not in game_results]
    return {"results": rows, "no_tap": no_tap, "active": game_active,
            "total": len(game_order)}


async def handle_tap(device_id: str, reaction_ms: float = 0.0):
    """Routed from the WS handler.  Dispatches by current game_mode."""
    if not game_active:
        return
    if game_mode == GAME_MODE_BUG:
        await _bug_handle_tap(device_id, reaction_ms)
    else:
        await _race_handle_tap(device_id)


# ------------------------------------------------------------------ #
# Bug game — server                                                    #
# ------------------------------------------------------------------ #

async def _start_bug_round(payload: dict) -> dict:
    global game_active, game_order, game_results, bug_slot_ms
    global _bug_countdown_task

    # Cancel anything still running
    if _bug_countdown_task and not _bug_countdown_task.done():
        _bug_countdown_task.cancel()
    for t in _bug_phone_tasks:
        if not t.done():
            t.cancel()
    if _bug_end_task and not _bug_end_task.done():
        _bug_end_task.cancel()
    await _cancel_race_progress()

    game_order   = list(payload.get("order", []))
    bug_slot_ms  = int(payload.get("slot_ms", 1400))
    game_active  = True
    game_results = {}
    _bug_countdown_task = asyncio.create_task(_bug_countdown_then_start())
    return {"ok": True}


async def _bug_countdown_then_start():
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
        await _bug_start_parallel()


async def _bug_start_parallel():
    """Schedule each phone's bug at a random time within the game window."""
    global _bug_phone_tasks, _bug_end_task, _bug_shown_set, _bug_missed_set
    _bug_shown_set    = set()
    _bug_missed_set   = set()
    _bug_phone_tasks  = []

    # Leave at least slot_ms of window after the bug appears so every phone
    # gets a fair chance to tap before the round-end timer fires.
    max_delay_ms = max(0, BUG_GAME_DURATION_MS - bug_slot_ms - 500)

    for blink_id in game_order:
        delay_ms = random.randint(0, max_delay_ms)
        task = asyncio.create_task(_bug_show(blink_id, delay_ms))
        _bug_phone_tasks.append(task)

    _bug_end_task = asyncio.create_task(_bug_end_timer())
    await _bug_broadcast_progress()


async def _bug_show(blink_id: int, delay_ms: int):
    """Wait delay_ms then send game_show to this phone."""
    await asyncio.sleep(delay_ms / 1000)
    if not game_active:
        return
    device_id = _blink_to_device(int(blink_id))
    if not device_id or device_id not in _connections:
        _bug_missed_set.add(blink_id)
        await _bug_check_finish_early()
        return
    show_at = int(time.time() * 1000)
    ws = _connections[device_id]
    try:
        await ws.send_json({
            "type":    "game_show",
            "show_at": show_at,
            "slot_ms": bug_slot_ms,
        })
        _bug_shown_set.add(blink_id)
    except Exception:
        _bug_missed_set.add(blink_id)
        await _bug_check_finish_early()
        return
    _bug_phone_tasks.append(asyncio.create_task(_bug_slot_expiry(blink_id)))


async def _bug_slot_expiry(blink_id: int):
    """Wait one slot; if the phone hasn't tapped, count it as missed."""
    await asyncio.sleep(bug_slot_ms / 1000)
    if not game_active:
        return
    if blink_id in game_results:
        return
    _bug_missed_set.add(blink_id)
    await _bug_check_finish_early()


async def _bug_check_finish_early():
    """End the round as soon as every phone has either tapped or missed."""
    global game_active, _bug_end_task
    if not game_active:
        return
    total = len(game_order)
    if total == 0:
        return
    if len(game_results) + len(_bug_missed_set) < total:
        return
    game_active = False
    if _bug_end_task and not _bug_end_task.done():
        _bug_end_task.cancel()
    await _bug_broadcast_winner()


async def _bug_end_timer():
    """Wall-clock fallback — ends the round after BUG_GAME_DURATION_MS."""
    await asyncio.sleep(BUG_GAME_DURATION_MS / 1000)
    global game_active
    if game_active:
        game_active = False
        await _bug_broadcast_winner()


async def _bug_broadcast_progress():
    if _broadcast is None:
        return
    await _broadcast({
        "type":   "game_progress",
        "tapped": len(game_results),
        "total":  len(game_order),
    })


async def _bug_broadcast_winner():
    if _broadcast is None:
        return
    if not game_results:
        await _broadcast({"type": "game_end"})
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
    # No auto default-wave: the winner screen stays up until the operator
    # fires the next effect or starts a new game.


async def _stop_bug_round() -> dict:
    global game_active, _bug_countdown_task, _bug_end_task
    if not game_active and _bug_countdown_task is None:
        return {"ok": True}
    game_active = False
    if _bug_countdown_task and not _bug_countdown_task.done():
        _bug_countdown_task.cancel()
    _bug_countdown_task = None
    for t in _bug_phone_tasks:
        if not t.done():
            t.cancel()
    _bug_phone_tasks.clear()
    if _bug_end_task and not _bug_end_task.done():
        _bug_end_task.cancel()
    _bug_end_task = None
    if _broadcast:
        await _broadcast({"type": "game_end"})
    await _start_default_wave()
    return {"ok": True}


async def _bug_handle_tap(device_id: str, reaction_ms: float):
    blink_id = _blink_assignments.get(device_id)
    if blink_id is None:
        return
    if blink_id not in _bug_shown_set:
        return
    if blink_id in game_results:
        return
    if blink_id in _bug_missed_set:
        # Slot already lapsed and was counted as missed — a late tap must not
        # record a result (it would double-count in the finish tally and could
        # even let a visibly-missed phone win).
        return
    game_results[blink_id] = float(reaction_ms)
    await _bug_broadcast_progress()
    await _bug_check_finish_early()


def _bug_state_snapshot() -> dict:
    return {
        "mode":   GAME_MODE_BUG,
        "active": game_active,
        "tapped": len(game_results),
        "total":  len(game_order),
        "missed": len(_bug_missed_set),
        "shown":  len(_bug_shown_set),
    }


# ------------------------------------------------------------------ #
# Race game — server                                                   #
# ------------------------------------------------------------------ #

async def _start_race_round(payload: dict) -> dict:
    """Each phone in blink_ids starts at position 0.0.  Every tap nudges
    the phone forward by 1/RACE_TAPS_PER_PLAYER.  First to 1.0 wins."""
    global game_active, race_positions, race_taps_total
    global race_winner, race_start_at, race_end_at
    global _race_progress_task, _race_last_tap_at
    global game_order, game_results

    # Cancel anything still running in the other modes
    await _cancel_bug_tasks()
    await _cancel_race_progress()

    # Clear bug-game back-compat fields so a stale round doesn't leak
    # into the post-show report.
    game_order   = []
    game_results = {}

    blink_ids = [int(b) for b in payload.get("blink_ids", [])]
    race_positions    = {bid: 0.0 for bid in blink_ids}
    race_taps_total   = {bid: 0   for bid in blink_ids}
    race_winner       = None
    race_start_at     = time.time()
    race_end_at       = 0.0
    _race_last_tap_at = {}
    game_active       = True

    if _stop_effects:
        await _stop_effects()
    if _enable_sync:
        await _enable_sync()

    if _broadcast:
        await _broadcast({
            "type":      "race_start",
            "blink_ids": blink_ids,
            "start_at":  int(race_start_at * 1000),
        })

    _race_progress_task = asyncio.create_task(_race_progress_loop())
    return {"ok": True}


async def _stop_race_round() -> dict:
    global game_active, _race_progress_task
    if not game_active and _race_progress_task is None:
        return {"ok": True}
    game_active = False
    if _race_progress_task and not _race_progress_task.done():
        _race_progress_task.cancel()
    _race_progress_task = None
    if _broadcast:
        await _broadcast({
            "type":      "race_end",
            "winner":    None,
            "positions": {str(b): round(p, 4) for b, p in race_positions.items()},
        })
    await _start_default_wave()
    return {"ok": True}


async def _race_handle_tap(device_id: str):
    blink_id = _blink_assignments.get(device_id)
    if blink_id is None:
        return
    if blink_id not in race_positions:
        return
    if race_winner is not None:
        return
    now = time.time()
    last = _race_last_tap_at.get(device_id, 0.0)
    if now - last < RACE_PER_TAP_INTERVAL:
        return
    _race_last_tap_at[device_id] = now
    race_positions[blink_id]  = min(1.0, race_positions[blink_id] + 1.0 / RACE_TAPS_PER_PLAYER)
    race_taps_total[blink_id] += 1
    if race_positions[blink_id] >= 1.0:
        await _race_finish(blink_id)


async def _race_progress_loop():
    last_sent = None
    try:
        while game_active:
            await asyncio.sleep(RACE_PROGRESS_INTERVAL)
            if _broadcast:
                positions = {str(b): round(p, 4) for b, p in race_positions.items()}
                # Skip fan-out to every phone when nothing moved this tick.
                # (Serialization is already done once per broadcast, not per
                # client, in server.broadcast.)
                if positions == last_sent:
                    continue
                last_sent = positions
                await _broadcast({"type": "race_progress", "positions": positions})
    except asyncio.CancelledError:
        pass


async def _race_finish(winner_bid: int):
    global game_active, race_winner, race_end_at, _race_progress_task
    race_winner = winner_bid
    race_end_at = time.time()
    game_active = False
    if _race_progress_task and not _race_progress_task.done():
        _race_progress_task.cancel()
    _race_progress_task = None
    if _broadcast:
        await _broadcast({
            "type":      "race_end",
            "winner":    int(winner_bid),
            "positions": {str(b): round(p, 4) for b, p in race_positions.items()},
        })
    # Winner banner stays up until the operator fires the next thing.


async def _cancel_race_progress():
    global _race_progress_task
    if _race_progress_task and not _race_progress_task.done():
        _race_progress_task.cancel()
    _race_progress_task = None


def _race_state_snapshot() -> dict:
    leader_bid = None
    leader_pos = -1.0
    for bid, pos in race_positions.items():
        if pos > leader_pos:
            leader_pos = pos
            leader_bid = bid
    return {
        "mode":      GAME_MODE_RACE,
        "active":    game_active,
        "positions": race_positions,
        "taps":      race_taps_total,
        "winner":    race_winner,
        "leader":    leader_bid,
        "leader_pos": max(0.0, leader_pos),
        "runners":   len(race_positions),
    }


# ------------------------------------------------------------------ #
# Shared helpers                                                       #
# ------------------------------------------------------------------ #

async def _cancel_bug_tasks():
    global _bug_countdown_task, _bug_end_task
    if _bug_countdown_task and not _bug_countdown_task.done():
        _bug_countdown_task.cancel()
    _bug_countdown_task = None
    for t in _bug_phone_tasks:
        if not t.done():
            t.cancel()
    _bug_phone_tasks.clear()
    if _bug_end_task and not _bug_end_task.done():
        _bug_end_task.cancel()
    _bug_end_task = None


async def _start_default_wave():
    """Fire a calm wave so audience phones aren't stuck on the game card."""
    if _start_effect is None:
        return
    await _start_effect("wave", {
        "speed":        0.25,
        "spatial_freq": 1.5,
        "angle":        0.0,
        "bpm":          100.0,
        "split":        0.5,
        "color_r":      180,
        "color_g":      210,
        "color_b":      240,
        "color2_r":     255,
        "color2_g":     255,
        "color2_b":     255,
    })


# ------------------------------------------------------------------ #
# Controller — start/stop helpers                                      #
# ------------------------------------------------------------------ #

def start_bug_game():
    """Controller-side: kick off a bug-tap round on whichever phones are
    currently calibrated, sorted left-to-right by render order."""
    with _state.lock:
        positions = dict(_state.calibrated_positions)
    if not positions:
        _set_status("No detected phones — run detection first")
        return
    if _render_order:
        ordered = sorted(_render_order.keys(),
                         key=lambda bid: _render_order[bid])
    else:
        ordered = sorted(positions, key=lambda bid: positions[bid].get("u", 0.0))
    payload = {"mode": GAME_MODE_BUG, "order": ordered, "slot_ms": bug_slot_ms}
    ok = _post_json("/admin/game/start", payload)
    if not ok:
        _set_status("Bug game start failed")
        return
    with _state.lock:
        _state.current_effect = None
    set_active_btn("game_start_bug")
    _set_status(f"Bug game — {len(ordered)} phones / "
                f"{BUG_GAME_DURATION_MS // 1000}s round")
    _start_poll()


def start_race_game():
    """Avatar race: every calibrated phone gets a generated avatar on
    stage and races left→right by tapping.  First to 100% wins.

    Lanes are assigned by each phone's calibrated u-position so the on-
    stage layout mirrors the room — audience-left sees their avatar at
    the top of the projection, audience-right at the bottom.  Falls
    back to render-order then blink_id for any phones missing a u.
    """
    with _state.lock:
        positions = dict(_state.calibrated_positions)
    if not positions:
        _set_status("No detected phones — run detection first")
        return
    blink_ids = sorted(
        positions.keys(),
        key=lambda bid: (positions[bid].get("u", 0.5), bid),
    )
    payload = {
        "mode":      GAME_MODE_RACE,
        "blink_ids": blink_ids,
    }
    ok = _post_json("/admin/game/start", payload)
    if not ok:
        _set_status("Race start failed")
        return
    with _state.lock:
        _state.current_effect = None
    set_active_btn("game_start_race")
    _set_status(f"Avatar race — {len(blink_ids)} runners")
    _start_poll()


def stop_game():
    _post_json("/admin/game/stop", {})
    set_active_btn(None)
    _set_status("Game stopped")


def set_active_btn(tag: str | None):
    """Bind the active-orange theme to the named Start button (or clear
    all if tag is None)."""
    for btn in ("game_start_bug", "game_start_race"):
        try:
            if dpg.does_item_exist(btn):
                dpg.bind_item_theme(btn,
                                    "game_active_theme" if btn == tag else None)
        except Exception:
            pass


# Back-compat helpers kept for controller's draw_winner_highlight on canvas
# (used to be a bug-game pulsing-ring marker; race winners are surfaced on
# the stage projection instead, so these stay as no-op stubs).

def get_last_winner():
    return None, 0.0


def clear_winner_highlight():
    pass


# Back-compat name — controller may still call set_game_btn_highlight from
# other paths.
def set_game_btn_highlight(active: bool):
    if not active:
        set_active_btn(None)


def _start_poll():
    """Background poll: refresh sidebar status until the round ends."""
    def _worker():
        while True:
            time.sleep(0.4)
            data = _fetch_json("/admin/game/state")
            if data is None:
                continue
            mode = data.get("mode", GAME_MODE_RACE)
            try:
                if mode == GAME_MODE_BUG:
                    line = (f"Bug: tapped {data.get('tapped', 0)}/"
                            f"{data.get('total', 0)}   "
                            f"missed {data.get('missed', 0)}")
                else:
                    leader = data.get("leader")
                    leader_pos = data.get("leader_pos", 0)
                    runners    = data.get("runners", 0)
                    leader_str = f"#{int(leader)+1}" if leader is not None else "—"
                    line = (
                        f"Race — {runners} runners\n"
                        f"Leader {leader_str}  {leader_pos:>3.0%}"
                    )
                if _ui_queue is not None:
                    _ui_queue.put(("game_status_text", line))
                else:
                    dpg.set_value("game_status_text", line)
            except Exception:
                pass
            if not data.get("active"):
                # Route the theme rebind onto the main thread — bind_item_theme
                # off the poll thread is the 05 May off-thread-DPG freeze pattern.
                if _ui_queue is not None:
                    _ui_queue.put(("_game_active_btn", None))
                else:
                    set_active_btn(None)
                winner = data.get("winner")
                if winner is not None:
                    try:
                        _set_status(f"🏆 Phone #{int(winner)+1} wins!")
                    except Exception:
                        pass
                break
    threading.Thread(target=_worker, daemon=True).start()


# ------------------------------------------------------------------ #
# Controller — sidebar UI                                              #
# ------------------------------------------------------------------ #

def build_sidebar_buttons(indent: int, pad: int):
    """Sidebar block with game modes and a shared Stop."""
    dpg.add_spacer(height=4)
    dpg.add_text("GAME 1 — BUG", color=(160, 160, 160), indent=indent)
    dpg.add_separator()
    dpg.add_button(label="Start Bug Game",
                   tag="game_start_bug",
                   callback=start_bug_game,
                   indent=indent, width=-(pad + 1))

    dpg.add_spacer(height=8)
    dpg.add_text("GAME 2 — AVATAR RACE", color=(160, 160, 160), indent=indent)
    dpg.add_separator()
    dpg.add_button(label="Start Avatar Race",
                   tag="game_start_race",
                   callback=start_race_game,
                   indent=indent, width=-(pad + 1))

    dpg.add_spacer(height=8)
    dpg.add_button(label="Stop Game",
                   callback=stop_game,
                   indent=indent, width=-(pad + 1))
    dpg.add_spacer(height=4)
    dpg.add_text("",
                 tag="game_status_text",
                 color=(200, 200, 200),
                 indent=indent)


def build_window():
    """Old bug-game leaderboard window removed; kept as a no-op so
    setup_ui()'s call site doesn't change."""
    return
