# (c) Adam Davis - adamdavis.co.uk
"""
PixelMesh V2 — Rope Climb game

Replaces the bug-tap game.  Audience phones split into two teams (Diana and
Rosie) by detected u-position, and each tap nudges that team's character up
the rope.  A gentle decay pulls the heights back down so sustained tapping
is required to win.  First to height 1.0 wins.

Server side:
    game.server_init(blink_to_device, connections, positions, blink_assignments,
                     broadcast, enable_sync, stop_effects)
    app.include_router(game.router)
    # In the WS handler, call game.handle_tap(device_id, ...)

Controller side:
    game.init(state, set_status, post_json, fetch_json, render_order)
    # In setup_ui(), call game.build_sidebar_buttons(indent, pad)
    #                     game.build_window()
"""

import asyncio
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


def init(state, set_status, post_json, fetch_json, render_order):
    global _state, _set_status, _post_json, _fetch_json, _render_order
    _state        = state
    _set_status   = set_status
    _post_json    = post_json
    _fetch_json   = fetch_json
    _render_order = render_order


# ------------------------------------------------------------------ #
# Tunables                                                            #
# ------------------------------------------------------------------ #

# Per-tap climb is computed PER TEAM at round start so a team of two
# isn't fighting decay forever while a team of twenty races to the top in
# seconds.  Target ~50 taps per player to reach 1.0 regardless of team
# size — solo player can do it in ~25s, team of 20 finishes in similar
# time without any single player having to spam-tap.
CLIMB_TAPS_PER_PLAYER  = 50
# Hard floor + ceiling on per-tap value so a wildly unbalanced split (1 vs 30)
# still feels playable.
CLIMB_PER_TAP_MIN      = 0.0015
CLIMB_PER_TAP_MAX      = 0.035
# Per-second decay so progress requires sustained tapping (and dramatic
# tug-of-war when the room slacks off).
CLIMB_DECAY            = 0.010
# Min interval between accepted taps from the same phone (anti-spam).
PER_PHONE_TAP_INTERVAL = 0.18      # ~5 taps/s max per phone
# Broadcast cadence.
PROGRESS_INTERVAL      = 0.20      # 5 Hz


def _climb_per_tap_for(team: str) -> float:
    """How much a single tap raises this team's height.  Inversely
    proportional to team size so per-player effort stays roughly constant
    across crowd sizes."""
    size = sum(1 for t in rope_teams.values() if t == team)
    if size <= 0:
        return CLIMB_PER_TAP_MAX
    raw = 1.0 / (CLIMB_TAPS_PER_PLAYER * size)
    return max(CLIMB_PER_TAP_MIN, min(CLIMB_PER_TAP_MAX, raw))

# ------------------------------------------------------------------ #
# State                                                                #
# ------------------------------------------------------------------ #

# Kept (as empty defaults) for backward compatibility with report.py and
# controller code that still references game.game_active / game_results /
# game_order.  The rope game doesn't populate the bug-game-shaped fields.
game_active:  bool             = False
game_results: dict[int, float] = {}
game_order:   list[int]        = []

# Rope-climb state
rope_teams:      dict[int, str]   = {}                       # blink_id → "diana" | "rosie"
rope_heights:    dict[str, float] = {"diana": 0.0, "rosie": 0.0}
rope_taps_total: dict[str, int]   = {"diana": 0,   "rosie": 0}
rope_winner:     str | None       = None
rope_start_at:   float            = 0.0
rope_end_at:     float            = 0.0

_last_tap_at:    dict[str, float] = {}   # device_id → epoch
_progress_task:  asyncio.Task | None = None


# ------------------------------------------------------------------ #
# Server — routes                                                      #
# ------------------------------------------------------------------ #

