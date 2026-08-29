#!/bin/zsh
# (c) Adam Davis - adamdavis.co.uk
# ─────────────────────────────────────────────────────────────
#  pixelmesh audience sim
#
#  Spawns N fake phones as separate Chrome instances pointed at
#  pixelmesh.show. Each gets its own --user-data-dir, so each has
#  its own localStorage and therefore its own device_id: the
#  server sees N genuinely distinct clients, not N tabs.
#
#  Windows are tiled across every display (menu bar and Dock
#  excluded) at phone aspect, never overlapping.
#
#    ./sim.sh              # 2 phones against pixelmesh.show
#    ./sim.sh 12           # 12 phones
#    ./sim.sh 6 --local    # against http://127.0.0.1:8000
#    ./sim.sh 6 --url http://192.168.1.20:8000
#    ./sim.sh --kill       # stop every sim phone
#    ./sim.sh 40 --fill    # tile edge to edge (load/UI work, not detection)
#    ./sim.sh 20 --jitter  # phones drift a few px, like hands that aren't still
#    ./sim.sh 20 --jitter 14   # ...with a wider wobble
#
#  Ctrl-C tears the whole crowd down. Profiles persist between
#  runs so each sim phone keeps its device_id (and its blink id);
#  --fresh wipes them for a brand new crowd.
# ─────────────────────────────────────────────────────────────

cd "$(dirname "$0")"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILES="$PWD/.sim-profiles"
URL="https://pixelmesh.show"
COUNT=2
GAP=10
FRESH=0
DETACH=0
KILL=0
FILL=0
JITTER=0
JITTER_PX=8
CDP_PORT_BASE=9400

C=$'\e[0;36m'; G=$'\e[0;32m'; Y=$'\e[0;33m'; R=$'\e[0;31m'
DIM=$'\e[2m'; BOLD=$'\e[1m'; RESET=$'\e[0m'

# ── args ────────────────────────────────────────────────────
while (( $# )); do
  case "$1" in
    ''|*[!0-9]*)
      case "$1" in
        --url)     URL="$2"; shift 2 ;;
        --local)   URL="http://127.0.0.1:8000"; shift ;;
        --gap)     GAP="$2"; shift 2 ;;
        --fresh)   FRESH=1; shift ;;
        --detach)  DETACH=1; shift ;;
        --kill)    KILL=1; shift ;;
        --fill)    FILL=1; shift ;;
        --jitter)
          JITTER=1; shift
          # Optional pixel amount: ./sim.sh 20 --jitter   or   --jitter 14
          if [[ -n "$1" && "$1" != -* && "$1" == ${~:-<->} ]]; then
            JITTER_PX="$1"; shift
          fi ;;
        --)        shift ;;
        -h|--help)
          sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
          exit 0 ;;
        *)
          echo "${R}Unknown option: $1${RESET}" >&2
          echo "Usage: ./sim.sh [N] [--url URL | --local] [--gap PX] [--fill] [--jitter [PX]] [--fresh] [--detach] [--kill]" >&2
          exit 1 ;;
      esac ;;
    *) COUNT="$1"; shift ;;
  esac
done

# ── kill any running crowd ──────────────────────────────────
stop_all() {
  # -i because macOS paths are case-insensitive but pkill matching is not:
  # a crowd started from ~/desktop/... is invisible to a pattern built from
  # ~/Desktop/..., and the windows outlive every attempt to kill them.
  pkill -if "sim_cdp.py $PROFILES/" 2>/dev/null
  pkill -if "user-data-dir=$PROFILES/" 2>/dev/null
  # pkill's default signal is TERM, which Chrome treats as "shut down
  # nicely" - and a wedged instance (GPU hang, beachball) ignores it
  # forever, which is how phones survived a kill that claimed success.
  # Give the polite signal two seconds to work, then force the rest.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -if "user-data-dir=$PROFILES/" >/dev/null 2>&1 || break
    /bin/sleep 0.2
  done
  if pgrep -if "user-data-dir=$PROFILES/" >/dev/null 2>&1; then
    pkill -9 -if "user-data-dir=$PROFILES/" 2>/dev/null
    pkill -9 -if "sim_cdp.py $PROFILES/"    2>/dev/null
    /bin/sleep 0.3
  fi
}

