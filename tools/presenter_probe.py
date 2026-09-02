# (c) Adam Davis - adamdavis.co.uk
"""Probe the Logitech Spotlight 2 over HID++, so the ISO-trim wiring is
built on what the device actually does rather than on what the docs claim.

The Spotlight talks a Logitech-proprietary protocol (HID++ 2.0) on a
vendor collection alongside its normal keyboard/mouse/consumer ones. Over
Bluetooth that collection is usage page 0xFF43, usage 0x0202: 20-byte
"long" reports, report ID 0x11. Everything interesting lives there -
feature discovery, diverting controls away from their normal keystrokes,
the touch panel's raw mode, and the haptic motor.

The device is opened NON-exclusively (hidapi on macOS seizes by default,
which would stop the remote working as a remote while we listen). So the
probe is safe to run mid-setup: slides keep advancing, the cursor keeps
moving, we just watch.

Usage:
    python3.14 tools/presenter_probe.py                    # discover + live dump
    python3.14 tools/presenter_probe.py --census 45 --divert-all
    python3.14 tools/presenter_probe.py --gesture 45       # feel presenter.py
    python3.14 tools/presenter_probe.py --buzz             # haptic only
    python3.14 tools/presenter_probe.py --discover         # features, then stop

A control only reports over HID++ while it is DIVERTED, so --divert-all is
usually what you want when hunting for one. Whatever this diverts is put
back exactly as it was found.

To identify a control, press ONLY that control and read the live output. A
census of "press everything" tells you which ids exist but not which button
sends which - guessing that mapping is how 0x00fc was once mistaken for the
laser pointer button, which is really 0x01b0.

No macOS permission is needed: the device is opened non-exclusively. An
exclusive open is what trips the Input Monitoring requirement.
"""

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH    = os.path.join(_ROOT, "debug", "presenter_probe.log")

class _Tee:
    """Everything printed also lands in debug/presenter_probe.log, so the
    guided run can happen in Adam's own terminal (where he can see the
    prompts) while the result is still readable afterwards."""

    def __init__(self, stream, path):
        self.stream = stream
        self.file = open(path, "w", encoding="utf-8")

    def write(self, s):
        self.stream.write(s)
        # Countdown lines rewrite themselves with \r; keep them out of the log.
        if not s.endswith("\r"):
            self.file.write(s)
        return len(s)

    def flush(self):
        self.stream.flush()
        self.file.flush()

try:
    import hid
except ImportError:
    sys.exit("hid module missing. brew install hidapi && "
             "pip3.14 install --break-system-packages hid")

VID = 0x046D
PID_SPOTLIGHT_2 = 0xB506

# Logitech HID++ vendor collections. BLE devices expose 0xFF43; devices on
# a USB receiver expose 0xFF00. Usage 0x0202/0x02 is the 20-byte long
# report, which is the only size Bluetooth accepts.
VENDOR_PAGES = (0xFF43, 0xFF00)

HIDPP_LONG = 0x11          # report ID
LONG_LEN   = 20            # report ID + device index + feature + func + 16 params
SW_ID      = 0x0A          # software id, echoed back so replies can be matched

# Feature IDs worth naming in the dump. The device reports its own list,
# so this is only for legibility - unknown ids still print as hex.
FEATURES = {
    0x0000: "root",
    0x0001: "feature set",
    0x0003: "device info",
    0x0005: "device name",
    0x1000: "battery",
    0x1001: "battery voltage",
    0x1004: "unified battery",
    0x1b04: "reprogrammable controls v4",
    0x1e00: "enable hidden features",
    0x19b0: "haptic",
    0x2110: "smart shift",
    0x2201: "adjustable dpi",
    0x6100: "touchpad raw XY",
    0x6110: "touch mouse raw points",
}

