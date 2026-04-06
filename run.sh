#!/bin/zsh
# PixelMesh V2 — kill old sessions and boot fresh

cd "$(dirname "$0")"

echo "→ Killing old sessions..."

# Kill anything on port 8000
lsof -ti tcp:8000 | xargs kill -9 2>/dev/null && echo "  killed port 8000" || true

# Kill any running uvicorn or controller processes for this project
pkill -f "uvicorn server:app" 2>/dev/null && echo "  killed uvicorn" || true
pkill -f "controller.py"      2>/dev/null && echo "  killed controller" || true

sleep 0.5

echo "→ Starting server..."
python3 -m uvicorn server:app --reload --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

sleep 1

echo "→ Starting controller..."
python3 controller.py &
CONTROLLER_PID=$!

echo ""
echo "  server PID:     $SERVER_PID"
echo "  controller PID: $CONTROLLER_PID"
echo ""
echo "  client → http://$(ipconfig getifaddr en0):8000/app"
echo "  sim    → http://localhost:8000/public/sim.html"
echo ""
echo "Press Ctrl+C to stop everything."

# On Ctrl+C, kill both
trap "echo '→ Shutting down...'; kill $SERVER_PID $CONTROLLER_PID 2>/dev/null; exit 0" INT TERM

wait