if (( KILL )); then
  if pgrep -if "user-data-dir=$PROFILES/" >/dev/null 2>&1; then
    stop_all
    # Say what actually happened, not what was attempted. The old message
    # printed "stopped" on the strength of having found something to signal.
    if pgrep -if "user-data-dir=$PROFILES/" >/dev/null 2>&1; then
      echo "${R}Some sim phones survived even SIGKILL:${RESET}"
      pgrep -ifl "user-data-dir=$PROFILES/"
      echo "${DIM}(a process unkillable by -9 is usually stuck in the kernel; check Activity Monitor)${RESET}"
      exit 1
    fi
    echo "${G}Sim phones stopped.${RESET}"
  else
    echo "${DIM}No sim phones running.${RESET}"
  fi
  exit 0
fi

if [[ ! -x "$CHROME" ]]; then
  echo "${R}Google Chrome not found at:${RESET} $CHROME" >&2
  exit 1
fi

if (( COUNT < 1 )); then
  echo "${R}Need at least 1 phone.${RESET}" >&2
  exit 1
fi

# A previous crowd would fight this one for screen space.
if pgrep -if "user-data-dir=$PROFILES/" >/dev/null 2>&1; then
  echo "${Y}Sim phones already running - replacing them.${RESET}"
  stop_all
  # Chrome needs a moment to release its profile locks.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -if "user-data-dir=$PROFILES/" >/dev/null 2>&1 || break
    /bin/sleep 0.2
  done
fi

if (( FRESH )); then
  rm -rf "$PROFILES"
fi
mkdir -p "$PROFILES"

# ── work out where each window goes ─────────────────────────
LAYOUT=$(python3 - "$COUNT" "$GAP" "$FILL" <<'PY'
"""Tile N phone-shaped windows across every display, no overlap.

Prints one "x y w h slack_x slack_y" line per window in Chrome's screen
coordinates.  The slack is the empty space each window has inside its own
layout cell; --jitter uses it as a movement bound so a wobbling phone can
never wander into its neighbour.  Coordinates are given
(origin at the top-left of the primary display, y growing downward).
NSScreen's visibleFrame already excludes the menu bar and the Dock;
it uses a bottom-left origin, so y is flipped on the way out.
"""
import math
import sys

from AppKit import NSScreen

want, gap, fill = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])

PHONE_AR = 0.50    # w/h - roughly a modern handset
MAX_W    = 560     # stay under the app's 620px desktop breakpoint
CHROME_MIN_W = 86  # measured: Chrome refuses to make a window narrower

# The detector's noise gate is 3.5x the 90th percentile of grid-point
# variance, capped at 0.15.  That percentile is meant to measure the dark
# room behind the phones.  Tile windows edge to edge and the phones become
# most of the frame, so the gate starts measuring phones instead, climbs to
# its ceiling and locks out every phone that does not clear it - the crowd
# raises the bar against itself.  A real audience is small bright rectangles
# in a lot of darkness, so phone coverage is capped here to match.
MAX_COVERAGE = 0.08
MIN_LIT_W    = 24  # px: below this the lit patch stops being worth detecting

screens = list(NSScreen.screens())
primary = next((s for s in screens
                if s.frame().origin.x == 0 and s.frame().origin.y == 0), screens[0])
flip = primary.frame().size.height

displays = []
for s in screens:
    v = s.visibleFrame()
    displays.append({
        "x": int(round(v.origin.x)),
        "y": int(round(flip - (v.origin.y + v.size.height))),
        "w": int(round(v.size.width)),
        "h": int(round(v.size.height)),
    })