# Control IDs seen on the original Spotlight, plus the standard mouse ones.
# Confirmed or corrected by the live dump; never assumed.
KNOWN_CIDS = {
    0x0050: "left click",
    0x0051: "right click",
    0x0052: "middle click",
    0x00d7: "pointer/highlight",
    0x00da: "next",
    0x00dc: "back",
    0x01b0: "laser pointer button",
}

# Feature index of 0x1b04 on this device, filled in during discovery so
# decode() can tell a diverted-button event from any other HID++ traffic.
_IDX_1B04 = None


def find_device():
    """Return (path, [collections]) for the Spotlight, or (None, []).

    Matched by the vendor collection rather than by product id alone, so a
    Spotlight reached through its USB receiver is found too.
    """
    # Strictly the Spotlight. Adam has other Logitech kit paired (a G309
    # mouse, spare receivers) that also speaks HID++ on the same vendor
    # collection, and quietly probing the wrong device produces a feature
    # list that looks plausible and describes something else entirely.
    rows = [d for d in hid.enumerate(VID, 0)
            if d["product_id"] == PID_SPOTLIGHT_2
            or "spotlight" in (d["product_string"] or "").lower()]
    if not rows:
        return None, []
    # macOS merges a BLE device's collections into one IOHIDDevice, so every
    # row shares a path; a receiver splits them and the vendor one is wanted.
    vendor = [d for d in rows if d["usage_page"] in VENDOR_PAGES]
    return (vendor or rows)[0]["path"], rows


def set_non_exclusive() -> bool:
    """Stop hidapi seizing the device on macOS.

    Its Darwin backend opens exclusively by default, which would take the
    remote away from the system for as long as we hold it open - no cursor,
    no slide advance. The switch lives in libhidapi but the python binding
    does not wrap it, so it is called through the ctypes handle (named
    `hidapi` here; `lib` is only the library path).
    """
    import ctypes
    for attr in ("hidapi", "lib"):
        obj = getattr(hid, attr, None)
        if not isinstance(obj, ctypes.CDLL):
            continue
        try:
            fn = obj.hid_darwin_set_open_exclusive
        except AttributeError:
            continue
        fn.argtypes = [ctypes.c_int]
        fn.restype  = None
        fn(0)
        return True
    return False


def open_all(rows):
    """Open every distinct collection the remote exposes.

    macOS sometimes merges a BLE device's collections behind one path and
    sometimes splits them. When they are split, opening only the vendor
    collection means the HID++ button events arrive but the mouse and
    consumer reports - where a scroll's DIRECTION lives - never do. Since
    direction is the whole point here, open whatever is separate and read
    the lot.
    """
    opened = []
    for path in dict.fromkeys(r["path"] for r in rows):     # unique, ordered
        labels = [f"{r['usage_page']:#06x}/{r['usage']:#04x}"
                  for r in rows if r["path"] == path]
        try:
            opened.append((" ".join(labels), open_device(path, fatal=False)))
        except Exception as e:
            print(f"  ! could not open {' '.join(labels)}: {e}")
    return [(lbl, d) for lbl, d in opened if d is not None]


def open_device(path, fatal=True):
    if not set_non_exclusive():
        print("  ! cannot set non-exclusive open; the remote may stop "
              "driving the cursor while this runs")
    try:
        return hid.Device(path=path)
    except hid.HIDException as e:
        if not fatal:
            return None
        if "privilege" in str(e).lower() or "0xE00002C1" in str(e):
            sys.exit(
                "\nmacOS refused the open (privilege violation).\n"
                "The Spotlight exposes a keyboard collection, so TCC gates it.\n\n"
                "Fix: System Settings > Privacy & Security > Input Monitoring,\n"
                "     then enable the terminal you are running this from\n"
                "     (Ghostty), and restart that terminal.\n\n"
                "     open 'x-apple.systempreferences:com.apple.preference."
                "security?Privacy_ListenEvent'\n")
        sys.exit(f"open failed: {e}")


