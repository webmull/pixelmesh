#!/bin/zsh
# PixelMesh V2 — interactive launcher

cd "$(dirname "$0")"

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

status_line() {
  local srv=$(pid_of_server)
  local ctl=$(pid_of_controller)
  local ngk=$(pid_of_ngrok)

  local srv_s="${R}stopped${RESET}"
  local ctl_s="${R}stopped${RESET}"
  local ngk_s="${R}stopped${RESET}"

  [[ -n $srv ]] && srv_s="${G}running${RESET} ${DIM}(pid $srv)${RESET}"
  [[ -n $ctl ]] && ctl_s="${G}running${RESET} ${DIM}(pid $ctl)${RESET}"
  [[ -n $ngk ]] && ngk_s="${G}running${RESET} ${DIM}(pid $ngk)${RESET}"

  local clients=""
  if [[ -n $srv ]]; then
    local count
    count=$(curl -s --max-time 1 -H "X-Admin-Token: $PIXELMESH_ADMIN_TOKEN" http://localhost:8000/admin/clients 2>/dev/null | grep -o '"clients":[0-9]*' | grep -o '[0-9]*')
    [[ -n $count ]] && clients="  ${DIM}(${count} connected)${RESET}"
  fi

  echo "  ${W}server     ${RESET}$srv_s$clients"
  echo "  ${W}controller ${RESET}$ctl_s"
  echo "  ${W}ngrok      ${RESET}$ngk_s"
  echo ""
  if [[ -n $LAST_STARTED ]]; then
    echo "  ${DIM}last started  $LAST_STARTED${RESET}"
    echo ""
  fi
  echo "  ${DIM}feed     → http://localhost:8000/internal/feed/v1${RESET}"
  echo "  ${DIM}public   → https://pixelmesh.show${RESET}"
  echo ""
}

# ─────────────────────────────────────────────
#  Actions
# ─────────────────────────────────────────────
kill_all() {
  echo "${Y}→ Stopping all processes...${RESET}"
  # Disarm the watchdog first so an intentional stop is not resurrected.
  rm -f "$WATCHDOG_FLAG"
  # Fire all kill signals in parallel — pixelmesh's own ngrok is scoped
  # by its config file so other projects' tunnels are left alone.
  lsof -ti tcp:8000 | xargs kill -9 2>/dev/null || true
  pkill -9 -f "uvicorn server:app"  2>/dev/null || true
  pkill -9 -f "controller.py"       2>/dev/null || true
  pkill -9 -f "ngrok.pixelmesh.yml" 2>/dev/null || true

  # Verify each one is actually gone before start_all runs.  An async
  # kill can lag by hundreds of ms; without this wait the next start
  # races against the corpse and produces "address already in use" or a
  # silent second controller running with stale state.
  local i=0
  while (( i < 20 )); do
    local stuck=0
    lsof -ti tcp:8000                   &>/dev/null && stuck=1
    pgrep -f "uvicorn server:app"       &>/dev/null && stuck=1
    pgrep -f "controller.py"            &>/dev/null && stuck=1
    pgrep -f "ngrok.pixelmesh.yml"      &>/dev/null && stuck=1
    (( stuck == 0 )) && break
    # Re-send the kill in case the first signal lost the race with a
    # forking child (preview thread, uvicorn worker, etc.).
    if (( i == 5 )); then
      pkill -9 -f "uvicorn server:app"  2>/dev/null || true
      pkill -9 -f "controller.py"       2>/dev/null || true
      pkill -9 -f "ngrok.pixelmesh.yml" 2>/dev/null || true
    fi
    sleep 0.25; (( i++ ))
  done

  if (( i >= 20 )); then
    echo "${R}  warning: some processes refused to die after 5s${RESET}"
    pgrep -af "uvicorn server:app|controller.py|ngrok.pixelmesh.yml" || true
  else
    echo "${G}  done.${RESET}"
  fi
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

start_all() {
  # Camera Hub must be up before the controller: it owns the Elgato's
  # exposure state and elgato.py connects to its WebSocket at startup.
  if ! pgrep -f "Camera Hub" >/dev/null; then
    echo "${Y}→ Starting Elgato Camera Hub...${RESET}"
    open -gja "Elgato Camera Hub" 2>/dev/null || true
    sleep 2
  fi

  echo "${Y}→ Starting server...${RESET}"
  $PYTHON -m uvicorn server:app --host 0.0.0.0 --port 8000 \
    >> /tmp/pixelmesh-server.log 2>&1 &

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

  echo "${Y}→ Starting controller...${RESET}"
  local i=0
  while ! curl -s --max-time 1 http://localhost:8000/health &>/dev/null && (( i < 20 )); do
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
