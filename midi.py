# (c) Adam Davis - adamdavis.co.uk
"""
pixelmesh V2 — MIDI input (BOSS FS-1-WL wireless footswitch)

Three switches, mapped by a one-time learn step to the three most
show-useful hands-free actions:

    switch 1  →  fresh detection run (reset, then start; stomp again stops)
    switch 2  →  cycle through effects (wave, gradient, pulse, ...)
    switch 3  →  toggle video recording

The FS-1-WL sends different messages depending on its power-on mode
(CC / note / HID), so mappings are LEARNED, not hardcoded:

    1. Pair the pedal once: Audio MIDI Setup → Window → Show MIDI Studio
       → Bluetooth → connect FS-1-WL.
    2. Run:  python3.14 midi.py --learn
    3. Stomp each switch when prompted. Mappings land in midi_map.json.

At show time the controller listens with the learned map. The pedal is
wireless and may wake after the app starts, so the port scanner retries
every 5s in the background instead of giving up at boot (the old LPD8
behaviour). Switch presses toggle internal state for sync/recording;
if you also flip those from the sidebar, the pedal's notion of on/off
can invert - stomp twice to resync, same caveat the LPD8 knobs had.
"""

import json
import logging
import os
import threading
import time as _time
from collections import deque

import rtmidi

from log import log

_DIR = os.path.dirname(__file__)
MAP_PATH = os.path.join(_DIR, "midi_map.json")

# Dedicated MIDI debug log
_midi_log_path = os.path.join(_DIR, "debug", "midi_debug.log")
_midi_handler = logging.FileHandler(_midi_log_path, mode="a", encoding="utf-8")
_midi_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
_mlog = logging.getLogger("pixelmesh.midi")
_mlog.setLevel(logging.DEBUG)
_mlog.addHandler(_midi_handler)
_mlog.propagate = False

# Actions in learn order. Names double as midi_map.json keys.
ACTIONS = ["detection", "effects", "recording"]

# Parameter-free effects the pedal steps through, in show order.
# (ripple needs a click point and groups needs column config - excluded.)
EFFECT_CYCLE = ["wave", "gradient", "pulse", "rainbow", "sparkle", "sections", "ring"]

# Port-name fragments that identify the pedal (BLE MIDI names vary a
# little between macOS versions; all contain "FS-1").
_PORT_HINTS = ("fs-1", "fs1")


def _find_port(midi_in) -> int | None:
    for i, name in enumerate(midi_in.get_ports()):
        if any(h in name.lower() for h in _PORT_HINTS):
            return i
    return None


def _is_press(msg: list[int]) -> bool:
    """True for the press edge of a switch in any FS-1-WL mode:
    CC with value > 0, or Note On with velocity > 0."""
    if len(msg) < 3:
        return False
    kind = msg[0] & 0xF0
    return (kind == 0xB0 and msg[2] > 0) or (kind == 0x90 and msg[2] > 0)


def _signature(msg: list[int]) -> dict:
    """The identity of a switch: status byte + first data byte."""
    return {"status": msg[0], "data1": msg[1]}


def load_map() -> dict | None:
    try:
        with open(MAP_PATH) as f:
            m = json.load(f)
        if all(a in m for a in ACTIONS):
            return m
    except (OSError, ValueError):
        pass
    return None