class HidPP:
    """Minimal HID++ 2.0 request/response over one long-report channel."""

    def __init__(self, dev, index):
        self.dev   = dev
        self.index = index
        self.pending = []      # input reports read while waiting for a reply

    def drain(self):
        """Clear anything already queued before issuing a request.

        Consecutive requests to the same feature and function are identical
        in the header - only the payload differs - and a reply carries no
        echo of its parameters. So a late reply to request N looks exactly
        like the reply to request N+1, and the whole enumeration silently
        shifts by one. Draining first is what keeps the indices honest.
        """
        while True:
            try:
                r = self.dev.read(LONG_LEN, 0)
            except hid.HIDException:
                return
            if not r:
                return
            self.pending.append(bytes(r))

    def request(self, feature_index, function, params=b"", timeout=1.0):
        self.drain()
        msg = bytes([HIDPP_LONG, self.index, feature_index,
                     ((function & 0x0F) << 4) | SW_ID]) + bytes(params)
        msg = msg.ljust(LONG_LEN, b"\0")
        self.dev.write(msg)
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.dev.read(LONG_LEN, 100)
            if not r:
                continue
            if r[0] != HIDPP_LONG:
                self.pending.append(bytes(r))     # mouse/consumer traffic
                continue
            if r[2] == 0xFF:                      # HID++ 2.0 error
                return None
            if r[2] == feature_index and r[3] == msg[3]:
                return bytes(r)
            self.pending.append(bytes(r))         # an event, not our reply
        return None

    def feature_index(self, feature_id):
        """Ask root for a feature's index. 0 means the device lacks it."""
        r = self.request(0x00, 0x00,
                         bytes([(feature_id >> 8) & 0xFF, feature_id & 0xFF]))
        return r[4] if r and r[4] else None


def pick_device_index(dev):
    """Find which HID++ device index answers.

    A directly-connected Bluetooth device usually answers on 0xFF; one behind
    a receiver answers on its pairing slot. Both are tried rather than guessed.
    """
    for index in (0xFF, 0x01, 0x02):
        pp = HidPP(dev, index)
        if pp.request(0x00, 0x00, b"\x00\x01") is not None:
            return pp
    return None


def dump_features(pp):
    """Enumerate everything the device supports, via the feature-set feature."""
    fs = pp.feature_index(0x0001)
    if fs is None:
        print("  ! no feature set - cannot enumerate")
        return {}
    r = pp.request(fs, 0x00)
    count = r[4] if r else 0
    print(f"  {count} features (plus root):")
    found = {}
    for i in range(count + 1):
        r = pp.request(fs, 0x01, bytes([i]))
        if not r:
            continue
        fid = (r[4] << 8) | r[5]
        flags = r[6]
        tags = []
        if flags & 0x80: tags.append("obsolete")
        if flags & 0x40: tags.append("hidden")
        if flags & 0x20: tags.append("engineering")
        found[fid] = i
        print(f"    idx {i:#04x}  {fid:#06x}  {FEATURES.get(fid, '?'):<28}"
              f"{' '.join(tags)}")
    return found


def dump_controls(pp, idx_1b04):
    """List the remote's control IDs and whether each can be diverted."""
    r = pp.request(idx_1b04, 0x00)
    if not r:
        return []
    count = r[4]
    print(f"  {count} controls:")
    cids = []
    for i in range(count):
        r = pp.request(idx_1b04, 0x01, bytes([i]))
        if not r:
            continue
        cid  = (r[4] << 8) | r[5]
        task = (r[6] << 8) | r[7]
        flags = r[8]
        caps  = []
        if flags & 0x01: caps.append("mouse-button")
        if flags & 0x02: caps.append("fkey")
        if flags & 0x04: caps.append("hotkey")
        if flags & 0x08: caps.append("fn-toggle")
        if flags & 0x10: caps.append("reprogrammable")
        if flags & 0x20: caps.append("DIVERTABLE")
        if flags & 0x40: caps.append("persist-divert")
        cids.append((cid, flags))
        print(f"    cid {cid:#06x}  task {task:#06x}  "
              f"{KNOWN_CIDS.get(cid, ''):<22}{' '.join(caps)}")
    return cids