@router.post("/admin/game/start")
async def game_start(payload: dict):
    """Start a rope-climb round.

    payload: {
        "teams": { "<blink_id>": "diana" | "rosie", ... }
    }

    The controller computes team assignments from the median u of detected
    phones; uncalibrated phones reach this endpoint with no team in the map
    and stay neutral (their taps are ignored)."""
    global game_active, rope_teams, rope_heights, rope_taps_total
    global rope_winner, rope_start_at, rope_end_at
    global _progress_task, _last_tap_at

    # Reset round state
    rope_teams      = {int(bid): str(team)
                       for bid, team in payload.get("teams", {}).items()}
    rope_heights    = {"diana": 0.0, "rosie": 0.0}
    rope_taps_total = {"diana": 0,   "rosie": 0}
    rope_winner     = None
    rope_start_at   = time.time()
    rope_end_at     = 0.0
    _last_tap_at    = {}
    game_active     = True

    # Cancel any prior progress loop before starting a fresh one.
    if _progress_task and not _progress_task.done():
        _progress_task.cancel()

    # Effects clash with the game view on phones — clear before starting.
    if _stop_effects:
        await _stop_effects()
    # Sync isn't strictly required (rope game isn't time-precision sensitive),
    # but enabling it keeps countdown/UI animations aligned across phones.
    if _enable_sync:
        await _enable_sync()

    if _broadcast:
        await _broadcast({
            "type":     "rope_start",
            "teams":    {str(bid): team for bid, team in rope_teams.items()},
            "start_at": int(rope_start_at * 1000),
        })

    _progress_task = asyncio.create_task(_rope_progress_loop())
    return {"ok": True}


@router.post("/admin/game/stop")
async def game_stop():
    """Abort the current round with no winner."""
    global game_active, _progress_task
    if not game_active and _progress_task is None:
        return {"ok": True}
    game_active = False
    if _progress_task and not _progress_task.done():
        _progress_task.cancel()
    _progress_task = None
    if _broadcast:
        await _broadcast({
            "type":   "rope_end",
            "winner": None,
            "diana":  rope_heights["diana"],
            "rosie":  rope_heights["rosie"],
        })
    await _start_default_wave()
    return {"ok": True}


async def _start_default_wave():
    """After a rope round ends, fire a calm wave so audience phones aren't
    stuck on the game card with no animation.  Goes through the server's
    own start_effect so current_effect_state is updated for reconnects."""
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


@router.get("/admin/game/state")
async def game_state():
    """Snapshot used by the controller's poll loop."""
    return {
        "active":  game_active,
        "heights": rope_heights,
        "taps":    rope_taps_total,
        "winner":  rope_winner,
        "teams":   {
            "diana": sum(1 for t in rope_teams.values() if t == "diana"),
            "rosie": sum(1 for t in rope_teams.values() if t == "rosie"),
        },
    }


# ------------------------------------------------------------------ #
# Server — tap handler + loop                                          #
# ------------------------------------------------------------------ #

async def handle_tap(device_id: str, reaction_ms: float = 0.0):
    """Called (awaited) from the WS handler when a game_tap message arrives.
    `reaction_ms` is kept in the signature for backward compatibility with the
    old bug-game wire format; the rope game ignores it."""
    if not game_active:
        return
    blink_id = _blink_assignments.get(device_id)
    if blink_id is None:
        return
    team = rope_teams.get(blink_id)
    if team is None:
        return   # neutral / uncalibrated phone — tap is ignored

    now = time.time()
    last = _last_tap_at.get(device_id, 0.0)
    if now - last < PER_PHONE_TAP_INTERVAL:
        return   # rate-limited
    _last_tap_at[device_id] = now

    rope_heights[team]    = min(1.0, rope_heights[team] + _climb_per_tap_for(team))
    rope_taps_total[team] += 1

    if rope_heights[team] >= 1.0 and rope_winner is None:
        await _rope_finish(team)


async def _rope_progress_loop():
    """Periodic decay + broadcast.  Decay drains heights down toward zero so
    teams have to keep up a steady tap rate to make progress."""
    last_t = time.time()
    try:
        while game_active:
            await asyncio.sleep(PROGRESS_INTERVAL)
            now = time.time()
            dt = now - last_t
            last_t = now
            if rope_winner is None:
                for team in rope_heights:
                    if rope_heights[team] < 1.0:
                        rope_heights[team] = max(
                            0.0, rope_heights[team] - CLIMB_DECAY * dt
                        )
            if _broadcast:
                await _broadcast({
                    "type":  "rope_progress",
                    "diana": round(rope_heights["diana"], 4),
                    "rosie": round(rope_heights["rosie"], 4),
                })
    except asyncio.CancelledError:
        pass


