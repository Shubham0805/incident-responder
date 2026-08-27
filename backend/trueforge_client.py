"""
Thin client for TrueForge's HTTP + SSE API (https://trueforge.dev). We talk to
it directly over REST rather than the TypeScript-only SDK, since the backend
is Python.

Routes used (confirmed against TrueForge's published /openapi.json on
2026-08-26 -- if TrueForge has since changed a route or a field name, check
your running instance's own schema at ``$TRUEFORGE_BASE_URL/openapi.json``
first; the two spots flagged VERIFY below are the ones most likely to drift):

  POST /api/v1/sessions                                  create a session
  POST /api/v1/sessions/{session_id}/turns                create+execute a turn
                                                            (stream=true -> SSE)
  GET  /api/v1/sessions/{session_id}/turns/{turn_id}       get a turn
  GET  /api/v1/sessions/{session_id}/turns/{turn_id}/subscribe   reconnect to
                                                            a running turn's SSE
  POST /api/v1/mcp-servers                                 register an MCP server
  PUT  /api/v1/mcp-servers/{name}                           replace/update one

SSE event types (discriminated by "type"): model.message, tool.call is NOT a
separate event -- tool calls are embedded in model.message's `tool_calls`
field; execution results arrive as tool.response. The ones this project
cares about:
  turn.created, model.message, tool.approval_required, tool.response,
  tool.response_required, sandbox.created, turn.done, thread.created,
  thread.done, mcp.initialize, mcp.auth_required
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable, Iterator

import httpx


def _docker_bridge_gateway() -> str | None:
    """Read this container's default-route gateway straight from
    /proc/net/route (pure stdlib -- python:3.11-slim doesn't ship `ip` or
    `route`). Only meaningful inside a Linux container on Docker's bridge
    network; returns None when it's not available (e.g. running natively,
    not in Docker) so callers can fall back cleanly."""
    import socket
    import struct

    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (FileNotFoundError, OSError, ValueError):
        pass
    return None


def _resolve_base_url() -> str:
    """TRUEFORGE_BASE_URL may contain the literal placeholder host
    `__DOCKER_GATEWAY__` (set by docker-compose.yml for the `backend`
    service specifically) instead of a real hostname.

    Found live, on Docker Desktop for Mac, verifying an actual dry run --
    neither of the two "obvious" ways to reach the host from inside a
    container held up: `localhost` means "this container", not your
    machine, and `host.docker.internal` resolved to an unroutable IPv6
    address in this environment even after removing our own extra_hosts
    override (Docker Desktop's own DNS gave only an IPv6 AAAA record here).
    `npx trueforge` itself also only binds to loopback -- no --host flag
    exists to change that.

    The fix: the Docker bridge gateway IP always routes back to the host
    (confirmed live via /proc/net/route + a raw connection test), so we
    discover it at runtime and pair it with scripts/host_proxy.py, which
    listens on all interfaces on the host and forwards to TrueForge's
    loopback-only port.
    """
    raw = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/")
    if "__DOCKER_GATEWAY__" in raw:
        gw = _docker_bridge_gateway() or "host.docker.internal"  # best-effort fallback
        return raw.replace("__DOCKER_GATEWAY__", gw)
    return raw


BASE_URL = _resolve_base_url()
API_KEY = os.environ.get("TRUEFORGE_API_KEY", "").strip()


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        # VERIFY: TrueForge's local (npx) mode is unauthenticated by default;
        # this Bearer-token convention applies if you've switched on hosted
        # mode's login/auth. Adjust here if your instance uses a different
        # scheme (e.g. an X-API-Key header).
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


@dataclass
class SSEEvent:
    event: str | None
    data: dict


def _iter_sse(response: httpx.Response) -> Iterator[SSEEvent]:
    """Minimal Server-Sent-Events parser: yields one SSEEvent per `data:` block."""
    event_name = None
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
        line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", "replace")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = {"raw": payload}
                yield SSEEvent(event=event_name, data=parsed)
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):
            continue  # comment/keepalive
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())


def create_session(agent_name: str) -> dict:
    resp = httpx.post(
        f"{BASE_URL}/api/v1/sessions",
        headers=_headers(),
        json={"agent": {"name": agent_name}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def stream_turn(session_id: str, input_items: list[dict], on_event: Callable[[dict], None]) -> None:
    """POST a new turn and call on_event(event_dict) for every SSE event until
    turn.done (or the connection ends). Blocking -- run this in a thread."""
    url = f"{BASE_URL}/api/v1/sessions/{session_id}/turns"
    with httpx.Client(timeout=httpx.Timeout(10.0, read=None)) as client:
        with client.stream(
            "POST", url, headers=_headers(),
            json={"input": input_items, "stream": True},
        ) as response:
            response.raise_for_status()
            for evt in _iter_sse(response):
                on_event(evt.data)
                if evt.data.get("type") == "turn.done":
                    return


def user_message(text: str) -> dict:
    return {"type": "user.message", "content": text}


def tool_approval(thread_id: str, tool_call_id: str, allow: bool, reason: str | None = None) -> dict:
    """Build a user.tool_approval input item to resume a paused turn.

    VERIFY: TrueForge's own TypeScript SDK examples use camelCase
    (threadId/toolCallId) because that's idiomatic JS; the raw JSON wire
    format documented for this project follows the rest of the API's
    snake_case convention. We send BOTH spellings for each key so this works
    whichever the real wire format turns out to be -- harmless extra fields
    are ignored by JSON APIs. If your instance rejects it, check
    $TRUEFORGE_BASE_URL/openapi.json for the TurnInputItem/ApprovalDecision
    schema and trim this to match.
    """
    approval = {"status": "allow" if allow else "deny"}
    if reason:
        approval["reason"] = reason
    return {
        "type": "user.tool_approval",
        "thread_id": thread_id,
        "threadId": thread_id,
        "tool_call_id": tool_call_id,
        "toolCallId": tool_call_id,
        "approval": approval,
    }
