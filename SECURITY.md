# Security

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** button on the
[Security tab](https://github.com/webmull/pixelmesh/security/advisories/new). That opens a
private advisory. Please do not open a public issue for anything exploitable.

This is a personal project, not a product. There is no on-call rota and no patch SLA. What
you will get is an acknowledgement within a week or so, and an honest answer about whether it
will be fixed or documented as a known limitation.

## What counts

The interesting boundary is the show server, `server.py`, and the ngrok edge in front of it.
Things worth reporting:

- A way past `AdminTokenMiddleware` to a write route.
- A way to make `_is_local_request()` return true for tunnelled traffic, which would unlock
  the seven routes in `_ADMIN_PUBLIC`.
- Anything that reaches the live camera feed, a debug recording or a calibration log from
  outside the show laptop.
- Anything in the phone client that leaks one audience member's state to another.

## What does not count, because it is already documented

The threat model is written out in the README under
[Security, and what is deliberately open](README.md#security-and-what-is-deliberately-open).
Read it before reporting. In short:

- **Seven admin routes have no token.** They are listed in `_ADMIN_PUBLIC` and guarded by
  `_is_local_request()` instead, because the talk deck driving a show is a static HTML file
  that cannot hold a token `run.sh` regenerates every launch.
- **`/internal/*` and `/debug-files/*` are unauthenticated.** They are the operator's own
  surface and the design assumes loopback.
- **Locality is decided by ngrok's forwarding headers.** That is sound for a tunnel that
  always sets them and unsound behind a reverse proxy that does not.

Each of those is a deliberate trade for a system that runs on a laptop at the front of a room
with the operator standing next to it. Pointing at one of them is not a finding. Showing that
one bites in the intended deployment is.

## If you run this yourself

The assumed deployment is a single laptop on loopback with a tunnel in front of it. Bind the
server to a LAN address or front it with a proxy that does not set forwarding headers, and on
that network anyone can end your show and read the live camera. Put the whole thing behind
authentication you control.

Recordings and calibration logs on disk are video of identifiable people. They are gitignored
for that reason. They are yours to look after.
