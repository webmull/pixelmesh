# (c) Adam Davis - adamdavis.co.uk
"""
pixelmesh V2 - Logitech Spotlight 2 as a hand-held ISO trim.

ISO is driven automatically around the detection toggle (35 detecting, 100
showtime), which leaves the only manual override on a slider at the laptop.
This puts it in Adam's hand instead, with the remote's vibration motor
confirming each step so nothing has to be read off a screen in a dark room.

TWO CONTROLS, ONE EACH WAY

    touch panel          (0x0050)   ISO up,   one buzz
    laser pointer button (0x00fc)   ISO down, two buzzes

Buzz count confirms the direction without looking, which is the whole point
in a dark room.

Getting here took some finding, and the dead ends are worth recording so
nobody repeats them (tools/presenter_probe.py runs the experiments):

  - The panel does not report WHICH HALF was pressed. Up and down are
    byte-identical, and diverting the 0x00da / 0x00dc next/back controls
    does not change that. The panel does not page slides either, so there
    is no direction hiding in a keystroke we were failing to read.
  - There is no press DURATION either: the release arrives immediately
    however long the panel is held, so click-and-hold can never work.
  - Deriving direction from click COUNT does work, but fights the hand -
    clicking several times to step up gets read as double-clicks and
    reverses. One control cannot carry two directions.

So the direction comes from two separate controls, which is both simpler
and unambiguous.

DIVERT IS WHAT MAKES A CONTROL REPORT

Neither control reaches us unless it is diverted. The panel ships diverted
already, which is why it seemed to report "for free"; the pointer button
does not, and is silent until asked.

Diverting does NOT suppress the control's normal function on this remote -
tested, the panel still left-clicks and still advances a web page while we
read it - so the remote goes on being a remote while pixelmesh trims ISO
from it. Worth knowing in both directions: a panel tap is still a click
landing wherever the cursor sits.

Both are put back exactly as they were found - restoring blindly to "off"
silences the panel for whatever runs next, which cost an afternoon once
already.

The device is opened NON-exclusively. hidapi's macOS backend seizes by
default, which needs an Input Monitoring grant AND takes the remote away
from the system; non-exclusive needs no permission at all.

Wire format is HID++ 2.0 long reports (report id 0x11, 20 bytes) on the
vendor collection. Deliberately duplicated from the probe rather than
shared: the probe is a diagnostic that changes freely, and the show path
should not import from tools/.
"""

import logging
import os
import threading
import time
from collections import deque

from log import log

try:
    import hid
except ImportError:                       # optional, like Pillow for the HUD
    hid = None

_DIR = os.path.dirname(__file__)