class MidiInput:
    def __init__(self):
        self._midi_in = None
        self._thread  = None
        self._running = False
        self._map     = None          # action → {"status", "data1"}
        self._state   = {"recording": False}
        self._fx_idx  = -1            # last fired index into EFFECT_CYCLE
        # Last 15 events for the sidebar MIDI panel. version bumps on
        # every append so the UI refresh can cheaply skip when idle.
        self._hist    = deque(maxlen=15)
        self.history_version = 0
        self.connected = False   # live pedal link state for the sidebar

    def _note(self, text: str):
        self._hist.append(f"{_time.strftime('%H:%M:%S')}  {text}")
        self.history_version += 1

    def history(self) -> list[str]:
        return list(reversed(self._hist))   # newest first

    # Signature kept identical to the LPD8 version so controller.py
    # needs no changes; trigger_effect/set_iso/set_overlays/reset are
    # accepted but unused (three switches, three actions).
    def start(self, trigger_effect, toggle_detect, set_iso, set_recording=None,
              set_overlays=None, set_sync=None, reset=None):
        self._toggle_detect  = toggle_detect
        self._set_recording  = set_recording
        self._trigger_effect = trigger_effect

        self._map = load_map()
        if self._map is None:
            _mlog.warning("[midi] no midi_map.json - run: python3.14 midi.py --learn")
            log.warning("[midi] FS-1-WL not mapped - run: python3.14 midi.py --learn")
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="midi")
        self._thread.start()

    def stop(self):
        self.connected = False
        self._running = False
        if self._midi_in:
            self._midi_in.close_port()
            self._midi_in = None

    # ---------------------------------------------------------------- #

    def _loop(self):
        import time
        announced_wait = False
        last_presence_check = 0.0
        while self._running:
            # (Re)connect: BLE pedals come and go; keep scanning.
            if self._midi_in is None:
                probe = rtmidi.MidiIn()
                idx = _find_port(probe)
                if idx is None:
                    del probe
                    if not announced_wait:
                        _mlog.info("[midi] waiting for FS-1-WL to appear...")
                        log.info("[midi] waiting for FS-1-WL (pair via Audio MIDI Setup > Bluetooth)")
                        announced_wait = True
                    time.sleep(5)
                    continue
                name = probe.get_ports()[idx]
                del probe
                self._midi_in = rtmidi.MidiIn()
                self._midi_in.open_port(idx)
                self._midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
                announced_wait = False
                self.connected = True
                _mlog.info(f"[midi] listening on: {name}")
                log.info(f"[midi] FS-1-WL connected: {name}")
                self._note("pedal connected")

            # BLE disconnects usually do NOT error the open port on macOS -
            # get_message just goes silent forever. Actively verify the
            # pedal is still in the system port list every 3s.
            now = time.time()
            if now - last_presence_check >= 3.0:
                last_presence_check = now
                probe = rtmidi.MidiIn()
                present = _find_port(probe) is not None
                del probe
                if not present:
                    self.connected = False
                    self._note("pedal lost")
                    _mlog.info("[midi] pedal vanished from port list - rescanning")
                    log.info("[midi] FS-1-WL disconnected")
                    try:
                        self._midi_in.close_port()
                    except Exception:
                        pass
                    self._midi_in = None
                    continue

            try:
                msg = self._midi_in.get_message()
            except Exception:
                # Pedal slept / BLE dropped - go back to scanning.
                self.connected = False
                _mlog.info("[midi] port lost, rescanning")
                try:
                    self._midi_in.close_port()
                except Exception:
                    pass
                self._midi_in = None
                continue
            if msg:
                _mlog.debug(f"[midi] raw: {msg[0]}")
                self._handle(msg[0])
            else:
                time.sleep(0.001)

    def _handle(self, msg: list[int]):
        if self._map is None or not _is_press(msg):
            return
        sig = _signature(msg)
        for action in ACTIONS:
            m = self._map[action]
            if m["status"] == sig["status"] and m["data1"] == sig["data1"]:
                self._dispatch(action)
                return
        self._note(f"? unmapped {sig['status']}/{sig['data1']}")
        _mlog.warning(f"[midi] unmapped press: {msg} - re-run learn if switches changed mode")

    def _dispatch(self, action: str):
        if action == "detection":
            _mlog.info("[midi] switch -> toggle detection")
            log.info("[midi] FS-1-WL -> toggle detection")
            self._note("toggle detection")
            if self._toggle_detect:
                self._toggle_detect()
        elif action == "effects":
            self._fx_idx = (self._fx_idx + 1) % len(EFFECT_CYCLE)
            name = EFFECT_CYCLE[self._fx_idx]
            _mlog.info(f"[midi] switch -> effect '{name}'")
            log.info(f"[midi] FS-1-WL -> effect '{name}'")
            self._note(f"effect: {name}")
            if self._trigger_effect:
                self._trigger_effect(name)
        elif action == "recording":
            self._state["recording"] = not self._state["recording"]
            on = self._state["recording"]
            _mlog.info(f"[midi] switch -> recording {'ON' if on else 'OFF'}")
            log.info(f"[midi] FS-1-WL -> recording {'ON' if on else 'OFF'}")
            self._note(f"recording {'ON' if on else 'OFF'}")
            if self._set_recording:
                self._set_recording(on)


midi = MidiInput()


# ------------------------------------------------------------------ #
# Learn mode: python3.14 midi.py --learn
# ------------------------------------------------------------------ #

def _learn():
    import time
    print("FS-1-WL learn mode")
    print("Waiting for the pedal (pair via Audio MIDI Setup > Bluetooth)...")
    midi_in = rtmidi.MidiIn()
    idx = None
    while idx is None:
        idx = _find_port(midi_in)
        if idx is None:
            time.sleep(2)
            midi_in = rtmidi.MidiIn()
    name = midi_in.get_ports()[idx]
    midi_in.open_port(idx)
    midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
    print(f"Connected: {name}\n")

    labels = {
        "detection": "TOGGLE DETECTION",
        "effects":   "CYCLE EFFECTS",
        "recording": "TOGGLE VIDEO RECORDING",
    }
    mapping = {}
    for action in ACTIONS:
        print(f"Press the switch for: {labels[action]}")
        sig = None
        while sig is None:
            msg = midi_in.get_message()
            if msg and _is_press(msg[0]):
                sig = _signature(msg[0])
                # duplicate-switch guard
                if any(m == sig for m in mapping.values()):
                    print("  that switch is already used - press a different one")
                    sig = None
                    time.sleep(0.4)
            else:
                time.sleep(0.005)
        mapping[action] = sig
        print(f"  learned: status={sig['status']} data1={sig['data1']}\n")
        time.sleep(0.6)   # swallow the release / bounce

    with open(MAP_PATH, "w") as f:
        json.dump(mapping, f, indent=2)
    print(f"Saved {MAP_PATH}")
    print("Done - restart the controller and the pedal is live.")


if __name__ == "__main__":
    import sys
    if "--learn" in sys.argv:
        _learn()
    else:
        print(__doc__)
