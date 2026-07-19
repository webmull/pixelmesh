# (c) Adam Davis - adamdavis.co.uk
"""
pixelmesh V2 — MIDI input (Akai LPD8 mk2)

Runs a background thread listening for MIDI messages and dispatches
them to effect triggers and parameter controls.

LPD8 mk2 sends pads as CC messages on MIDI channel 9 (status 0xB9),
not Note On. Knobs are CC on channel 0 (status 0xB0).

Pad CC layout (discovered by testing — value 127 = press, 0 = release):
  [ ? ][ ? ][ ? ][ 19 ]   ← top row    (? + detection toggle)
  [ ? ][ ? ][ ? ][ ?  ]   ← bottom row (effects — TBD)

Knob CC numbers (channel 0):
  CC 70 → ISO gain
  CC 71–77 → reserved
"""

import logging
import os
import threading
import rtmidi

from log import log

# Dedicated MIDI debug log
_midi_log_path = os.path.join(os.path.dirname(__file__), "debug", "midi_debug.log")
_midi_handler = logging.FileHandler(_midi_log_path, mode="a", encoding="utf-8")
_midi_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
_mlog = logging.getLogger("pixelmesh.midi")
_mlog.setLevel(logging.DEBUG)
_mlog.addHandler(_midi_handler)
_mlog.propagate = False

# ------------------------------------------------------------------ #
# Pad CC map (channel 9 / status 0xB9)
# CC number → effect name; add entries here as pads are discovered.
PAD_CC_MAP: dict[int, str] = {
    # TBD — press each pad and check debug/midi_debug.log for CC number
}

# Special pad CC → detection toggle
CC_PAD_TOGGLE = 19   # top-right pad (pad 8 on LPD8 mk2)

# Note On pad map (fallback if device is in note mode)
PAD_NOTE_MAP: dict[int, str] = {
    36: "wave",
    37: "gradient",
    38: "pulse",
    39: "rainbow",
    40: "ripple",
    41: "sparkle",
}
NOTE_PAD_TOGGLE = 43

# ------------------------------------------------------------------ #
# Knob (channel 0 / status 0xB0)
CC_ISO       = 70
CC_RECORDING = 71   # knob 2 — any value > 0 = record on, 0 = record off
CC_OVERLAYS  = 72   # knob 3 — any value > 0 = ID overlays on, 0 = off
CC_SYNC      = 73   # knob 4 — any value > 0 = clock sync on, 0 = off
CC_RESET     = 77   # knob 8 — any value > 0 = server reset

_ISO_MIN = 0
_ISO_MAX = 160


def _cc_to_iso(cc_val: int) -> int:
    return int(_ISO_MIN + (cc_val / 127) * (_ISO_MAX - _ISO_MIN))


# ------------------------------------------------------------------ #

