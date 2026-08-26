#!/usr/bin/env python3
"""
One-time setup: register the sre-tools MCP server and the incident-responder
agent against a locally running TrueForge instance.

Run this AFTER `npx @truefoundry/trueforge@latest` and the sre-tools MCP
server (`python mcp-server/sre_tools_server.py`) are both up.

Two pieces of this need a live instance to fully confirm (TrueForge's public
docs didn't give a verbatim schema for every field -- see comments below):
  - MCPServerManifest.type's exact enum value
  - whether MCP servers / skills can be registered by name immediately after
    creation, or need a moment / a UI confirmation step

If either POST below fails, this script prints the exact JSON payload it
tried so you can paste it into TrueForge's UI (Settings -> Connectors /
Settings -> Skills) by hand instead -- that always works and only takes a
minute.
"""

import json
import os
import sys
from pathlib import Path

import httpx


def _load_dotenv(path: Path = Path(__file__).resolve().parent.parent / ".env") -> None:
    """Load KEY=VALUE lines from .env into os.environ, without adding a
    python-dotenv dependency. Real exported env vars always win -- this only
    fills in values that aren't already set, so `.env` acts as a default,
    not an override. (Flagged by Qodo: this script used to silently ignore
    .env entirely and only pick up already-exported shell vars.)"""
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

BASE_URL = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8082/mcp")
AGENT_NAME = os.environ.get("TRUEFORGE_AGENT_NAME", "incident-responder")
MCP_SERVER_NAME = os.environ.get("TRUEFORGE_MCP_SERVER_NAME", "sre-tools")
SKILL_NAME = os.environ.get("TRUEFORGE_SKILL_NAME", "sre-playbook")
MODEL_NAME = os.environ.get("TRUEFORGE_MODEL", "openai/gpt-4o")

GITHUB_REPO = os.environ.get("GITHUB_REPO_URL", "https://github.com/<you>/<repo>")


def _post(path: str, body: dict) -> tuple[int, dict]:
    try:
        resp = httpx.post(f"{BASE_URL}{path}", json=body, timeout=15)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data
    except httpx.HTTPError as exc:
        return -1, {"error": str(exc)}


def register_mcp_server():
    manifest = {
        "type": "streamable-http",  # VERIFY against your instance if this 400s
        "name": MCP_SERVER_NAME,
        "url": MCP_SERVER_URL,
        "description": "SRE triage/remediation tools for demo-app (tail_log, "
                        "check_db_health, apply_system_change).",
        "auth": {"type": "none"},
    }
    print(f"-> registering MCP server '{MCP_SERVER_NAME}' at {MCP_SERVER_URL}")
    status, data = _post("/api/v1/mcp-servers", {"manifest": manifest})
    if status and 200 <= status < 300:
        print(f"   OK ({status})")
    else:
        print(f"   FAILED ({status}): {data}")
        print("   Fall back to the UI: Settings -> Connectors -> Add server, using:")
        print("   " + json.dumps(manifest, indent=2).replace("\n", "\n   "))


def register_agent():
    spec = {
        "model": {"name": MODEL_NAME},
        "instructions": (
            "You are the on-call SRE agent for demo-app. When told an alert "
            "fired, use the sre-playbook skill and the sre-tools MCP server "
            "to triage, diagnose, propose, and (once approved) apply a fix, "
            "then verify recovery."
        ),
        "mcp_servers": [
            {
                "name": MCP_SERVER_NAME,
                "enable_tools": ["@all"],
                "require_approval_for_tools": ["apply_system_change"],
                "preload": True,
            }
        ],
        "skills": [{"name": SKILL_NAME}],
        "config": {
            "sandbox": {"enabled": True},
            "iteration_limit": 40,
        },
    }
    print(f"-> registering agent '{AGENT_NAME}' (model={MODEL_NAME})")
    status, data = _post("/api/v1/agents", {"name": AGENT_NAME, "manifest": spec})
    if status and 200 <= status < 300:
        print(f"   OK ({status})")
    elif status == 409 or (data.get("error") and "exists" in str(data.get("error")).lower()):
        print(f"   agent already exists -- update it via PUT /api/v1/agents/{AGENT_NAME} "
              f"or the UI if you changed the spec.")
    else:
        print(f"   FAILED ({status}): {data}")
        print("   Fall back to the UI (Agent Library -> New agent), using this spec:")
        print("   " + json.dumps(spec, indent=2).replace("\n", "\n   "))


def print_skill_instructions():
    print(f"""
-> register the skill (no public REST endpoint documented for this -- do it
   once via the UI):
   Settings -> Skills -> Add skill
     name: {SKILL_NAME}
     repo: {GITHUB_REPO}
     path: skills/{SKILL_NAME}
     ref:  main   (or a tagged commit once you've pushed)
   (Push this repo to GitHub first if you haven't -- the skill registry is
   git-backed, it can't read a local-only folder.)
""")


if __name__ == "__main__":
    print(f"TrueForge at {BASE_URL} -- make sure it's already running.\n")
    register_mcp_server()
    print()
    register_agent()
    print_skill_instructions()
    print("Done. Then: ./scripts/run_all.sh")