def get_divert(pp, idx_1b04, cid):
    """Whether this control is currently diverted, or None if unreadable.

    Worth asking rather than assuming off: the Spotlight ships with its
    panel control ALREADY diverted, which is why it reports at all. Blindly
    "restoring" it to undiverted on exit silences the remote for the next
    run - which is exactly what happened once already.
    """
    r = pp.request(idx_1b04, 0x02, bytes([(cid >> 8) & 0xFF, cid & 0xFF]))
    return bool(r[6] & 0x01) if r else None


def set_divert(pp, idx_1b04, cid, on):
    """Divert one control: its presses arrive as HID++ events instead of
    keystrokes, and it stops emitting its normal usage. Surgical - other
    controls keep working as a presenter."""
    flags = 0x03 if on else 0x02      # bit0 = divert value, bit1 = "I mean it"
    pp.request(idx_1b04, 0x03,
               bytes([(cid >> 8) & 0xFF, cid & 0xFF, flags]))


def buzz(pp, idx_19b0, waveform=0x07, level=0x64):
    """Spotlight 2 haptic: set the global level, then play a built-in
    waveform. 0x07 is 'Completed', the short confirm tick."""
    pp.request(idx_19b0, 0x02, bytes([0x01, level]))
    pp.request(idx_19b0, 0x04, bytes([waveform]))


# HID keyboard usages worth naming: these are what currently leaks into
# whatever app has focus, and Escape in particular quits the controller.
KEYS = {0x28: "Enter", 0x29: "Esc", 0x2c: "Space", 0x4a: "Home", 0x4b: "PgUp",
        0x4d: "End", 0x4e: "PgDn", 0x4f: "Right", 0x50: "Left", 0x51: "Down",
        0x52: "Up", 0x3e: "F5", 0x05: "b", 0x1a: "w"}


def decode(report: bytes) -> str:
    """One-line reading of an input report, so the dump is scannable."""
    rid = report[0]
    if rid == HIDPP_LONG:
        feat, func = report[2], report[3] >> 4
        if feat == _IDX_1B04 and func == 0:
            # divertedButtonsEvent: up to 4 big-endian CIDs, all currently
            # held. An all-zero payload is the release of everything.
            cids = [(report[4 + 2 * i] << 8) | report[5 + 2 * i] for i in range(4)]
            cids = [c for c in cids if c]
            if not cids:
                return "BUTTON  release"
            named = ", ".join(f"{c:#06x} {KNOWN_CIDS.get(c, '?')}" for c in cids)
            return f"BUTTON  down: {named}"
        return f"HID++ feature idx {feat:#04x} event {func}  {report[4:].hex(' ')}"
    if rid == 0x01:
        keys = [KEYS.get(k, f"{k:#04x}") for k in report[3:] if k]
        return (f"keyboard mods={report[1]:#04x} "
                f"{('+'.join(keys) or 'release'):<12} raw {report[1:].hex(' ')}")
    if rid == 0x02:
        btn = report[1]
        dx  = int.from_bytes(report[2:4], "little", signed=True)
        dy  = int.from_bytes(report[4:6], "little", signed=True)
        return (f"mouse buttons={btn:#04x} dx={dx:+5d} dy={dy:+5d}"
                f"   raw {report[1:].hex(' ')}")
    if rid == 0x03:
        return f"consumer {report[1:].hex(' ')}"
    return f"report {rid:#04x} {report[1:].hex(' ')}"