class MidiController:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._running = False
        self._midi_in: rtmidi.MidiIn | None = None

        self._trigger_effect  = None   # fn(name: str)
        self._toggle_detect   = None   # fn()
        self._set_iso         = None   # fn(value: int)
        self._set_recording   = None   # fn(on: bool)
        self._set_overlays    = None   # fn(on: bool)
        self._set_sync        = None   # fn(on: bool)
        self._reset           = None   # fn()

    # ---------------------------------------------------------------- #

    def start(self, trigger_effect, toggle_detect, set_iso, set_recording=None, set_overlays=None, set_sync=None, reset=None):
        self._trigger_effect  = trigger_effect
        self._toggle_detect   = toggle_detect
        self._set_iso         = set_iso
        self._set_recording   = set_recording
        self._set_overlays    = set_overlays
        self._set_sync        = set_sync
        self._reset           = reset

        ports = rtmidi.MidiIn().get_ports()
        lpd8_idx = next(
            (i for i, p in enumerate(ports) if "lpd8" in p.lower()),
            None,
        )
        if lpd8_idx is None:
            _mlog.warning("[midi] LPD8 not found — ports: " + str(ports))
            log.warning("[midi] LPD8 not found — ports: " + str(ports))
            return

        self._midi_in = rtmidi.MidiIn()
        self._midi_in.open_port(lpd8_idx)
        self._midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="midi")
        self._thread.start()
        _mlog.info(f"[midi] listening on port {lpd8_idx}: {ports[lpd8_idx]}")
        log.info(f"[midi] listening on port {lpd8_idx}: {ports[lpd8_idx]}")

    def stop(self):
        self._running = False
        if self._midi_in:
            self._midi_in.close_port()
            self._midi_in = None

    # ---------------------------------------------------------------- #

    def _loop(self):
        import time
        while self._running:
            msg = self._midi_in.get_message()
            if msg:
                _mlog.debug(f"[midi] raw: {msg[0]}")
                self._handle(msg[0])
            time.sleep(0.001)

    def _handle(self, msg: list[int]):
        if len(msg) < 2:
            return

        status, data1 = msg[0], msg[1]
        data2 = msg[2] if len(msg) > 2 else 0
        kind    = status & 0xF0
        channel = status & 0x0F

        # ---- Pad CC messages (channel 9, value > 0 = press) ----
        if kind == 0xB0 and channel == 9 and data2 > 0:
            if data1 == CC_PAD_TOGGLE:
                _mlog.info(f"[midi] pad CC{data1} → toggle detection")
                log.info(f"[midi] pad CC{data1} → toggle detection")
                if self._toggle_detect:
                    self._toggle_detect()
            elif data1 in PAD_CC_MAP:
                name = PAD_CC_MAP[data1]
                _mlog.info(f"[midi] pad CC{data1} → effect '{name}'")
                log.info(f"[midi] pad CC{data1} → effect '{name}'")
                if self._trigger_effect:
                    self._trigger_effect(name)
            else:
                _mlog.warning(f"[midi] unmapped pad CC{data1} (val={data2}) — add to PAD_CC_MAP")

        # ---- Knob CC messages (channel 0) ----
        elif kind == 0xB0 and channel == 0:
            if data1 == CC_ISO:
                iso = _cc_to_iso(data2)
                _mlog.info(f"[midi] knob CC{data1} → ISO {iso}")
                log.info(f"[midi] knob CC{data1} → ISO {iso}")
                if self._set_iso:
                    self._set_iso(iso)
            elif data1 == CC_OVERLAYS:
                on = data2 > 0
                _mlog.info(f"[midi] knob CC{data1} → overlays {'ON' if on else 'OFF'} (val={data2})")
                log.info(f"[midi] knob CC{data1} → overlays {'ON' if on else 'OFF'}")
                if self._set_overlays:
                    self._set_overlays(on)
            elif data1 == CC_SYNC:
                on = data2 > 0
                _mlog.info(f"[midi] knob CC{data1} → sync {'ON' if on else 'OFF'} (val={data2})")
                log.info(f"[midi] knob CC{data1} → sync {'ON' if on else 'OFF'}")
                if self._set_sync:
                    self._set_sync(on)
            elif data1 == CC_RESET and data2 > 0:
                _mlog.info(f"[midi] knob CC{data1} → server reset")
                log.info(f"[midi] knob CC{data1} → server reset")
                if self._reset:
                    self._reset()
            elif data1 == CC_RECORDING:
                on = data2 > 0
                _mlog.info(f"[midi] knob CC{data1} → recording {'ON' if on else 'OFF'} (val={data2})")
                log.info(f"[midi] knob CC{data1} → recording {'ON' if on else 'OFF'}")
                if self._set_recording:
                    self._set_recording(on)
            else:
                _mlog.debug(f"[midi] unhandled knob CC{data1} val={data2}")

        # ---- Note On pad (fallback note mode) ----
        elif kind == 0x90 and data2 > 0:
            if data1 == NOTE_PAD_TOGGLE:
                _mlog.info(f"[midi] note pad {data1} → toggle detection")
                if self._toggle_detect:
                    self._toggle_detect()
            elif data1 in PAD_NOTE_MAP:
                name = PAD_NOTE_MAP[data1]
                _mlog.info(f"[midi] note pad {data1} → effect '{name}'")
                if self._trigger_effect:
                    self._trigger_effect(name)
            else:
                _mlog.warning(f"[midi] unmapped note {data1}")

        # ---- Program Change (mode button) ----
        elif kind == 0xC0:
            _mlog.info(f"[midi] program change → program {data1}")

        # ---- Catch-all ----
        else:
            _mlog.debug(f"[midi] unhandled: status={status:#04x} d1={data1} d2={data2}")


midi = MidiController()
