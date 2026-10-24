#!/usr/bin/env bash
# Start the LocPilot backend (127.0.0.1:8420) and the Vite dev UI (127.0.0.1:5420).
# A random per-session token protects the backend; the Vite proxy injects it server-side.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PORT="${LOCPILOT_PORT:-8420}"
UI_PORT=5420

port_in_use() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
for p in "$BACKEND_PORT" "$UI_PORT"; do
  if port_in_use "$p"; then
    echo "Port $p is already in use:"; lsof -nP -iTCP:"$p" -sTCP:LISTEN | tail -n +2
    echo "Stop that process or set LOCPILOT_PORT for the backend."; exit 1
  fi
done
[[ -d "$ROOT/backend/.venv" && -d "$ROOT/frontend/node_modules" ]] || { echo "Run scripts/setup_macos.sh first."; exit 1; }

TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export LOCPILOT_AUTH_TOKEN="$TOKEN" LOCPILOT_DEV_TOKEN="$TOKEN"
export LOCPILOT_BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"

pids=()
cleanup() { for pid in ${pids[@]+"${pids[@]}"}; do kill "$pid" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd "$ROOT/backend" && exec uv run locpilot-backend --port "$BACKEND_PORT") &
pids+=($!)
for _ in $(seq 1 60); do
  curl -fsS -m 1 "http://127.0.0.1:${BACKEND_PORT}/api/health" >/dev/null 2>&1 && break
  sleep 0.5
done
(cd "$ROOT/frontend" && exec npm run dev -- --port "$UI_PORT" --strictPort) &
pids+=($!)

echo ""
echo "  LocPilot backend: http://127.0.0.1:${BACKEND_PORT}  (API docs: /docs)"
echo "  LocPilot UI:      http://127.0.0.1:${UI_PORT}"
echo "  Press Ctrl+C to stop both."
# Portable to macOS's bash 3.2 (no `wait -n`): stop when either process exits.
while kill -0 "${pids[0]}" 2>/dev/null && kill -0 "${pids[1]}" 2>/dev/null; do sleep 1; done
