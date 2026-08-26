"""
sre-tools: a real MCP server (streamable-HTTP transport) that gives the
TrueForge agent everything it needs to triage and fix the incident in
demo-app, WITHOUT ever giving the model direct shell/file access to a real
production system.

Tools exposed:
  tail_log(lines)         read-only  -- tails demo-app's app.log (over HTTP,
                                         via demo-app's own /internal/log --
                                         this server never touches the
                                         filesystem, so it works whether
                                         demo-app is next door or in a
                                         separate container)
  check_db_health()       read-only  -- current DB pool config/status
                                         (this is what the agent's own
                                         sandbox diagnostic script calls, via
                                         TrueForge Code Mode, to inspect
                                         "database connection states")
  apply_system_change(dsn, reason)   destructive/write, GATED -- the rollback.
                                         TrueForge pauses the turn the instant
                                         this is called and will not execute
                                         it until a human approves.

Run standalone:
    python sre_tools_server.py
Then register its URL (http://localhost:<port>/mcp) with TrueForge -- see
scripts/register.py -- with:
    "require_approval_for_tools": ["apply_system_change"]
in the agent's mcp_servers entry (belt-and-braces on top of the
destructiveHint annotation below, which is TrueForge's default gate).
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

DEMO_APP_URL = os.environ.get("DEMO_APP_URL", "http://localhost:8081")
PORT = int(os.environ.get("MCP_SERVER_PORT", 8082))

mcp = FastMCP(
    "sre-tools",
    instructions=(
        "Tools for investigating and remediating the demo-app incident. "
        "tail_log and check_db_health are safe, read-only diagnostics -- use "
        "them freely. apply_system_change makes a real (sandboxed) change and "
        "requires human approval before it runs; only call it once you have "
        "identified the exact root cause and the exact fix."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path="/mcp",
)


@mcp.tool(
    description="Tail the last N lines of demo-app's application log (app.log). "
    "Use this first to see recent errors and any deployment markers.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, title="Tail app.log"),
)
def tail_log(lines: int = 100) -> str:
    with httpx.Client(timeout=5) as client:
        resp = client.get(f"{DEMO_APP_URL}/internal/log", params={"lines": lines})
        resp.raise_for_status()
        log_lines = resp.json().get("lines", [])
    return "\n".join(log_lines) if log_lines else "(log is empty)"


@mcp.tool(
    description="Check the current database connection pool configuration and "
    "health for demo-app: the DSN in use, the last-known-good DSN, and whether "
    "the service currently considers itself healthy.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, title="Check DB health"),
)
def check_db_health() -> dict:
    with httpx.Client(timeout=5) as client:
        resp = client.get(f"{DEMO_APP_URL}/internal/db-status")
        resp.raise_for_status()
        return resp.json()


@mcp.tool(
    description="Apply a database connection rollback to demo-app. THIS IS A "
    "DESTRUCTIVE, WRITE ACTION and will be gated for human approval by the "
    "harness -- the call will not actually run until a person approves it. "
    "Pass the exact DSN to roll back to (normally the last_known_good_dsn "
    "returned by check_db_health) and a one-line human-readable reason.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
        title="Apply rollback (requires approval)",
    ),
)
def apply_system_change(dsn: str, reason: str) -> dict:
    with httpx.Client(timeout=10) as client:
        resp = client.post(f"{DEMO_APP_URL}/internal/rollback", json={"dsn": dsn})
        resp.raise_for_status()
        result = resp.json()
    result["reason"] = reason
    return result


if __name__ == "__main__":
    print(f"[sre-tools] serving MCP over streamable-http on :{PORT}/mcp "
          f"(demo-app={DEMO_APP_URL})")
    mcp.run(transport="streamable-http")