def read_tolerant(dev, timeout_ms=100):
    """One read that survives the link hiccuping.

    A BLE remote drops and re-negotiates constantly, and the binding turns
    any hidapi -1 into an exception - including the useless
    'HIDException: Success' when there is no error string at all. A live
    show cannot fall over for that, so transient failures read as "no data"
    and only a sustained run of them is worth reporting.
    """
    try:
        r = dev.read(64, timeout_ms)
        read_tolerant.errors = 0
        return bytes(r) if r else None
    except hid.HIDException:
        read_tolerant.errors = getattr(read_tolerant, "errors", 0) + 1
        if read_tolerant.errors in (20, 200):
            print(f"      ! {read_tolerant.errors} consecutive read errors "
                  f"- is the remote still awake?")
        time.sleep(0.05)
        return None


def live(channels, seconds):
    """Print every event as it happens, decoded, so a control can be named
    by pressing it and reading the screen - no fixed timing to race.

    Motion is buffered and flushed once it stops, so a scroll gesture prints
    as one line with its direction and magnitude rather than 200 samples.
    """
    print("\n" + "=" * 64)
    print(f"LIVE - {seconds}s. Press things; each is named as it arrives.")
    print("For ISO the one that matters is UP / DOWN on the scroll.")
    print("=" * 64 + "\n")
    end = time.time() + seconds
    burst = None          # [n, dx, dy, wheel, last_seen]
    while time.time() < end:
        r, src = None, ""
        for label, d in channels:
            r = read_tolerant(d, timeout_ms=0)
            if r:
                src = label
                break
        if r is None:
            time.sleep(0.005)
        now = time.time()
        if r and r[0] == 0x02:
            dx = int.from_bytes(r[2:4], "little", signed=True)
            dy = int.from_bytes(r[4:6], "little", signed=True)
            # Byte 6 is the wheel on a Logitech mouse report; a scroll
            # gesture shows here while a pointer sweep shows in dx/dy.
            wheel = int.from_bytes(r[6:7], "little", signed=True) if len(r) > 6 else 0
            if burst is None:
                burst = [0, 0, 0, 0, now]
            burst[0] += 1
            burst[1] += dx
            burst[2] += dy
            burst[3] += wheel
            burst[4] = now
            continue
        # Flush a finished motion burst before printing anything else.
        if burst and (now - burst[4] > 0.25 or r):
            n, dx, dy, wheel, _ = burst
            print(f"  {time.strftime('%H:%M:%S')}  MOTION  {n:3d} reports  "
                  f"dx {dx:+5d}  dy {dy:+5d}  wheel {wheel:+4d}"
                  + ("   <-- SCROLL" if wheel else ""))
            burst = None
        if r:
            print(f"  {time.strftime('%H:%M:%S')}  [{src}]  {decode(r)}")
    if burst:
        n, dx, dy, wheel, _ = burst
        print(f"  MOTION  {n:3d} reports  dx {dx:+5d}  dy {dy:+5d}  "
              f"wheel {wheel:+4d}")
    print("\ndone - nothing was diverted, remote left exactly as it was")



