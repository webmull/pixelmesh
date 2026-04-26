#!/bin/zsh
# PixelMesh V2 — interactive launcher

cd "$(dirname "$0")"

# ─────────────────────────────────────────────
#  Wrap in tmux so the session persists and
#  can be reached remotely via the ttyd tunnel.
# ─────────────────────────────────────────────
if [[ -z "$TMUX" && -z "$PIXELMESH_IN_TMUX" ]]; then
  export PIXELMESH_IN_TMUX=1
  # Re-attach if session already exists, otherwise create it
  if tmux has-session -t pixelmesh 2>/dev/null; then
    exec tmux attach-session -t pixelmesh
  else
    exec tmux new-session -s pixelmesh "$0" "$@"
  fi
fi

# ─────────────────────────────────────────────
#  Ensure ttyd is available
# ─────────────────────────────────────────────
if ! command -v ttyd &>/dev/null; then
  echo "Installing ttyd..."
  brew install ttyd
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
pid_of_server()     { pgrep -f "uvicorn server:app" | head -1; }
pid_of_controller() { pgrep -f "controller.py"      | head -1; }
pid_of_ngrok()      { pgrep -f "ngrok"               | head -1; }

status_line() {
  local srv=$(pid_of_server)
  local ctl=$(pid_of_controller)
  local ngk=$(pid_of_ngrok)
  local tty=$(pid_of_ttyd)

  local srv_s="${R}stopped${RESET}"
  local ctl_s="${R}stopped${RESET}"
  local ngk_s="${R}stopped${RESET}"
  local tty_s="${R}stopped${RESET}"

  [[ -n $srv ]] && srv_s="${G}running${RESET} ${DIM}(pid $srv)${RESET}"
  [[ -n $ctl ]] && ctl_s="${G}running${RESET} ${DIM}(pid $ctl)${RESET}"
  [[ -n $ngk ]] && ngk_s="${G}running${RESET} ${DIM}(pid $ngk)${RESET}"
  [[ -n $tty ]] && tty_s="${G}running${RESET} ${DIM}(pid $tty)${RESET}"

  local clients=""
  if [[ -n $srv ]]; then
    local count
    count=$(curl -s --max-time 1 -H "X-Admin-Token: $PIXELMESH_ADMIN_TOKEN" http://localhost:8000/admin/clients 2>/dev/null | grep -o '"clients":[0-9]*' | grep -o '[0-9]*')
    [[ -n $count ]] && clients="  ${DIM}(${count} connected)${RESET}"
  fi

  # Fetch terminal tunnel URL from ngrok API
  local term_url=""
  term_url=$(curl -s --max-time 1 http://localhost:4040/api/tunnels 2>/dev/null | \
    python3 -c "
import sys, json
try:
    ts = json.load(sys.stdin).get('tunnels', [])
    t  = next((x for x in ts if x.get('config',{}).get('addr','').endswith('7681')), None)
    if t: print(t['public_url'])
except: pass
" 2>/dev/null)

  echo "  ${W}server     ${RESET}$srv_s$clients"
  echo "  ${W}controller ${RESET}$ctl_s"
  echo "  ${W}ngrok      ${RESET}$ngk_s"
  echo "  ${W}terminal   ${RESET}$tty_s"
  echo ""
  if [[ -n $LAST_STARTED ]]; then
    echo "  ${DIM}last started  $LAST_STARTED${RESET}"
    echo ""
  fi
  echo "  ${DIM}local    → http://localhost:8000/internal/dashboard${RESET}"
  echo "  ${DIM}public   → https://join.pixelmesh.live${RESET}"
  echo "  ${DIM}sim      → http://localhost:8000/internal/sim${RESET}"
  if [[ -n $term_url ]]; then
    echo "  ${C}terminal → $term_url${RESET}  ${DIM}(pixel / mesh)${RESET}"
  else
    echo "  ${DIM}terminal → starting...${RESET}"
  fi
  echo ""
}

# ─────────────────────────────────────────────
#  Actions
# ─────────────────────────────────────────────
kill_all() {
  echo "${Y}→ Stopping all processes...${RESET}"
  lsof -ti tcp:8000 | xargs kill -9 2>/dev/null || true
  lsof -ti tcp:7681 | xargs kill -9 2>/dev/null || true
  pkill -9 -f "uvicorn"       2>/dev/null || true
  pkill -9 -f "controller.py" 2>/dev/null || true
  pkill -9 -f "ngrok"         2>/dev/null || true
  pkill -9 -f "ttyd"          2>/dev/null || true

  # Wait until port 8000 is actually free (up to 5s)
  local i=0
  while lsof -ti tcp:8000 &>/dev/null && (( i < 10 )); do
    sleep 0.5; (( i++ ))
  done

  echo "${G}  done.${RESET}"
  sleep 0.3
}

pid_of_ttyd() { pgrep -f "ttyd" | head -1; }

start_all() {
  echo "${Y}→ Starting server...${RESET}"
  python3 -m uvicorn server:app --host 0.0.0.0 --port 8000 \
    >> /tmp/pixelmesh-server.log 2>&1 &

  echo "${Y}→ Starting ngrok (audience + terminal)...${RESET}"
  # Audience tunnel (reserved domain)
  ngrok http 8000 \
    --region eu \
    --hostname join.pixelmesh.live \
    --log stdout \
    --log-format logfmt >> /tmp/pixelmesh-ngrok.log 2>&1 &
  # Terminal tunnel (basic auth pixel:mesh, dynamic URL)
  ngrok start terminal \
    --config ~/.config/ngrok/ngrok.yml \
    --config "$(pwd)/ngrok.pixelmesh.yml" \
    --log stdout \
    --log-format logfmt >> /tmp/pixelmesh-ngrok-terminal.log 2>&1 &

  echo "${Y}→ Starting ttyd (web terminal on :7681)...${RESET}"
  ttyd -p 7681 -W tmux attach-session -t pixelmesh \
    >> /tmp/pixelmesh-ttyd.log 2>&1 &

  echo "${Y}→ Starting controller...${RESET}"
  local i=0
  while ! curl -s --max-time 1 http://localhost:8000/health &>/dev/null && (( i < 20 )); do
    sleep 0.5; (( i++ ))
  done
  PIXELMESH_LAUNCHED=1 python3 controller.py >> /tmp/pixelmesh-controller.log 2>&1 &

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
