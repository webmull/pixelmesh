#!/bin/zsh
# PixelMesh V2 — interactive launcher

cd "$(dirname "$0")"

# ─────────────────────────────────────────────
#  Colours
# ─────────────────────────────────────────────
R=$'\e[0;31m'  G=$'\e[0;32m'  Y=$'\e[0;33m'
C=$'\e[0;36m'  W=$'\e[0;37m'  B=$'\e[1;34m'
DIM=$'\e[2m'   BOLD=$'\e[1m'  RESET=$'\e[0m'

THEME="dark"   # default
LAST_STARTED=""

# ─────────────────────────────────────────────
#  ASCII header
# ─────────────────────────────────────────────
header() {
  if [[ $THEME == "light" ]]; then
    clear
    local HC=$'\e[0;34m'   # blue on light bg
    local HD=$'\e[2m'
    echo "${HC}"
    echo "  ██████╗ ██╗██╗  ██╗███████╗██╗     ███╗   ███╗███████╗███████╗██╗  ██╗"
    echo "  ██╔══██╗██║╚██╗██╔╝██╔════╝██║     ████╗ ████║██╔════╝██╔════╝██║  ██║"
    echo "  ██████╔╝██║ ╚███╔╝ █████╗  ██║     ██╔████╔██║█████╗  ███████╗███████║"
    echo "  ██╔═══╝ ██║ ██╔██╗ ██╔══╝  ██║     ██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║"
    echo "  ██║     ██║██╔╝ ██╗███████╗███████╗██║ ╚═╝ ██║███████╗███████║██║  ██║"
    echo "  ╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝"
    echo "${HD}                                                               V2${RESET}"
    echo "${HD}                                                 by Adam Davis${RESET}"
    echo ""
  else
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
  fi
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

  local srv_s="${R}stopped${RESET}"
  local ctl_s="${R}stopped${RESET}"
  local ngk_s="${R}stopped${RESET}"

  [[ -n $srv ]] && srv_s="${G}running${RESET} ${DIM}(pid $srv)${RESET}"
  [[ -n $ctl ]] && ctl_s="${G}running${RESET} ${DIM}(pid $ctl)${RESET}"
  [[ -n $ngk ]] && ngk_s="${G}running${RESET} ${DIM}(pid $ngk)${RESET}"

  local clients=""
  if [[ -n $srv ]]; then
    local count
    count=$(curl -s --max-time 1 http://localhost:8000/admin/clients 2>/dev/null | grep -o '"clients":[0-9]*' | grep -o '[0-9]*')
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
  echo "  ${DIM}local  → http://localhost:8000${RESET}"
  echo "  ${DIM}public → https://local.pixelmesh.live${RESET}"
  echo "  ${DIM}sim    → https://local.pixelmesh.live/sim${RESET}"
  echo ""
}

# ─────────────────────────────────────────────
#  Actions
# ─────────────────────────────────────────────
kill_all() {
  echo "${Y}→ Stopping all processes...${RESET}"
  lsof -ti tcp:8000 | xargs kill -9 2>/dev/null || true
  pkill -f "uvicorn server:app" 2>/dev/null || true
  pkill -f "controller.py"      2>/dev/null || true
  pkill -f "ngrok"              2>/dev/null || true
  sleep 0.5
  echo "${G}  done.${RESET}"
  sleep 0.8
}

start_all() {
  echo "${Y}→ Starting server...${RESET}"
  python3 -m uvicorn server:app --reload --host 0.0.0.0 --port 8000 \
    >> /tmp/pixelmesh-server.log 2>&1 &
  sleep 1

  echo "${Y}→ Starting ngrok (eu → local.pixelmesh.live)...${RESET}"
  ngrok http 8000 \
    --region eu \
    --hostname local.pixelmesh.live \
    --log stdout \
    --log-format logfmt >> /tmp/pixelmesh-ngrok.log 2>&1 &
  sleep 2

  echo "${Y}→ Starting controller...${RESET}"
  python3 controller.py >> /tmp/pixelmesh-controller.log 2>&1 &
  sleep 1

  LAST_STARTED=$(date "+%d %b %Y  %H:%M:%S")
  echo "${G}  all started.${RESET}"
  sleep 1
}

# ─────────────────────────────────────────────
#  Menu loop
# ─────────────────────────────────────────────
while true; do
  header
  status_line

  local theme_label="light"
  [[ $THEME == "light" ]] && theme_label="dark"
  echo "  ${BOLD}${W}[s]${RESET} start      ${BOLD}${W}[r]${RESET} reload      ${BOLD}${W}[d]${RESET} die      ${BOLD}${W}[t]${RESET} ${theme_label} mode      ${BOLD}${W}[q]${RESET} quit"
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
    t|T)
      [[ $THEME == "dark" ]] && THEME="light" || THEME="dark"
      ;;
    q|Q)
      header
      echo "  ${DIM}bye.${RESET}"
      echo ""
      exit 0
      ;;
    *)
      ;;
  esac
done