def census(channels, seconds=45, relink=None):
    """Answer one question: which control ids does this remote emit?

    No labelling, no ordering, no timing to get right - press things and
    this reports the distinct ids that appeared. Note what it CANNOT tell
    you: which physical button produced which id. Pressing everything at
    once and inferring the mapping is how 0x00fc got mistaken for the laser
    button. To identify one control, press only that control, and use live().
    """
    print("\n" + "=" * 66)
    print(f"CONTROL CENSUS - {seconds}s.")
    print("Press EVERY physical control you can find, several times each:")
    print("  the touch panel, both arrow buttons, any side or back button,")
    print("  the laser pointer button - and hold each one too.")
    print("=" * 66 + "\n")

    seen  = {}                 # cid -> [count, first_seen]
    other = {}                 # non-button reports, by decoded line
    start = time.time()
    quiet = start
    while time.time() - start < seconds:
        got = False
        for label, d in channels:
            r = read_tolerant(d, timeout_ms=0)
            if not r:
                continue
            got = True
            quiet = time.time()
            if r[0] == HIDPP_LONG and r[2] == _IDX_1B04 and (r[3] >> 4) == 0:
                cids = [(r[4 + 2 * i] << 8) | r[5 + 2 * i] for i in range(4)]
                for c in [c for c in cids if c]:
                    if c not in seen:
                        seen[c] = [0, time.time() - start]
                        print(f"  {time.time() - start:5.1f}s  NEW CONTROL "
                              f"{c:#06x}  {KNOWN_CIDS.get(c, '?')}")
                    seen[c][0] += 1
            else:
                line = decode(r)
                if line not in other:
                    other[line] = 0
                    print(f"  {time.time() - start:5.1f}s  non-button: {line}")
                other[line] += 1
        if not got:
            now = time.time()
            if relink is not None and now - quiet > 6.0:
                quiet = now
                fresh = relink()
                if fresh:
                    channels = fresh
                    print("  ! remote had re-paired - reopened, carry on")
            time.sleep(0.004)

    print("\n" + "=" * 66)
    print("distinct controls seen:")
    for cid, (count, first) in sorted(seen.items()):
        print(f"  {cid:#06x}  {KNOWN_CIDS.get(cid, '?'):<18} "
              f"x{count:<4} first at {first:.1f}s")
    if other:
        print("other report types:")
        for line, count in other.items():
            print(f"  x{count:<4} {line}")
    if len(seen) >= 2:
        print("\n>>> MORE THAN ONE CONTROL - true up/down is possible.")
    elif len(seen) == 1:
        print("\n>>> ONE CONTROL ONLY. Every physical press on this remote "
              "reports\n    the same id, so the gesture has to carry the "
              "direction after all.")
    else:
        print("\n>>> NOTHING CAPTURED - link was probably asleep. Worth a retry.")
    print("=" * 66)