async def _rope_finish(winner: str):
    global game_active, rope_winner, rope_end_at, _progress_task
    rope_winner = winner
    rope_end_at = time.time()
    game_active = False
    if _progress_task and not _progress_task.done():
        _progress_task.cancel()
    _progress_task = None
    if _broadcast:
        await _broadcast({
            "type":   "rope_end",
            "winner": winner,
            "diana":  rope_heights["diana"],
            "rosie":  rope_heights["rosie"],
        })
    # Give phones (and the stage page) enough time to celebrate the winner
    # before swapping everyone over to the calm default wave.  Without this
    # the wave message landed almost instantly after rope_end and the
    # audience flicked off the winner banner before they could read it.
    asyncio.create_task(_delayed_default_wave(5.0))


async def _delayed_default_wave(delay: float):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await _start_default_wave()


# ------------------------------------------------------------------ #
# Controller — start / stop / poll                                     #
# ------------------------------------------------------------------ #

def _assign_teams(positions: dict[int, dict]) -> dict[int, str]:
    """Split phones into Diana/Rosie by median u.  Below-median → Diana
    (left of stage); at-or-above → Rosie.  Returns blink_id → team."""
    if not positions:
        return {}
    us = sorted(p["u"] for p in positions.values())
    median_u = us[len(us) // 2]
    return {
        bid: ("diana" if p["u"] < median_u else "rosie")
        for bid, p in positions.items()
    }


def start_rope_game():
    with _state.lock:
        positions = dict(_state.calibrated_positions)
    if not positions:
        _set_status("No detected phones — run detection first")
        return
    teams = _assign_teams(positions)
    payload = {"teams": {str(b): t for b, t in teams.items()}}
    ok = _post_json("/admin/game/start", payload)
    if not ok:
        _set_status("Rope start failed")
        return
    with _state.lock:
        _state.current_effect = None
    set_game_btn_highlight(True)
    diana_n = sum(1 for t in teams.values() if t == "diana")
    rosie_n = sum(1 for t in teams.values() if t == "rosie")
    _set_status(f"Rope climb — Diana {diana_n}  vs  Rosie {rosie_n}")
    _start_poll()


def stop_rope_game():
    _post_json("/admin/game/stop", {})
    set_game_btn_highlight(False)
    _set_status("Rope climb stopped")


def set_game_btn_highlight(active: bool):
    try:
        if dpg.does_item_exist("game_start_btn"):
            dpg.bind_item_theme("game_start_btn",
                                "game_active_theme" if active else None)
    except Exception:
        pass


# Winner-highlight API kept for controller.py's draw_winner_highlight call.
# The rope game has no per-phone winner so this stays empty.

def get_last_winner():
    return None, 0.0


def clear_winner_highlight():
    pass


def _start_poll():
    """Background poll: refresh sidebar height readout, drop highlight on end."""
    def _worker():
        while True:
            time.sleep(0.4)
            data = _fetch_json("/admin/game/state")
            if data is None:
                continue
            heights = data.get("heights", {})
            taps    = data.get("taps", {})
            try:
                dpg.set_value(
                    "rope_status_text",
                    f"Diana {heights.get('diana', 0):>3.0%}  ({taps.get('diana', 0)})\n"
                    f"Rosie {heights.get('rosie', 0):>3.0%}  ({taps.get('rosie', 0)})"
                )
            except Exception:
                pass
            if not data.get("active"):
                set_game_btn_highlight(False)
                winner = data.get("winner")
                if winner:
                    try:
                        _set_status(f"🏆 {winner.title()} wins!")
                    except Exception:
                        pass
                break
    threading.Thread(target=_worker, daemon=True).start()


# ------------------------------------------------------------------ #
# Controller — UI                                                      #
# ------------------------------------------------------------------ #

def build_sidebar_buttons(indent: int, pad: int):
    """Add the ROPE CLIMB section to the sidebar."""
    dpg.add_spacer(height=4)
    dpg.add_text("ROPE CLIMB", color=(160, 160, 160), indent=indent)
    dpg.add_separator()
    dpg.add_button(label="Start Rope Climb",
                   tag="game_start_btn",
                   callback=start_rope_game,
                   indent=indent, width=-(pad + 1))
    dpg.add_button(label="Stop",
                   callback=stop_rope_game,
                   indent=indent, width=-(pad + 1))
    dpg.add_spacer(height=4)
    dpg.add_text("",
                 tag="rope_status_text",
                 color=(200, 200, 200),
                 indent=indent)


def build_window():
    """Old bug-game leaderboard window removed.  Kept as a no-op so the
    controller setup_ui() call site doesn't change."""
    return