displays.sort(key=lambda d: d["x"])

total_area = sum(d["w"] * d["h"] for d in displays) or 1
# w * (w / PHONE_AR) is one phone's area, so w = sqrt(share * PHONE_AR).
cov_w = int(math.sqrt(MAX_COVERAGE * total_area / want * PHONE_AR))

# Chrome will not make a window narrower than CHROME_MIN_W, so past a certain
# crowd size the windows cannot shrink far enough to stay inside the coverage
# budget.  The lit area does not have that limit: #card-blink is its own
# fixed-position element, so the window can sit at Chrome's floor while the
# blinking patch inside it shrinks freely, with black around it.  That is also
# closer to the real thing - a phone in a dark auditorium is a small bright
# rectangle, not a wall-to-wall glow.
win_w = max(cov_w, CHROME_MIN_W)
lit_frac = min(1.0, cov_w / win_w) if win_w else 1.0

# Share the crowd out by screen area, largest remainder first.
areas = [d["w"] * d["h"] for d in displays]
total = sum(areas) or 1
exact = [want * a / total for a in areas]
counts = [int(e) for e in exact]
leftover = want - sum(counts)
for i in sorted(range(len(displays)), key=lambda i: exact[i] - counts[i], reverse=True):
    if leftover <= 0:
        break
    counts[i] += 1
    leftover -= 1


def best_grid(d, n):
    """Pick the rows x cols that gives the largest phone-shaped window."""
    best = None
    for cols in range(1, n + 1):
        rows = math.ceil(n / cols)
        cw = (d["w"] - gap * (cols + 1)) / cols
        ch = (d["h"] - gap * (rows + 1)) / rows
        if cw <= 0 or ch <= 0:
            continue
        ww = min(cw, MAX_W) if fill else min(cw, MAX_W, win_w)
        wh = min(ch, ww / PHONE_AR)
        ww = min(ww, wh * PHONE_AR)
        score = ww * wh
        if best is None or score > best[0]:
            best = (score, cols, rows, cw, ch, int(ww), int(wh))
    return best


smallest = None
out = []
for d, n in zip(displays, counts):
    if n <= 0:
        continue
    _, cols, rows, cw, ch, ww, wh = best_grid(d, n)
    smallest = ww if smallest is None else min(smallest, ww)  # noqa: E501
    for i in range(n):
        r, c = divmod(i, cols)
        cx = d["x"] + gap + c * (cw + gap)
        cy = d["y"] + gap + r * (ch + gap)
        # Percentage inset applied to #card-blink so the lit area lands on the
        # coverage budget regardless of how big Chrome insisted the window be.
        inset = 0 if fill else int(round((1.0 - lit_frac) / 2 * 100))
        out.append((int(cx + (cw - ww) / 2), int(cy + (ch - wh) / 2), ww, wh,
                    int((cw - ww) / 2), int((ch - wh) / 2), inset))

if fill:
    print("fill mode: phones tile edge to edge, which saturates the detector's "
          "noise gate - fine for load and UI work, not for detection runs",
          file=sys.stderr)
elif cov_w < MIN_LIT_W:
    print(f"too crowded even with shrinking: {want} phones would need a {cov_w}px "
          f"lit patch, below the {MIN_LIT_W}px the camera can usefully resolve. "
          f"Max for a clean detection run here is "
          f"{int(MAX_COVERAGE * total_area * PHONE_AR / (MIN_LIT_W ** 2))}.",
          file=sys.stderr)
elif lit_frac < 1.0:
    print(f"note: {want} phones need {cov_w}px of lit area but Chrome will not "
          f"make a window under {CHROME_MIN_W}px, so the blink patch is inset to "
          f"{int(lit_frac * 100)}% inside a black window to stay within the "
          f"coverage budget", file=sys.stderr)