def gesture_test(seconds):
    """Drive presenter.py for real, with ISO replaced by a print.

    This is how the hold threshold gets tuned: the same code the show will
    run, minus the camera, so a click that feels like a hold shows up here
    instead of during a set.
    """
    sys.path.insert(0, _ROOT)
    import presenter as P

    trim = [0]

    def nudge(direction):
        trim[0] += direction * 5
        kind = "PANEL   -> up  " if direction > 0 else "POINTER -> down"
        print(f"  {time.strftime('%H:%M:%S')}  {kind}   trim now {trim[0]:+d}")
        return abs(trim[0]) < 60          # False near the ends, to feel the
                                          # "did nothing" triple buzz

    print(f"\ntap the PANEL = up (one buzz), "
          f"LASER POINTER button = down (two buzzes)")
    print(f"listening {seconds}s...\n")
    P.presenter.start(nudge_iso=nudge)
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.2)
    P.presenter.stop()
    time.sleep(0.4)
    print(f"\nfinal trim {trim[0]:+d} - remote released, divert restored")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=int, nargs="?", const=45, default=None,
                    help="print events as they arrive, named (default mode)")
    ap.add_argument("--discover", action="store_true",
                    help="stop after feature/control discovery")
    ap.add_argument("--buzz", action="store_true",
                    help="fire the haptic and exit")
    ap.add_argument("--census", type=int, nargs="?", const=45, default=None,
                    help="press every control; report the distinct ids seen")
    ap.add_argument("--divert-all", action="store_true",
                    help="divert every control the device lists, so each one "
                         "reports its id instead of its normal function")
    ap.add_argument("--gesture", type=int, nargs="?", const=45, default=None,
                    help="run presenter.py's click/hold layer with a printing "
                         "callback, to feel the timing before it drives ISO")
    ap.add_argument("--divert", default="",
                    help="comma-separated control ids to divert during the "
                         "watch, e.g. 0xda,0xdc")
    args = ap.parse_args()

    sys.stdout = _Tee(sys.stdout, LOG_PATH)
    print(f"(logging to {LOG_PATH})")

    if args.gesture:
        gesture_test(args.gesture)
        return

    path, rows = find_device()
    if not path:
        sys.exit("No Spotlight found. It sleeps off the Bluetooth list when "
                 "idle - press any button on it and run this again.")

    print("collections on this device:")
    for d in rows:
        print(f"  usage page {d['usage_page']:#06x} usage {d['usage']:#06x}  "
              f"path {d['path'].decode(errors='replace')}")
    print(f"opening HID++ channel {path.decode(errors='replace')}")
    dev = open_device(path)
    print("  open OK (non-exclusive)")

    pp = pick_device_index(dev)
    if pp is None:
        sys.exit("  ! no HID++ reply on any device index. The vendor "
                 "collection may not be reachable over this transport.")
    print(f"  HID++ answering on device index {pp.index:#04x}\n")

    print("features:")
    feats = dump_features(pp)

    idx_19b0 = feats.get(0x19b0) or pp.feature_index(0x19b0)
    if args.buzz:
        if idx_19b0 is None:
            sys.exit("no haptic feature on this device")
        print("\nbuzzing...")
        buzz(pp, idx_19b0)
        return

    global _IDX_1B04
    idx_1b04 = feats.get(0x1b04) or pp.feature_index(0x1b04)
    _IDX_1B04 = idx_1b04
    cids = []
    if idx_1b04 is not None:
        print("\ncontrols:")
        cids = dump_controls(pp, idx_1b04)

    if 0x6100 in feats:
        print("\n  touchpad raw XY (0x6100) IS present - the touch panel can "
              "report raw coordinates, which is the clean way to drive ISO")

    if idx_19b0 is not None:
        print("\nhaptic: firing one tick - did it buzz?")
        buzz(pp, idx_19b0)
        time.sleep(0.5)

    diverted = []
    wanted = []
    if args.divert_all:
        wanted = [cid for cid, _flags in cids]
    elif args.divert:
        wanted = [int(tok, 0) for tok in args.divert.split(",")]
    if wanted and idx_1b04 is not None:
        for cid in wanted:
            was = get_divert(pp, idx_1b04, cid)
            set_divert(pp, idx_1b04, cid, True)
            diverted.append((cid, was))
        print("\ndiverted: " + ", ".join(
            f"{c:#04x}{'' if w is None else (' (was on)' if w else '')}"
            for c, w in diverted))

    try:
        if args.discover:
            pass
        else:
            # Read every collection, and never the same one twice: when macOS
            # merges them behind one path the HID++ handle already carries the
            # lot, and a second handle on that path duplicates every report.
            extra = [r for r in rows if r["path"] != path]
            channels = [("hid++ / merged", dev)] + open_all(extra)
            print("\nlistening on: " + ", ".join(lbl for lbl, _ in channels))

            open_path = [path]

            def relink():
                """Reopen if the remote has come back under a new path."""
                new_path, new_rows = find_device()
                if new_path is None or new_path == open_path[0]:
                    return None
                open_path[0] = new_path
                new_dev = open_device(new_path, fatal=False)
                if new_dev is None:
                    return None
                extra_now = [r for r in new_rows if r["path"] != new_path]
                return [("hid++ / merged", new_dev)] + open_all(extra_now)
            if args.census:
                census(channels, args.census, relink=relink)
            else:
                live(channels, args.live or 45)
    finally:
        # Hand the remote back exactly as it was found - restoring to "off"
        # regardless would silence a control that shipped diverted.
        for cid, was in diverted:
            set_divert(pp, idx_1b04, cid, bool(was))
        if diverted:
            print("restored: " + ", ".join(
                f"{c:#04x}->{'on' if w else 'off'}" for c, w in diverted))
        dev.close()


if __name__ == "__main__":
    main()
