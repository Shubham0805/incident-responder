#!/usr/bin/env bash
# Launches every local process for the demo: demo-app, the sre-tools MCP
# server, the backend orchestrator, the telemetry watcher, and the Streamlit
# dashboard. Does NOT start TrueForge itself -- run
# `npx @truefoundry/trueforge@latest` in its own terminal first, and run
# scripts/register.py once before this.
#
# If demo-app and/or the MCP server are already running elsewhere (e.g. via
# `docker compose up`, which publishes them on the same 8081/8082 ports),
# set SKIP_DEMO_APP=1 and/or SKIP_MCP_SERVER=1 so this script doesn't start
# a second copy that fails to bind. (Flagged by Qodo: the README's Docker
# section used to say to run "the rest natively as in step 3", but step 3
# is this script unconditionally starting native demo-app/mcp-server too --
# a guaranteed port clash with Compose's copies.)
set -euo pipefail
cd "$(dirname "$0")/.."

# Auto-activate the repo's venv if one exists and isn't already active --
# avoids the classic "opened a fresh terminal, forgot to activate" trip,
# which otherwise surfaces as a confusing "streamlit: command not found"
# deep inside this script instead of an obvious "activate your venv" hint.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f .venv/bin/activate ]; then
  echo "-> activating .venv (wasn't already active in this shell)"
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if [ -f .env ]; then
  set -a; source .env; set +a
fi

export DEMO_APP_URL="${DEMO_APP_URL:-http://localhost:8081}"
export MCP_SERVER_URL="${MCP_SERVER_URL:-http://localhost:8082/mcp}"
export BACKEND_URL="${BACKEND_URL:-http://localhost:8083}"
export TRUEFORGE_BASE_URL="${TRUEFORGE_BASE_URL:-http://localhost:8790}"

PIDS=()
cleanup() {
  echo ""
  echo "Shutting down..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

if [ "${SKIP_DEMO_APP:-0}" = "1" ]; then
  echo "-> demo-app: SKIP_DEMO_APP=1, assuming it's already up on ${DEMO_APP_URL}"
else
  echo "-> demo-app on ${DEMO_APP_URL}"
  python demo-app/app.py > /tmp/demo-app.log 2>&1 &
  PIDS+=($!)
  sleep 1
fi

if [ "${SKIP_MCP_SERVER:-0}" = "1" ]; then
  echo "-> sre-tools MCP server: SKIP_MCP_SERVER=1, assuming it's already up on ${MCP_SERVER_URL}"
else
  echo "-> sre-tools MCP server on ${MCP_SERVER_URL}"
  python mcp-server/sre_tools_server.py > /tmp/mcp-server.log 2>&1 &
  PIDS+=($!)
fi

echo "-> backend orchestrator on ${BACKEND_URL}"
python backend/main.py > /tmp/backend.log 2>&1 &
PIDS+=($!)

sleep 1

echo "-> telemetry watcher"
python telemetry/watcher.py > /tmp/watcher.log 2>&1 &
PIDS+=($!)

echo "-> Streamlit dashboard on http://localhost:${DASHBOARD_PORT:-8501}"
echo ""
echo "Logs: /tmp/demo-app.log /tmp/mcp-server.log /tmp/backend.log /tmp/watcher.log"
echo "Press Ctrl+C to stop everything."
echo ""
streamlit run dashboard/app.py --server.port "${DASHBOARD_PORT:-8501}"