# Dedicated debug log, alongside the pedal's.
_log_path = os.path.join(_DIR, "debug", "presenter_debug.log")
_handler = logging.FileHandler(_log_path, mode="a", encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
_plog = logging.getLogger("pixelmesh.presenter")
_plog.setLevel(logging.DEBUG)
_plog.addHandler(_handler)
_plog.propagate = False

VID             = 0x046D
PID_SPOTLIGHT_2 = 0xB506
VENDOR_PAGES    = (0xFF43, 0xFF00)     # BLE / receiver HID++ collections

HIDPP_LONG = 0x11
LONG_LEN   = 20
SW_ID      = 0x0A

FEAT_BATTERY    = 0x1004
FEAT_CONTROLS   = 0x1B04
FEAT_HAPTIC     = 0x19B0

# Tap the panel to go up, press the laser pointer button to go down.
#
# The pointer button is 0x01b0, confirmed by pressing it ten times and
# counting ten events. It is emphatically NOT 0x00fc, which is what a census
# of "press everything" suggested - that run could not tell which physical
# control produced which id, and the wrong guess showed up as a button that
# only registered when hammered.
CID_PANEL   = 0x0050
CID_POINTER = 0x01B0
CID_STEP = {CID_PANEL: +1, CID_POINTER: -1}

# Presses arrive as an instantaneous down/release pair, so a step fires on
# the down edge and the repeat rate is simply whatever the hand does. There
# is deliberately no debounce: edge detection already ignores a repeated
# report of an unchanged state, and a time-based guard would instead throw
# away the second of two genuine presses made in quick succession.

RESCAN_SECS = 5.0     # the remote drops off Bluetooth entirely when idle


class Presenter:
    """Reads the Spotlight and turns panel gestures into ISO trim steps."""

    def __init__(self):
        self._thread   = None
        self._running  = False
        self._dev      = None
        self._path     = None          # the exact handle we hold, for re-pair checks
        self._index    = 0xFF          # HID++ device index; BLE answers on 0xFF
        self._feat     = {}            # feature id -> index
        self._divert_was = {}      # cid -> divert state as we found it
        self._nudge    = None
        # Haptic runs on its own thread so its pulse gaps never stall the
        # read loop. Bounded: pressing faster than the motor can answer
        # should drop buzzes, not build a backlog that rumbles on after the
        # hand has stopped.
        self._buzz_q     = deque(maxlen=3)
        self._write_lock = threading.Lock()
        self.connected = False
        self.battery   = None
        # Last 15 events for the sidebar, with the same cheap version guard
        # the MIDI panel uses so an idle UI does no work.
        self._hist = deque(maxlen=15)
        self.history_version = 0

    # ---------------------------------------------------------------- #
    # Public API - called from the controller, any thread
    # ---------------------------------------------------------------- #

    def start(self, nudge_iso):
        """nudge_iso(step) -> bool, True when the step actually landed.

        A False return is what drives the "that did nothing" buzz: ISO
        already at the end of its range, or Camera Hub not connected.
        """
        if hid is None:
            log.info("[presenter] hid module missing - remote disabled "
                     "(brew install hidapi && pip install hid)")
            return
        self._nudge   = nudge_iso
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="presenter")
        self._thread.start()
        threading.Thread(target=self._buzz_worker, daemon=True,
                         name="presenter-haptic").start()

    def stop(self):
        self._running = False
        self.connected = False

    def history(self) -> list[str]:
        return list(reversed(self._hist))       # newest first

    def status_line(self) -> str:
        """One ASCII line for the sidebar. The DPG font has no glyphs beyond
        ASCII, so no arrows or ellipses here."""
        if hid is None:
            return "hid module missing"
        if not self.connected:
            return "waiting for remote"
        batt = f"  batt {self.battery}%" if self.battery is not None else ""
        return f"connected{batt}"

    # ---------------------------------------------------------------- #
    # Internals
    # ---------------------------------------------------------------- #

    def _note(self, text: str):
        self._hist.append(f"{time.strftime('%H:%M:%S')}  {text}")
        self.history_version += 1

    def _find(self):
        """Strictly the Spotlight. Adam has other Logitech kit paired that
        also speaks HID++ on the same vendor collection, and talking to the
        wrong one produces plausible nonsense."""
        try:
            rows = [d for d in hid.enumerate(VID, 0)
                    if d["product_id"] == PID_SPOTLIGHT_2
                    or "spotlight" in (d["product_string"] or "").lower()]
        except Exception:
            return None
        if not rows:
            return None
        vendor = [d for d in rows if d["usage_page"] in VENDOR_PAGES]
        return (vendor or rows)[0]["path"]

    def _open(self, path) -> bool:
        import ctypes
        # Non-exclusive, or macOS demands Input Monitoring and the remote
        # stops working as a remote for as long as we hold it.
        for attr in ("hidapi", "lib"):
            obj = getattr(hid, attr, None)
            if isinstance(obj, ctypes.CDLL):
                try:
                    fn = obj.hid_darwin_set_open_exclusive
                    fn.argtypes = [ctypes.c_int]
                    fn.restype  = None
                    fn(0)
                except AttributeError:
                    pass
                break
        try:
            self._dev  = hid.Device(path=path)
            self._path = path
            return True
        except Exception as e:
            _plog.info(f"open failed: {e}")
            return False

    def _read(self, timeout_ms=50):
        """One read that survives the BLE link hiccuping.

        The binding turns any hidapi -1 into an exception, including a
        useless 'HIDException: Success' when there is no error string at
        all. A show cannot fall over for that.
        """
        try:
            r = self._dev.read(LONG_LEN, timeout_ms)
            return bytes(r) if r else None
        except Exception:
            return None

    def _drain(self):
        while self._read(0):
            pass

    def _request(self, feature_index, function, params=b"", timeout=0.6):
        """One HID++ request, waiting for its reply.

        Drains first: consecutive requests to the same feature and function
        are identical in the header and a reply echoes no parameters, so a
        late reply to the previous request is indistinguishable from this
        one's - which silently shifts a whole feature enumeration by one.
        """
        self._drain()
        msg = bytes([HIDPP_LONG, self._index, feature_index,
                     ((function & 0x0F) << 4) | SW_ID]) + bytes(params)
        msg = msg.ljust(LONG_LEN, b"\0")
        try:
            with self._write_lock:
                self._dev.write(msg)
        except Exception as e:
            _plog.info(f"write failed: {e}")
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self._read(100)
            if not r or r[0] != HIDPP_LONG:
                continue
            if r[2] == 0xFF:                       # HID++ error
                return None
            if r[2] == feature_index and r[3] == msg[3]:
                return r
        return None

    def _feature(self, feature_id):
        r = self._request(0x00, 0x00,
                          bytes([(feature_id >> 8) & 0xFF, feature_id & 0xFF]))
        return r[4] if r and r[4] else None

    def _handshake(self) -> bool:
        """Confirm HID++ answers and locate the features we need."""
        for index in (0xFF, 0x01, 0x02):
            self._index = index
            if self._request(0x00, 0x00, b"\x00\x01") is not None:
                break
        else:
            return False
        for fid in (FEAT_CONTROLS, FEAT_HAPTIC, FEAT_BATTERY):
            idx = self._feature(fid)
            if idx is not None:
                self._feat[fid] = idx
        if FEAT_CONTROLS not in self._feat:
            _plog.info("no 0x1b04 - cannot read buttons")
            return False
        self._read_battery()
        return True

    def _read_battery(self):
        idx = self._feat.get(FEAT_BATTERY)
        if idx is None:
            return
        r = self._request(idx, 0x00)
        if r:
            self.battery = r[4]

    def _get_divert(self, cid) -> bool | None:
        idx = self._feat.get(FEAT_CONTROLS)
        if idx is None:
            return None
        r = self._request(idx, 0x02, bytes([(cid >> 8) & 0xFF, cid & 0xFF]))
        return bool(r[6] & 0x01) if r else None

    def _set_divert(self, cid, on: bool):
        """Divert is what makes a control report over HID++ at all. It does
        not suppress the control's normal function here - the panel still
        left-clicks while diverted - so this only adds a reporting channel
        rather than taking the remote over.

        The prior state is remembered and put back on the way out. Restoring
        to "off" unconditionally would be wrong: this remote ships with its
        panel control already diverted, and blindly clearing it leaves the
        remote silent for whatever runs next.
        """
        idx = self._feat.get(FEAT_CONTROLS)
        if idx is None:
            return
        if on and cid not in self._divert_was:
            self._divert_was[cid] = self._get_divert(cid)
        self._request(idx, 0x03,
                      bytes([(cid >> 8) & 0xFF, cid & 0xFF,
                             0x03 if on else 0x02]))

    def buzz(self, times=1):
        """Queue a haptic. Returns immediately.

        This used to pulse inline, sleeping between pulses - which stalled
        the read loop long enough to miss a button's release event. The CID
        then stayed in the held set and the NEXT press registered as no
        change at all, so pressing quickly did nothing. The motor gets its
        own thread for that reason.
        """
        self._buzz_q.append(times)

    def _buzz_worker(self):
        while self._running:
            if not self._buzz_q:
                time.sleep(0.01)
                continue
            times = self._buzz_q.popleft()
            idx = self._feat.get(FEAT_HAPTIC)
            if idx is None or self._dev is None:
                continue
            for i in range(times):
                if i:
                    time.sleep(0.09)
                try:
                    for fn, params in ((0x02, bytes([0x01, 0x64])),
                                       (0x04, bytes([0x07]))):
                        msg = bytes([HIDPP_LONG, self._index, idx,
                                     ((fn & 0x0F) << 4) | SW_ID]) + params
                        with self._write_lock:
                            self._dev.write(msg.ljust(LONG_LEN, b"\0"))
                except Exception:
                    break

    def _held_cids(self, report):
        """The set of controls currently down, or None if not a button event.

        The diverted-buttons event carries every held control in one message,
        so an empty set is the release of everything.
        """
        if report[0] != HIDPP_LONG:
            return None
        if report[2] != self._feat.get(FEAT_CONTROLS) or (report[3] >> 4) != 0:
            return None
        cids = [(report[4 + 2 * i] << 8) | report[5 + 2 * i] for i in range(4)]
        return {c for c in cids if c}

    def _apply(self, step):
        """Run one trim step and answer with the matching buzz: one for up,
        two for down, three when the press did nothing."""
        ok = False
        try:
            ok = bool(self._nudge(step))
        except Exception as e:
            log.warning(f"[presenter] iso nudge failed: {e}")
        if not ok:
            self.buzz(3)
            self._note(f"{'up' if step > 0 else 'down'} - no change")
        else:
            self.buzz(1 if step > 0 else 2)
            self._note(f"ISO {'up' if step > 0 else 'down'}")

    def _loop(self):
        announced = False
        while self._running:
            if self._dev is None:
                path = self._find()
                if path is None:
                    if not announced:
                        log.info("[presenter] waiting for Spotlight (it drops "
                                 "off Bluetooth when idle - press a button)")
                        _plog.info("waiting for device")
                        announced = True
                    time.sleep(RESCAN_SECS)
                    continue
                if not self._open(path) or not self._handshake():
                    self._close()
                    time.sleep(RESCAN_SECS)
                    continue
                for cid in CID_STEP:
                    self._set_divert(cid, True)
                self.connected = True
                announced = False
                batt = f" batt={self.battery}%" if self.battery is not None else ""
                log.info(f"[presenter] Spotlight connected{batt}")
                _plog.info(f"connected index={self._index:#04x} "
                           f"features={ {hex(k): v for k, v in self._feat.items()} }")
                self._note("remote connected")

            held      = set()          # controls currently down
            last_seen = time.time()

            while self._running:
                # Block briefly for the first report, then take everything
                # else already queued. Handling one report per 50ms pass
                # would let a quick press and its release pile up, and a
                # missed release means the next press looks like no change.
                batch = []
                r = self._read(50)
                while r is not None:
                    batch.append(r)
                    r = self._read(0)
                now = time.time()

                for r in batch:
                    last_seen = now
                    cids = self._held_cids(r)
                    if cids is not None:
                        # Step on the down edge of each newly pressed control,
                        # so a press acts immediately rather than waiting to
                        # see what follows it.
                        for cid in cids - held:
                            step = CID_STEP.get(cid)
                            if step is not None:
                                self._apply(step)
                        held = cids

                # Silence alone means nothing - the remote is simply idle
                # most of the time. What matters is whether the handle we
                # hold still refers to the device that is there now: a sleep
                # and wake brings it back under a NEW path, leaving this one
                # open, silent and forever "connected". Compare paths, not
                # mere presence.
                if now - last_seen > 10.0:
                    last_seen = now
                    path_now = self._find()
                    if path_now is None:
                        log.info("[presenter] Spotlight gone - rescanning")
                        self._note("remote lost")
                        self._close()
                        break
                    if path_now != self._path:
                        log.info("[presenter] Spotlight re-paired - reconnecting")
                        _plog.info(f"path changed {self._path} -> {path_now}")
                        self._note("remote re-paired")
                        self._close()
                        break
                    self._read_battery()

        self._close()

    def _close(self):
        if self._dev is not None:
            try:
                for cid, was in self._divert_was.items():
                    self._set_divert(cid, bool(was))
                self._dev.close()
            except Exception:
                pass
        self._dev = None
        self._path = None
        self._divert_was = {}
        self.connected = False


presenter = Presenter()
