#!/usr/bin/env bash
# Launches every local process for the demo: demo-app, the sre-tools MCP
# server, the backend orchestrator, the telemetry watcher, and the Streamlit
# dashboard. Does NOT start TrueForge itself -- run
# `npx @truefoundry/trueforge@latest` in its own terminal first, and run
# scripts/register.py once before this.
set -euo pipefail
cd "$(dirname "$0")/.."

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

echo "-> demo-app on ${DEMO_APP_URL}"
python demo-app/app.py > /tmp/demo-app.log 2>&1 &
PIDS+=($!)

sleep 1

echo "-> sre-tools MCP server on ${MCP_SERVER_URL}"
python mcp-server/sre_tools_server.py > /tmp/mcp-server.log 2>&1 &
PIDS+=($!)

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
