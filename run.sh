#!/bin/zsh
# (c) Adam Davis - adamdavis.co.uk
# pixelmesh V2 — interactive launcher

cd "$(dirname "$0")"

# Canonical, case-correct absolute path to this directory. $(pwd) reports
# whichever case you happened to type, because macOS is case-insensitive, so
# ./run.sh from ~/desktop and from ~/Desktop produce different strings for the
# same file. The sudoers rule that stops the front door asking for a password
# every single time has to match one fixed string, so it gets this one.
# Exported so the tmux re-exec below does not recompute it from a different cwd.
export PIXELMESH_DIR="${PIXELMESH_DIR:-${0:A:h}}"

# The server's port. Not 8000: that is every framework's default, and a dev
# server from another project left on it takes the audience tunnel with it -
# ngrok dials localhost:<port>, and a loopback bind beats the wildcard one, so
# the phones get that project's site and no error anywhere. 16924 is P-I-X.
export PIXELMESH_PORT="${PIXELMESH_PORT:-16924}"

# Local show mode. Set PIXELMESH_LOCAL=1 when the room has no internet and a
# router resolves pixelmesh.show to this laptop instead of to ngrok's edge.
# It swaps the tunnel for a local TLS front door (caddy.pixelmesh.conf) holding
# a real certificate for the same name, which matters more than it sounds:
# navigator.wakeLock is secure-context only, so on plain http every phone in
# the room dims on its own auto-lock timer mid-show, and there is no fallback
# in app.js. navigator.share goes the same way and the end card quietly drops
# its share button.
export PIXELMESH_LOCAL="${PIXELMESH_LOCAL:-}"
export PIXELMESH_CERT="${PIXELMESH_CERT:-$HOME/.pixelmesh/letsencrypt/live/pixelmesh.show/fullchain.pem}"
export PIXELMESH_KEY="${PIXELMESH_KEY:-$HOME/.pixelmesh/letsencrypt/live/pixelmesh.show/privkey.pem}"
# joinmesh.io is the name in the QR for a local show: a public A record pointing
# at this laptop's static LAN address, so every resolver returns it and the
# router needs no DNS setup. Short on purpose - it is 33 QR modules against 37
# for a longer name, which is more pixels per module on the deck's panel and so
# scan distance from the back of a big hall. This is the certificate the audience actually
# meets, so it is the one a short expiry refuses on.
export PIXELMESH_LOCAL_CERT="${PIXELMESH_LOCAL_CERT:-$HOME/.pixelmesh/letsencrypt/live/joinmesh.io/fullchain.pem}"
export PIXELMESH_LOCAL_KEY="${PIXELMESH_LOCAL_KEY:-$HOME/.pixelmesh/letsencrypt/live/joinmesh.io/privkey.pem}"

# Refuse a local show on a certificate with less than this left. It is issued
# out of band by certbot over DNS-01 and renewing needs internet, which is the
# one thing a local show does not have. Finding that out in the venue is too
# late, so the check is a start-time refusal rather than a warning.
_CERT_MIN_DAYS=7

# ─────────────────────────────────────────────
#  Wrap in tmux so the session persists across
#  detach/attach.
# ─────────────────────────────────────────────
if [[ -z "$TMUX" && -z "$PIXELMESH_IN_TMUX" ]]; then
  export PIXELMESH_IN_TMUX=1
  # Re-attach if session already exists AND it still has pixelmesh processes
  # running inside it.  A stale session (e.g. left over from days ago after
  # the processes died) gets recycled rather than silently re-attached —
  # otherwise the menu shows everything stopped with no clear cause.
  if tmux has-session -t pixelmesh 2>/dev/null; then
    if pgrep -f "uvicorn server:app" &>/dev/null \
       || pgrep -f "controller.py" &>/dev/null \
       || pgrep -f "ngrok.pixelmesh.yml" &>/dev/null; then
      exec tmux attach-session -t pixelmesh
    else
      echo "Found stale pixelmesh tmux session with no live processes — recycling."
      tmux kill-session -t pixelmesh 2>/dev/null || true
    fi
  fi
  exec tmux new-session -s pixelmesh "$0" "$@"
