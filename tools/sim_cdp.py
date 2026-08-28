# (c) Adam Davis - adamdavis.co.uk
"""Drive sim phones over the Chrome DevTools protocol.

Two jobs, both things a window cannot do for itself:

Shrink.  Chrome will not make a window narrower than about 86px, so past
roughly 20 phones the windows cannot get small enough to keep the crowd
inside its share of the camera frame, and the detector's noise gate
climbs until it hides the weaker phones.  The lit area has no such
floor: #card-blink is its own fixed-position element, so the window can
sit at Chrome's minimum while the blinking patch inside it is inset to
whatever the coverage budget allows, black all around.  That lifts the
ceiling on this machine from about 20 phones to a few hundred, and it
looks more like the real thing - a phone in a dark auditorium is a small
bright rectangle, not a wall-to-wall glow.

Jitter.  Real phones never hold still: hands shake, people sway, someone
shifts in their seat.  A crowd nailed to the pixel grid is a best case
the show never gets, and it leaves the diff-based phone finder and the
centroid tracking untested.  Each window random-walks around its home
position, clamped to the slack sim.sh measured in its layout cell, so a
wobble can never push two phones into overlapping.

Going through CDP rather than System Events means no Accessibility
permission is needed.  Driven by sim.sh; not meant to be run by hand.

    python3 tools/sim_cdp.py <state-file> <jitter-px> [interval-s]

State file, one line per phone:
    port home_x home_y w h slack_x slack_y inset_pct
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

    def __init__(self, port, home_x, home_y, w, h, slack_x, slack_y, inset, amp):
        self.port = port
        self.inset = inset
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
        self.page_ws = None
        self.window_id = None

    async def _endpoints(self):
        base = f"http://127.0.0.1:{self.port}"
        ver = await asyncio.to_thread(
            lambda: json.load(urllib.request.urlopen(base + "/json/version", timeout=5)))
        lst = await asyncio.to_thread(
            lambda: json.load(urllib.request.urlopen(base + "/json/list", timeout=5)))
        page = next(p for p in lst if p["type"] == "page")
        return ver["webSocketDebuggerUrl"], page["webSocketDebuggerUrl"]

    async def shrink(self):
        """Inset #card-blink so the lit area matches the coverage budget.

        Registered for future loads as well as applied to the current one:
        the client reloads itself whenever the server ships a new app.js, and
        a phone that came back full-size would quietly blow the budget for
        the whole crowd.
        """
        if self.inset <= 0:
            return
        css = ("(()=>{let s=document.getElementById('sim-shrink');"
               "if(!s){s=document.createElement('style');s.id='sim-shrink';"
               "document.documentElement.appendChild(s);}"
               f"s.textContent='#card-blink{{inset:{self.inset}% !important}}';"
               "return 1;})()")
        await self.page_ws.send(json.dumps({
            "id": 10, "method": "Page.addScriptToEvaluateOnNewDocument",
            "params": {"source": css}}))
        await self.page_ws.recv()
        await self.page_ws.send(json.dumps({
            "id": 11, "method": "Runtime.evaluate",
            "params": {"expression": css, "returnByValue": True}}))
        await self.page_ws.recv()

    async def connect(self):
        url = f"http://127.0.0.1:{self.port}/json/version"
        browser_url, page_url = await self._endpoints()
        self.page_ws = await websockets.connect(page_url, max_size=None,
                                                open_timeout=10)
        await self.shrink()
        if self.rx <= 0 and self.ry <= 0:
            return   # shrink-only phone, no need for the browser endpoint
        self.ws = await websockets.connect(browser_url, max_size=None,
                                           open_timeout=10)
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
    if phone.ws is None:
        # Shrink applied, nothing to move. Hold the page connection open so
        # the reload hook stays registered for the life of the run.
        while True:
            await asyncio.sleep(3600)
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
            port, hx, hy, w, h, sx, sy, inset = (int(v) for v in line.split())
            phones.append(Phone(port, hx, hy, w, h, sx, sy, inset, amp))

    await asyncio.gather(*(run(p, interval) for p in phones))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
