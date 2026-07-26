"""Load-test harness: can this machine hold a Brighton-sized crowd?

Simulates N audience phones against a running pixelmesh server: each
client connects to /ws, says hello, waits for its blink assignment, then
behaves like the real app (heartbeat ping every 10s, sync_ping every 5s
as an RTT probe). Mid-hold, a slice of the crowd fires a like storm so
the broadcast fan-out path gets exercised at full house.

Usage (server must already be running, e.g. via run.sh or uvicorn):
    python3 tools/load_test.py                     # 500 clients
    python3 tools/load_test.py --clients 200 --ramp 50 --hold 30

Reports connect/assign success and timing percentiles, RTT under load,
like fan-out spread across the whole crowd, and the server process's
CPU/RSS over the run, with pass/fail verdicts at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import subprocess
import time

import websockets


def pct(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


class Stats:
    def __init__(self):
        self.connect_fail = 0
        self.assign_times = []      # seconds from connect start to "assigned"
        self.assign_timeout = 0
        self.rtts = []              # sync_ping round-trips, ms
        self.drops = 0              # unexpected closes after assignment
        self.like_recv_ts = []      # wall-clock of each client's first like_count bump
        self.connected_now = 0
        self.peak_connected = 0


async def client(i: int, args, stats: Stats, storm_gate: asyncio.Event,
                 storm_members: set, stop: asyncio.Event):
    scheme = "wss" if args.wss else "ws"
    uri = f"{scheme}://{args.host}/ws"
    device_id = f"loadtest-{i:04d}"
    t0 = time.time()
    try:
        ws = await websockets.connect(uri, ping_interval=None, open_timeout=15)
    except Exception:
        stats.connect_fail += 1
        return
    assigned = False
    baseline_likes = None
    try:
        await ws.send(json.dumps({"type": "hello", "device_id": device_id}))
        last_hb = last_sync = time.time()
        sync_sent = {}
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                raw = None
            except websockets.ConnectionClosed:
                if assigned:
                    stats.drops += 1
                return
            if raw is not None:
                try:
                    msg = json.loads(raw)
                except Exception:
                    msg = {}
                mtype = msg.get("type")
                if mtype == "assigned" and not assigned:
                    assigned = True
                    stats.assign_times.append(time.time() - t0)
                    stats.connected_now += 1
                    stats.peak_connected = max(stats.peak_connected,
                                               stats.connected_now)
                elif mtype == "sync_pong":
                    sent = sync_sent.pop(msg.get("client_time"), None)
                    if sent is not None:
                        stats.rtts.append((time.time() - sent) * 1000)
                elif mtype == "like_count":
                    n = msg.get("count", 0)
                    if baseline_likes is None:
                        baseline_likes = n
                    elif n > baseline_likes and len(stats.like_recv_ts) < args.clients:
                        stats.like_recv_ts.append(time.time())
                        baseline_likes = n

            if not assigned and time.time() - t0 > 15:
                stats.assign_timeout += 1
                return

            now = time.time()
            if assigned and now - last_hb >= 10:
                await ws.send(json.dumps({"type": "ping"}))
                last_hb = now
            if assigned and now - last_sync >= 5:
                ct = int(now * 1000) + i  # unique key per client
                sync_sent[ct] = now
                await ws.send(json.dumps({"type": "sync_ping",
                                          "client_time": ct}))
                last_sync = now

            if assigned and i in storm_members and storm_gate.is_set():
                storm_members.discard(i)
                for _ in range(5):
                    await ws.send(json.dumps({"type": "like_tap"}))
                    await asyncio.sleep(0.05)
    finally:
        if assigned:
            stats.connected_now -= 1
        try:
            await ws.close()
        except Exception:
            pass


async def monitor_server(samples: list, stop: asyncio.Event):
    try:
        pid = subprocess.check_output(
            ["pgrep", "-f", "uvicorn server:app"], text=True
        ).split()[0]
    except Exception:
        print("!! server process not found - resource stats unavailable")
        return
    while not stop.is_set():
        try:
            out = subprocess.check_output(
                ["ps", "-o", "rss=,pcpu=", "-p", pid], text=True).split()
            samples.append((float(out[1]), int(out[0]) / 1024))
        except Exception:
            break
        await asyncio.sleep(2)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=500)
    ap.add_argument("--host", default="127.0.0.1:8000",
                    help="host[:port], or a bare domain with --wss")
    ap.add_argument("--wss", action="store_true",
                    help="connect wss:// (TLS) - use for the public domain")
    ap.add_argument("--ramp", type=int, default=100, help="new clients per second")
    ap.add_argument("--hold", type=int, default=45, help="seconds at full load")
    args = ap.parse_args()

    # Preflight: this process needs ~1 fd per client
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    need = args.clients + 256
    if soft < need:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(need, hard), hard))
        print(f"raised harness fd limit {soft} -> {min(need, hard)}")
    print(f"fd limits: harness={resource.getrlimit(resource.RLIMIT_NOFILE)[0]} "
          f"(server inherits its own shell's ulimit - check if connects fail)")

    stats = Stats()
    stop = asyncio.Event()
    storm_gate = asyncio.Event()
    storm_members = set(range(0, args.clients, max(1, args.clients // 50)))
    samples: list = []

    mon = asyncio.create_task(monitor_server(samples, stop))
    print(f"ramping {args.clients} clients at {args.ramp}/s "
          f"against {'wss' if args.wss else 'ws'}://{args.host}/ws ...")
    tasks = []
    t_start = time.time()
    for i in range(args.clients):
        tasks.append(asyncio.create_task(
            client(i, args, stats, storm_gate, storm_members, stop)))
        if (i + 1) % args.ramp == 0:
            await asyncio.sleep(1)
            print(f"  {i+1} launched, {stats.connected_now} assigned, "
                  f"{stats.connect_fail} failed")

    # settle, then hold
    await asyncio.sleep(5)
    ramp_secs = time.time() - t_start
    print(f"ramp complete in {ramp_secs:.0f}s: {stats.connected_now} connected")

    half = args.hold / 2
    await asyncio.sleep(half)
    print("firing like storm from ~50 clients ...")
    storm_t0 = time.time()
    storm_gate.set()
    await asyncio.sleep(half)

    stop.set()
    await asyncio.sleep(2)
    for t in tasks:
        t.cancel()
    mon.cancel()

    # ---- report ----
    n = args.clients
    ok = len(stats.assign_times)
    cpu = [s[0] for s in samples]
    rss = [s[1] for s in samples]
    fan = ([t - storm_t0 for t in stats.like_recv_ts]
           if stats.like_recv_ts else [])

    print("\n" + "=" * 58)
    print(f"  LOAD TEST - {n} clients @ "
          f"{'wss' if args.wss else 'ws'}://{args.host}")
    print("=" * 58)
    print(f"  connected+assigned   {ok}/{n}  "
          f"(fail={stats.connect_fail} timeout={stats.assign_timeout} "
          f"dropped={stats.drops})")
    print(f"  peak concurrent      {stats.peak_connected}")
    if stats.assign_times:
        print(f"  time-to-assign       p50={pct(stats.assign_times,50)*1000:.0f}ms  "
              f"p95={pct(stats.assign_times,95)*1000:.0f}ms  "
              f"max={max(stats.assign_times)*1000:.0f}ms")
    if stats.rtts:
        print(f"  RTT under load       p50={pct(stats.rtts,50):.0f}ms  "
              f"p95={pct(stats.rtts,95):.0f}ms  p99={pct(stats.rtts,99):.0f}ms  "
              f"max={max(stats.rtts):.0f}ms  (n={len(stats.rtts)})")
    if fan:
        print(f"  like fan-out         first={min(fan)*1000:.0f}ms  "
              f"p95={pct(fan,95)*1000:.0f}ms  last={max(fan)*1000:.0f}ms  "
              f"reached {len(fan)} clients")
    if cpu:
        print(f"  server CPU           avg={statistics.mean(cpu):.0f}%  "
              f"max={max(cpu):.0f}%")
        print(f"  server RSS           max={max(rss):.0f}MB")

    verdicts = [
        ("connect rate >= 99%", ok >= n * 0.99),
        ("zero drops during hold", stats.drops == 0),
        ("assign p95 < 2s", bool(stats.assign_times) and pct(stats.assign_times, 95) < 2),
        ("RTT p95 < 250ms", bool(stats.rtts) and pct(stats.rtts, 95) < 250),
        ("server CPU max < 80%", bool(cpu) and max(cpu) < 80),
    ]
    print("-" * 58)
    for name, passed in verdicts:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print("=" * 58)


if __name__ == "__main__":
    asyncio.run(main())