fi

# ─────────────────────────────────────────────
#  Colours
# ─────────────────────────────────────────────
R=$'\e[0;31m'  G=$'\e[0;32m'  Y=$'\e[0;33m'
C=$'\e[0;36m'  W=$'\e[0;37m'  B=$'\e[1;34m'
DIM=$'\e[2m'   BOLD=$'\e[1m'  RESET=$'\e[0m'

LAST_STARTED=""

# ─────────────────────────────────────────────
#  Admin token (generated once per session)
# ─────────────────────────────────────────────
if [[ -z $PIXELMESH_ADMIN_TOKEN ]]; then
  PIXELMESH_ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")
  export PIXELMESH_ADMIN_TOKEN
fi

# ─────────────────────────────────────────────
#  ASCII header
# ─────────────────────────────────────────────
header() {
  clear
  echo "${C}"
  echo "  ██████╗ ██╗██╗  ██╗███████╗██╗     ███╗   ███╗███████╗███████╗██╗  ██╗"
  echo "  ██╔══██╗██║╚██╗██╔╝██╔════╝██║     ████╗ ████║██╔════╝██╔════╝██║  ██║"
  echo "  ██████╔╝██║ ╚███╔╝ █████╗  ██║     ██╔████╔██║█████╗  ███████╗███████║"
  echo "  ██╔═══╝ ██║ ██╔██╗ ██╔══╝  ██║     ██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║"
  echo "  ██║     ██║██╔╝ ██╗███████╗███████╗██║ ╚═╝ ██║███████╗███████║██║  ██║"
  echo "  ╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝"
  echo "${DIM}                                                               V2${RESET}"
  echo "${DIM}                                                 by Adam Davis${RESET}"
  echo ""
}

# ─────────────────────────────────────────────
#  Status helpers
# ─────────────────────────────────────────────
pid_of_server()     { pgrep -f "uvicorn server:app"   | head -1; }
pid_of_controller() { pgrep -f "controller.py"        | head -1; }
# Only match pixelmesh's own ngrok (via its config file), so unrelated
# ngrok tunnels from other projects don't fool the status check or get
# killed by [r]/[d].
pid_of_ngrok()      { pgrep -f "ngrok.pixelmesh.yml"  | head -1; }
# Same scoping for the local TLS front door, so an unrelated caddy is neither
# reported as ours nor killed by [r]/[d]. Runs as root for ports 443 and 80,
# hence the sudo on the pkills below.
pid_of_caddy()      { pgrep -f "caddy.pixelmesh.conf" | head -1; }

# Everything a local show needs before anything is started. Echoes the days
# left on the certificate so the caller can report it, and explains any refusal
# itself. Checked up front because failing halfway through start_all leaves a
# server running with no controller and no front door, which is a worse place
# to debug from than a clean stop.
local_mode_preflight() {
  if ! command -v caddy >/dev/null 2>&1; then
    echo "${R}  caddy is not installed - brew install caddy${RESET}" >&2
    return 1
  fi
  # Caddy loads both certificates at startup and refuses to start if either
  # file is missing, so both are checked here rather than only the one the
  # audience meets.
  local f
  for f in "$PIXELMESH_CERT" "$PIXELMESH_KEY" "$PIXELMESH_LOCAL_CERT" "$PIXELMESH_LOCAL_KEY"; do
    [[ -f $f ]] && continue
    echo "${R}  missing $f${RESET}" >&2
    echo "${R}  a local show needs both certificates - see docs/operations.md${RESET}" >&2
    return 1
  done

  # The tunnel's certificate expiring does not stop a local show, so it warns
  # rather than refuses. The one in the QR is the one that must be good.
  local other
  other=$(cert_days_left "$PIXELMESH_CERT")
  if [[ -n $other ]] && (( other < _CERT_MIN_DAYS )); then
    echo "${Y}  note: the pixelmesh.show certificate has ${other} days left${RESET}" >&2
  fi

  local days
  days=$(cert_days_left "$PIXELMESH_LOCAL_CERT") || {
    echo "${R}  no certificate at $PIXELMESH_LOCAL_CERT${RESET}" >&2
    echo "${R}  a local show needs one - see docs/operations.md${RESET}" >&2
    return 1
  }
  if (( days < _CERT_MIN_DAYS )); then
    if (( days < 0 )); then
      echo "${R}  certificate expired $(( -days )) days ago${RESET}" >&2
    elif (( days == 0 )); then
      echo "${R}  certificate expires today - too close for a show${RESET}" >&2
    elif (( days == 1 )); then
      echo "${R}  certificate expires tomorrow - too close for a show${RESET}" >&2
    else
      echo "${R}  certificate expires in ${days} days - too close for a show${RESET}" >&2
    fi
    echo "${R}  renew it while there is still internet - see docs/operations.md${RESET}" >&2
    return 1
  fi
  echo "$days"
}