for x, y, w, h, sx, sy, inset in out:
    print(x, y, w, h, sx, sy, inset)
PY
)

if [[ -z "$LAYOUT" ]]; then
  echo "${R}Could not work out a window layout.${RESET}" >&2
  exit 1
fi

# ── launch the crowd ────────────────────────────────────────
echo ""
echo "${C}${BOLD}pixelmesh sim${RESET} ${DIM}- $COUNT phone(s) -> $URL${RESET}"
echo ""

JSTATE="$PROFILES/jitter.state"
: > "$JSTATE"

i=0
while IFS=' ' read -r X Y W H SX SY INSET; do
  [[ -z "$X" ]] && continue
  i=$(( i + 1 ))
  DIR="$PROFILES/phone-$i"
  mkdir -p "$DIR"

  # Chrome remembers where an app window last sat and would ignore
  # --window-position on a reused profile. Forget the saved bounds.
  PREFS="$DIR/Default/Preferences"
  if [[ -f "$PREFS" ]]; then
    python3 - "$PREFS" <<'PY' 2>/dev/null
import json
import sys

path = sys.argv[1]
try:
    with open(path) as fh:
        prefs = json.load(fh)
except Exception:
    sys.exit(0)
browser = prefs.get("browser", {})
browser.pop("app_window_placement", None)
browser.pop("window_placement", None)
with open(path, "w") as fh:
    json.dump(prefs, fh)
PY
  fi

  # The DevTools port carries both the lit-area shrink and --jitter, so it is
  # opened whenever either is in play and left shut otherwise.
  CDP=()
  if (( JITTER )) || (( INSET > 0 )); then
    PORT=$(( CDP_PORT_BASE + i ))
    CDP=(--remote-debugging-port="$PORT")
    echo "$PORT $X $Y $W $H $SX $SY $INSET" >> "$JSTATE"
  fi

  "$CHROME" \
    --user-data-dir="$DIR" \
    --app="$URL" \
    --window-position="$X,$Y" \
    --window-size="$W,$H" \
    "${CDP[@]}" \
    --no-first-run \
    --no-default-browser-check \
    --disable-session-crashed-bubble \
    --disable-infobars \
    --autoplay-policy=no-user-gesture-required \
    --disable-features=Translate,MediaRouter,InfiniteSessionRestore \
    --disable-backgrounding-occluded-windows \
    --disable-renderer-backgrounding \
    --disable-background-timer-throttling \
    >/dev/null 2>&1 &

  printf "  ${G}phone %-3s${RESET} ${DIM}%4sx%-4s at %5s,%-5s${RESET}\n" "$i" "$W" "$H" "$X" "$Y"
done <<< "$LAYOUT"

if [[ -s "$JSTATE" ]]; then
  # Chrome needs to be listening before the driver can attach.
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    curl -s --max-time 1 "http://127.0.0.1:$(( CDP_PORT_BASE + 1 ))/json/version" \
      >/dev/null 2>&1 && break
    /bin/sleep 0.5
  done
  python3 "$PWD/tools/sim_cdp.py" "$JSTATE" "$(( JITTER ? JITTER_PX : 0 ))" \
    >/dev/null 2>&1 &
  (( JITTER )) && echo "  ${DIM}jitter on: +/-${JITTER_PX}px drift per phone${RESET}"
  (( INSET > 0 )) && echo "  ${DIM}blink patch inset ${INSET}% to hold the coverage budget${RESET}"
fi

echo ""

if (( DETACH )); then
  echo "${DIM}Running detached. Stop them with: ./sim.sh --kill${RESET}"
  echo ""
  exit 0
fi

cleanup() {
  echo ""
  echo "${Y}Stopping sim phones...${RESET}"
  stop_all
  echo "${G}Done.${RESET}"
  exit 0
}
trap cleanup INT TERM

echo "${DIM}Ctrl-C to stop the crowd.${RESET}"
echo ""
wait
