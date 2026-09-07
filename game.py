# (c) Adam Davis - adamdavis.co.uk
"""
pixelmesh V2 — Audience games (dual-mode)

Two games sit behind the same /admin/game/* endpoints, dispatched by
game_mode set on round start:


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
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

# ------------------------------------------------------------------ #
# Wiring                                                              #
# ------------------------------------------------------------------ #

_is_local          = None   # server's local-only test, for the token-free start
_live_devices      = None   # server's liveness rule, so ghosts get no lane
_blink_to_device   = None
_connections       = None
_positions         = None
_blink_assignments = None
_broadcast         = None
_broadcast_spectators = None
_send_safe            = None
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
                broadcast, enable_sync, stop_effects, start_effect=None,
                broadcast_spectators=None, send_safe=None,
                is_local=None, live_devices=None):
    global _blink_to_device, _connections, _positions, _blink_assignments
    global _broadcast, _enable_sync, _stop_effects, _start_effect
    global _broadcast_spectators, _send_safe
    global _is_local, _live_devices
    _broadcast_spectators = broadcast_spectators
    _send_safe            = send_safe
    _is_local          = is_local
    _live_devices      = live_devices
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

GAME_MODE_RACE = "race"

game_mode:   str  = GAME_MODE_RACE
game_active: bool = False


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
async def game_start(request: Request, payload: dict | None = None):
    """Start a race round: {"blink_ids": [<int>, ...]}

    blink_ids is optional. Sent empty, the round is every live phone the camera
    has placed, in u order, which is what the sidebar button works out for
    itself. That is what lets the talk deck start the race as it arrives on the
    race slide: it knows nothing about who is in the room.

    Token-free but LOCAL ONLY, the same exemption /admin/end carries and for
    the same reason - the deck is a static file and cannot hold a token run.sh
    regenerates every launch. The worst this route can do is start a race, and
    it is refused outright for anything arriving through the tunnel.
    """
    if _is_local and not _is_local(request):
        raise HTTPException(403, "this route is only served to local clients")
    return await _start_race_round(payload or {})


@router.post("/admin/game/stop")
async def game_stop():
    """Abort the current round."""
    return await _stop_race_round()


@router.get("/admin/game/state")
async def game_state():
    """Snapshot used by the controller's poll loop."""
    return _race_state_snapshot()


async def handle_tap(device_id: str, reaction_ms: float = 0.0):
    """Routed from the WS handler."""
    if not game_active:
        return
    await _race_handle_tap(device_id)


# ------------------------------------------------------------------ #
# Race game — server                                                   #
# ------------------------------------------------------------------ #

def _room_blink_ids() -> list[int]:
    """Every live phone the camera has placed, ordered left to right.

    The same crowd and the same order start_race_game() works out on the
    controller, derived here instead so a caller that knows nothing about the
    room - the deck - can still start a round. Live rather than merely
    connected: a phone that dropped and came back under a new identity would
    otherwise be given a lane nobody is standing in.
    """
    live = set(_live_devices()) if _live_devices else set(_connections)
    placed = [(dev, pos) for dev, pos in _positions.items()
              if dev in live and dev in _blink_assignments]
    placed.sort(key=lambda dp: (dp[1].get("u", 0.5), _blink_assignments[dp[0]]))
    return [_blink_assignments[dev] for dev, _ in placed]


async def _start_race_round(payload: dict) -> dict:
    """Each phone in blink_ids starts at position 0.0.  Every tap nudges
    the phone forward by 1/RACE_TAPS_PER_PLAYER.  First to 1.0 wins."""
    global game_active, race_positions, race_taps_total
    global race_winner, race_start_at, race_end_at
    global _race_progress_task, _race_last_tap_at

    # Cancel anything still running from a previous round
    await _cancel_race_progress()

    blink_ids = [int(b) for b in payload.get("blink_ids", [])] or _room_blink_ids()
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
                if positions == last_sent:
                    continue
                last_sent = positions
                if _broadcast_spectators is None or _send_safe is None:
                    # Legacy path: full roster to everyone.
                    await _broadcast({"type": "race_progress", "positions": positions})
                    continue
                # The stage renders every runner, so it gets the roster. A phone
                # renders exactly three numbers — its bar, the leader's bar and
                # its rank — so it gets exactly three numbers. At 250 runners the
                # old full-roster broadcast was 3KB × 250 phones × 10Hz, about
                # 60Mbit/s through one tunnel; the slim form is ~50B per phone
                # regardless of roster size.
                await _broadcast_spectators(
                    {"type": "race_progress", "positions": positions})
                vals = sorted(race_positions.values(), reverse=True)
                leader = round(vals[0], 4) if vals else 0.0
                # Rank with ties sharing a place: 1 + how many are strictly ahead.
                # Matches the arithmetic the client used to do over the roster.
                rank_of: dict[float, int] = {}
                for i, v in enumerate(vals):
                    rank_of.setdefault(v, i + 1)
                n_r = len(race_positions)
                sends = []
                for bid, prog in race_positions.items():
                    dev = _blink_to_device(bid)
                    ws = _connections.get(dev) if dev else None
                    if ws:
                        sends.append(_send_safe(ws, {
                            "type":   "race_progress",
                            "mine":   round(prog, 4),
                            "leader": leader,
                            "rank":   rank_of[prog],
                            "n":      n_r,
                        }))
                if sends:
                    await asyncio.gather(*sends)
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
        _set_status("No detected phones - run detection first")
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
    _set_status(f"Avatar race - {len(blink_ids)} runners")
    _start_poll()


def stop_game():
    _post_json("/admin/game/stop", {})
    set_active_btn(None)
    _set_status("Game stopped")


def set_active_btn(tag: str | None):
    """Bind the active-orange theme to the named Start button (or clear
    all if tag is None)."""
    for btn in ("game_start_race",):
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
            try:
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
                        _set_status(f"Phone #{int(winner)+1} wins!")
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
    _t = dpg.add_text("AVATAR RACE", color=(160, 160, 160), indent=indent)
    if dpg.does_item_exist("heading_font"):
        dpg.bind_item_font(_t, "heading_font")
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