# Days until the certificate at $1 expires, defaulting to the one in the QR.
# Empty if there is no such cert.
cert_days_left() {
  local cert="${1:-$PIXELMESH_LOCAL_CERT}"
  [[ -f $cert ]] || return 1
  local raw end_ts
  raw=$(openssl x509 -in "$cert" -noout -enddate 2>/dev/null | cut -d= -f2)
  [[ -n $raw ]] || return 1
  end_ts=$(date -j -f "%b %e %T %Y %Z" "$raw" "+%s" 2>/dev/null) || return 1
  echo $(( (end_ts - $(date "+%s")) / 86400 ))
}

status_line() {
  local srv=$(pid_of_server)
  local ctl=$(pid_of_controller)
  local ngk=$(pid_of_ngrok)
  local cad=$(pid_of_caddy)

  local srv_s="${R}stopped${RESET}"
  local ctl_s="${R}stopped${RESET}"
  local ngk_s="${R}stopped${RESET}"
  local cad_s="${R}stopped${RESET}"

  [[ -n $srv ]] && srv_s="${G}running${RESET} ${DIM}(pid $srv)${RESET}"
  [[ -n $ctl ]] && ctl_s="${G}running${RESET} ${DIM}(pid $ctl)${RESET}"
  [[ -n $ngk ]] && ngk_s="${G}running${RESET} ${DIM}(pid $ngk)${RESET}"
  [[ -n $cad ]] && cad_s="${G}running${RESET} ${DIM}(pid $cad)${RESET}"

  local clients=""
  if [[ -n $srv ]]; then
    local count
    count=$(curl -s --max-time 1 -H "X-Admin-Token: $PIXELMESH_ADMIN_TOKEN" http://localhost:$PIXELMESH_PORT/admin/clients 2>/dev/null | grep -o '"clients":[0-9]*' | grep -o '[0-9]*')
    [[ -n $count ]] && clients="  ${DIM}(${count} connected)${RESET}"
  fi

  echo "  ${W}server     ${RESET}$srv_s$clients"
  echo "  ${W}controller ${RESET}$ctl_s"
  if [[ -n $PIXELMESH_LOCAL ]]; then
    local days=$(cert_days_left)
    local certnote=""
    [[ -n $days ]] && certnote="  ${DIM}(cert ${days}d left)${RESET}"
    echo "  ${W}front door ${RESET}$cad_s$certnote"
  else
    echo "  ${W}ngrok      ${RESET}$ngk_s"
  fi
  echo ""
  if [[ -n $LAST_STARTED ]]; then
    echo "  ${DIM}last started  $LAST_STARTED${RESET}"
    echo ""
  fi
  echo "  ${DIM}feed     → http://localhost:$PIXELMESH_PORT/internal/feed/v1${RESET}"
  if [[ -n $PIXELMESH_LOCAL ]]; then
    echo "  ${DIM}public   → https://joinmesh.io ${RESET}${DIM}(this laptop, across the room)${RESET}"
  else
    echo "  ${DIM}public   → https://pixelmesh.show${RESET}"
  fi
  echo ""
}

# ─────────────────────────────────────────────
#  Actions
# ─────────────────────────────────────────────
# Seconds to allow for a graceful exit before escalating to SIGKILL.
# Generous on purpose: the controller catches SIGTERM and closes any open
# recording, and ffmpeg only writes an mp4's moov atom when its stdin closes.
# With +faststart it then rewrites the index, which on a multi-GB file is not
# instant.  A -9 here is what used to leave unplayable recordings behind.
# The ceiling comes from the controller's own bounds: debug capture joins its
# writer for 5s, then waits up to 30s on ffmpeg.
_TERM_GRACE_SECS=40

kill_all() {
  echo "${Y}→ Stopping all processes...${RESET}"
  # Disarm the watchdog first so an intentional stop is not resurrected.
  rm -f "$WATCHDOG_FLAG"

  # SIGTERM, not SIGKILL.  The controller has a handler that finalises any
  # open recording; uvicorn uses it to tell connected phones the show is over
  # rather than dropping their sockets.  pixelmesh's own ngrok is scoped by
  # its config file so other projects' tunnels are left alone.
  pkill -f "uvicorn server:app"  2>/dev/null || true
  pkill -f "controller.py"       2>/dev/null || true
  pkill -f "ngrok.pixelmesh.yml" 2>/dev/null || true
  # Root-owned, so this needs sudo, and prompting is right here: the menu is
  # interactive anyway. sudo -n looks safer but is worse - the credential
  # cached at start has long since timed out by the end of a show, so the kill
  # silently does nothing and then the wait loop below spends the full 40s
  # grace period on a process it was never going to kill, on every stop and
  # every reload. If it survives anyway, say so and stop waiting on it.
  local caddy_stuck=0
  if [[ -n $(pid_of_caddy) ]]; then
    sudo pkill -f "caddy.pixelmesh.conf" 2>/dev/null || true
    sleep 0.5
    if [[ -n $(pid_of_caddy) ]]; then
      caddy_stuck=1
      echo "${R}  the TLS front door survived and still holds 443${RESET}"
      echo "${R}  stop it with: sudo pkill -f caddy.pixelmesh.conf${RESET}"
    fi
  fi

  # Wait for a clean exit.  This also guards the next start_all: an async
  # kill can lag, and without the wait the next start races against the
  # corpse and produces "address already in use" or a silent second
  # controller running with stale state.
  local waited=0 announced=0
  local max=$(( _TERM_GRACE_SECS * 4 ))   # 0.25s ticks
  while (( waited < max )); do
    local stuck=0
    pgrep -f "uvicorn server:app"       &>/dev/null && stuck=1
    pgrep -f "controller.py"            &>/dev/null && stuck=1
    pgrep -f "ngrok.pixelmesh.yml"      &>/dev/null && stuck=1
    (( caddy_stuck == 0 )) && pgrep -f "caddy.pixelmesh.conf" &>/dev/null && stuck=1
    (( stuck == 0 )) && break
    # Anything past a second means real work is happening — an mp4 being
    # closed — so say so rather than looking hung.  Then count down: a silent
    # 40s wait is indistinguishable from a lock-up, and the whole point of
    # this loop is that it is sometimes meant to take a while.
    if (( waited == 4 && announced == 0 )); then
      announced=1
      echo "${DIM}  waiting for a clean exit (finalising any recording)${RESET}"
      echo "${DIM}  ctrl-c to stop waiting and force the kill${RESET}"
    fi
    if (( announced == 1 && waited % 4 == 0 )); then
      printf "\r${DIM}  %2ds left...${RESET}" $(( (max - waited) / 4 ))
    fi
    sleep 0.25; (( waited++ ))
  done
  (( announced == 1 )) && printf "\r\033[K"

  # Escalate only for whatever refused to go.  Anything still alive here has
  # had the full grace period, so its files are either written or already
  # lost — SIGKILL costs nothing further.
  if (( waited >= max )); then
    echo "${R}  warning: forcing kill after ${_TERM_GRACE_SECS}s${RESET}"
    pgrep -af "uvicorn server:app|controller.py|ngrok.pixelmesh.yml|caddy.pixelmesh.conf" || true
    pkill -9 -f "uvicorn server:app"  2>/dev/null || true
    pkill -9 -f "controller.py"       2>/dev/null || true
    pkill -9 -f "ngrok.pixelmesh.yml" 2>/dev/null || true
    sudo -n pkill -9 -f "caddy.pixelmesh.conf" 2>/dev/null || true
    sleep 0.5
  else
    echo "${G}  done.${RESET}"
  fi

  # Last resort for the port itself: a stray listener that is none of the
  # above still blocks the next start.
  lsof -ti "tcp:$PIXELMESH_PORT" -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
  sleep 0.3
}

PYTHON=python3.14
WATCHDOG_FLAG=/tmp/pixelmesh-watchdog

launch_controller() {
  PIXELMESH_LAUNCHED=1 $PYTHON controller.py >> /tmp/pixelmesh-controller.log 2>&1 &
  local ctl_pid=$!
  # No sleep, no display dim, no idle nap while the show runs. Tied to
  # the controller pid, so it exits with the controller.
  caffeinate -dis -w "$ctl_pid" &
}

# Auto-restart the controller if it dies (MaccTech: a projector unplug
# wedged the GUI and recovery was a manual [r]). Runs in the background;
# the flag file is its kill switch - kill_all removes it so intentional
# stops are never resurrected. Bounded: 3 restarts in 60s then give up
# loudly, so a crash-loop is visible rather than masked.
controller_watchdog() {
  local hub_tick=0
  while [[ -f $WATCHDOG_FLAG ]]; do
    sleep 2
    [[ -f $WATCHDOG_FLAG ]] || break
    # Camera Hub keepalive (checked every ~10s): the AE watchdog and ISO
    # control die with it, so it is show-critical. open -gja restarts it
    # in the background without stealing focus mid-show.
    (( hub_tick++ ))
    if (( hub_tick >= 5 )); then
      hub_tick=0
      if ! pgrep -f "Camera Hub" >/dev/null; then
        echo "$(date '+%H:%M:%S') watchdog: Camera Hub gone - relaunching" \
          >> /tmp/pixelmesh-controller.log
        open -gja "Elgato Camera Hub" 2>/dev/null || true
      fi
    fi
    if [[ -z $(pid_of_controller) ]]; then
      local now=$(date +%s)
      # keep only restarts from the last 60s in the flag file
      local recent=$(awk -v t=$((now-60)) '$1 > t' "$WATCHDOG_FLAG" 2>/dev/null)
      local count=$(echo -n "$recent" | grep -c . || true)
      if (( count >= 3 )); then
        echo "$(date '+%H:%M:%S') watchdog: controller died 4x in 60s - giving up"           >> /tmp/pixelmesh-controller.log
        rm -f "$WATCHDOG_FLAG"
        break
      fi
      { echo "$recent"; echo "$now"; } | grep . > "$WATCHDOG_FLAG"
      echo "$(date '+%H:%M:%S') watchdog: controller died - restarting ($((count+1))/3)"         >> /tmp/pixelmesh-controller.log
      launch_controller
      sleep 3   # grace so a fast crash doesn't double-count
    fi
  done
}

# The local-show alternative to the ngrok tunnel. Terminates TLS on 443 and
# reverse-proxies to the server's ordinary plain-http port, so server.py,
# network.py and the ngrok config all stay exactly as they are.
start_local_front_door() {
  # Already up, from a [r] reload that could not take it down, or a stray from
  # an earlier run. Starting a second one would fail to bind 443, and the pid
  # check below would then find the old process and report success.
  if [[ -n $(pid_of_caddy) ]]; then
    echo "${DIM}  front door already running (pid $(pid_of_caddy)) - reusing it${RESET}"
    return 0
  fi

  # A local show has no use for the tunnel. The QR sends everyone to
  # joinmesh.io, which resolves to this laptop from any resolver on earth, so
  # nobody is pointed at pixelmesh.show and a tunnel would only serve a name
  # nothing is asking for. It briefly earned its place as the fallback for
  # Android phones that stayed on mobile data, but a public record for the LAN
  # address solved that properly: those phones now resolve to this laptop too
  # and reach it over wifi, because a LAN-subnet destination beats the default
  # route.
  if [[ -n $(pid_of_ngrok) ]]; then
    echo "${Y}→ Local mode: stopping the ngrok tunnel...${RESET}"
    pkill -f "ngrok.pixelmesh.yml" 2>/dev/null || true
    sleep 1
  fi

  [[ -n ${_CERT_DAYS:-} ]] || _CERT_DAYS=$(cert_days_left)
  echo "${Y}→ Starting local TLS front door (cert has ${_CERT_DAYS} days left)...${RESET}"
  # Absolute path on purpose. sudoers can carry a secure_path that does not
  # include /opt/homebrew/bin, and "caddy: command not found" from inside a
  # backgrounded sudo would surface only as a front door that never came up.
  local caddy_bin
  caddy_bin=$(command -v caddy)
  # 443 and 80 are privileged, so this one process needs root. Prompt for it
  # up front rather than letting sudo block behind a repainted menu.
  # Caddy reads the {env.*} placeholders in the config from this file rather
  # than from sudo's environment. sudo's env handling depends on sudoers, and
  # a variable quietly dropped on the way to root would surface only as a
  # front door that never came up, minutes before doors.
  # Not /tmp: that is world-writable, and this file names the certificate and
  # key that a root process is about to load. Anything with a sudoers rule
  # pointing at it wants to live somewhere only this user can write.
  local envfile="$HOME/.pixelmesh/caddy.env"
  printf 'PIXELMESH_CERT=%s\nPIXELMESH_KEY=%s\nPIXELMESH_LOCAL_CERT=%s\nPIXELMESH_LOCAL_KEY=%s\nPIXELMESH_PORT=%s\n' \
    "$PIXELMESH_CERT" "$PIXELMESH_KEY" "$PIXELMESH_LOCAL_CERT" "$PIXELMESH_LOCAL_KEY" \
    "$PIXELMESH_PORT" > "$envfile" || {
      echo "${R}  could not write $envfile${RESET}"; return 1; }

  # No sudo -v first. It asks for a password even when a sudoers rule already
  # covers the command without one, which defeats the point of the rule; and
  # the obvious guard against that, testing sudo -n -l for the command, is
  # worthless here because it succeeds for anything an admin may run at all,
  # password or not. sudo -b authenticates before it forks, so it prompts at
  # this same point when there is no rule and says nothing when there is.
  sudo -b "$caddy_bin" run --adapter caddyfile \
          --config "$PIXELMESH_DIR/caddy.pixelmesh.conf" --envfile "$envfile" \
          >> /tmp/pixelmesh-caddy.log 2>&1 \
    || { echo "${R}  sudo refused - cannot bind 443${RESET}"; return 1; }

  local i=0
  while [[ -z $(pid_of_caddy) ]] && (( i < 20 )); do sleep 0.25; (( i++ )); done
  if [[ -z $(pid_of_caddy) ]]; then
    echo "${R}  front door did not start - see /tmp/pixelmesh-caddy.log${RESET}"
    return 1
  fi
}

start_all() {
  # In local mode the certificate and caddy are hard preconditions, so they are
  # settled before a single process is started.
  if [[ -n $PIXELMESH_LOCAL ]]; then
    _CERT_DAYS=$(local_mode_preflight) || return 1
  fi

  # Camera Hub must be up before the controller: it owns the Elgato's
  # exposure state and elgato.py connects to its WebSocket at startup.
  if ! pgrep -f "Camera Hub" >/dev/null; then
    echo "${Y}→ Starting Elgato Camera Hub...${RESET}"
    open -gja "Elgato Camera Hub" 2>/dev/null || true
    sleep 2
  fi

  # Refuse to start over someone else's listener. Before the bespoke port
  # this was the failure that ends a show before it starts: a dev server from
  # another project sat on the same port, ngrok dialled it, and every phone
  # was served that project's site with no error anywhere. Starting uvicorn
  # anyway just fails "address in use" after the health wait times out, which
  # is the same outcome with less to go on.
  local holder
  holder=$(lsof -nP -iTCP:"$PIXELMESH_PORT" -sTCP:LISTEN 2>/dev/null \
             | awk 'NR>1 {print $1" (pid "$2")"}' | sort -u | tr '\n' ' ')
  if [[ -n $holder ]]; then
    echo "${R}  port $PIXELMESH_PORT is already held by: ${holder}${RESET}"
    echo "${R}  not starting - the audience tunnel would reach that instead of pixelmesh${RESET}"
    return 1
  fi

  echo "${Y}→ Starting server...${RESET}"
  # --ws-per-message-deflate false: uvicorn's default keeps ~163KB of zlib
  #   state per connection (~41MB at 250 phones) and per-connection deflate on
  #   every broadcast costs the same order as the dumps-once optimisation
  #   saves. Show messages are small JSON; the feed socket carries JPEGs that
  #   do not compress at all.
  # --ws-max-size 1MB: phone messages are tiny, but the feed ingest socket
  #   receives ~220KB JPEG frames, so it cannot go lower than that. The 16MB
  #   default was just an oversized buffering ceiling per socket.
  # --no-access-log: the controller polls admin routes ~5 req/s all show;
  #   each poll was a synchronous log write on the event loop.
  $PYTHON -m uvicorn server:app --host 0.0.0.0 --port "$PIXELMESH_PORT" \
    --ws-per-message-deflate false --ws-max-size 1048576 --no-access-log \
    >> /tmp/pixelmesh-server.log 2>&1 &

  if [[ -n $PIXELMESH_LOCAL ]]; then
    start_local_front_door || return 1
    echo "${DIM}  no tunnel in local mode - the room's wifi is the only way in${RESET}"
  else
    echo "${Y}→ Starting ngrok (audience tunnel)...${RESET}"
    # ngrok 3.16 doesn't expose --pooling-enabled on the agent CLI (the
    # ERR_NGROK_334 hint to use it is misleading on this client) and
    # --region is deprecated — both removed.  ngrok v3 stops auto-loading
    # the default auth-token config the moment any explicit --config is
    # passed, so we still have to pass it alongside the project-local
    # tunnel definitions.  Default location on macOS is
    # ~/Library/Application Support/ngrok/ngrok.yml (path contains a space).
    NGROK_AUTH_CONFIG="$HOME/Library/Application Support/ngrok/ngrok.yml"
    ngrok start audience \
      --config "$NGROK_AUTH_CONFIG" \
      --config "$(pwd)/ngrok.pixelmesh.yml" \
      --log stdout \
      --log-format logfmt >> /tmp/pixelmesh-ngrok.log 2>&1 &
  fi

  echo "${Y}→ Starting controller...${RESET}"
  local i=0
  while ! curl -s --max-time 1 http://localhost:$PIXELMESH_PORT/health &>/dev/null && (( i < 20 )); do
    sleep 0.5; (( i++ ))
  done
  launch_controller

  : > "$WATCHDOG_FLAG"
  controller_watchdog &

  LAST_STARTED=$(date "+%d %b %Y  %H:%M:%S")
  echo "${G}  all started.${RESET}"
  sleep 1
}

# ─────────────────────────────────────────────
#  Auto-start on launch
# ─────────────────────────────────────────────
if [[ -z $(pid_of_server) && -z $(pid_of_controller) && -z $(pid_of_ngrok) ]]; then
  header
  start_all
fi

# ─────────────────────────────────────────────
#  Menu loop
# ─────────────────────────────────────────────
while true; do
  header
  status_line

  echo "  ${BOLD}${W}[s]${RESET} start      ${BOLD}${W}[r]${RESET} reload      ${BOLD}${W}[d]${RESET} die      ${BOLD}${W}[q]${RESET} quit"
  echo ""
  printf "  ${C}→ ${RESET}"
  read -r choice

  case $choice in
    s|S)
      header
      if [[ -n $(pid_of_server) || -n $(pid_of_controller) || -n $(pid_of_ngrok) ]]; then
        echo "${Y}  Already running — use [r] to reload.${RESET}"
        sleep 1.5
      else
        start_all
      fi
      ;;
    r|R)
      header
      kill_all
      start_all
      ;;
    d|D)
      header
      kill_all
      ;;
    q|Q)
      header
      kill_all
      echo "  ${DIM}bye.${RESET}"
      echo ""
      exit 0
      ;;
    *)
      ;;
  esac
done
