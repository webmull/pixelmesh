# (c) Adam Davis - adamdavis.co.uk
"""Nudge sim phone windows around so they are not nailed to the pixel grid.

Real audience phones never hold still: hands shake, people sway, someone
shifts in their seat.  A sim crowd rendered at fixed coordinates is a
best case the real show never gets, and it leaves the detector's
diff-based phone finder and centroid tracking completely untested.

Windows are moved over the Chrome DevTools Protocol rather than through
System Events, so this needs no Accessibility permission: sim.sh gives
each phone a --remote-debugging-port and this drives Browser.setWindowBounds
on it.

Each window random-walks around its home position, clamped to a radius
that sim.sh computed from the slack in its layout cell, so jitter can
never push two phones into overlapping.  Driven by sim.sh --jitter; not
meant to be run by hand.

    python3 tools/sim_jitter.py <state-file> <amplitude-px> [interval-s]

The state file has one line per phone:  port home_x home_y w h slack_x slack_y
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import urllib.request

import websockets


class Phone:
    """One sim window, walking around its home position."""

    def __init__(self, port, home_x, home_y, w, h, slack_x, slack_y, amp):
        self.port = port
        self.home_x, self.home_y = home_x, home_y
        self.w, self.h = w, h
        # Never wander further than the empty space around the window in its
        # layout cell, whatever amplitude was asked for - overlapping phones
        # would make the run useless.
        self.rx = max(0, min(amp, slack_x))
        self.ry = max(0, min(amp, slack_y))
        self.ox = self.oy = 0.0
        self.sent = None
        self.ws = None
        self.window_id = None

    async def connect(self):
        url = f"http://127.0.0.1:{self.port}/json/version"
        info = await asyncio.to_thread(
            lambda: json.load(urllib.request.urlopen(url, timeout=5)))
        self.ws = await websockets.connect(info["webSocketDebuggerUrl"],
                                           max_size=None, open_timeout=10)
        await self.ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
        targets = json.loads(await self.ws.recv())["result"]["targetInfos"]
        page = next(t for t in targets if t["type"] == "page")
        await self.ws.send(json.dumps({
            "id": 2, "method": "Browser.getWindowForTarget",
            "params": {"targetId": page["targetId"]}}))
        self.window_id = json.loads(await self.ws.recv())["result"]["windowId"]

    def step(self):
        """One tick of a bounded random walk.

        A walk rather than an independent random offset per tick: a phone
        that teleports to a new spot every 150ms reads as noise, while a
        walk drifts the way a held phone actually does.  The pull back
        toward home keeps it from resting against the clamp.
        """
        for axis in ("x", "y"):
            r = self.rx if axis == "x" else self.ry
            if r <= 0:
                continue
            o = self.ox if axis == "x" else self.oy
            o += random.uniform(-1, 1) * max(1.0, r / 3)
            o -= o * 0.15                     # drift home
            o = max(-r, min(r, o))
            if axis == "x":
                self.ox = o
            else:
                self.oy = o
        return int(round(self.home_x + self.ox)), int(round(self.home_y + self.oy))

    async def nudge(self):
        pos = self.step()
        if pos == self.sent:
            return   # sub-pixel tick, nothing to send
        self.sent = pos
        await self.ws.send(json.dumps({
            "id": 3, "method": "Browser.setWindowBounds",
            "params": {"windowId": self.window_id,
                       "bounds": {"left": pos[0], "top": pos[1]}}}))
        await self.ws.recv()


async def run(phone: Phone, interval: float):
    try:
        await phone.connect()
    except Exception:
        return   # window never came up, or was closed before we got to it
    # Stagger the fleet so 50 phones don't all fire on the same tick.
    await asyncio.sleep(random.uniform(0, interval))
    while True:
        try:
            await phone.nudge()
        except Exception:
            return   # window closed; sim.sh owns the lifecycle, so just stop
        await asyncio.sleep(interval)


async def main():
    state    = sys.argv[1]
    amp      = int(sys.argv[2])
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 0.15

    phones = []
    with open(state) as fh:
        for line in fh:
            if not line.strip():
                continue
            port, hx, hy, w, h, sx, sy = (int(v) for v in line.split())
            phones.append(Phone(port, hx, hy, w, h, sx, sy, amp))

    await asyncio.gather(*(run(p, interval) for p in phones))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
