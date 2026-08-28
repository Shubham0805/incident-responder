#!/usr/bin/env python3
"""
One-time setup: register the sre-tools MCP server, the incident-responder
agent, and the sre-playbook skill against a locally running TrueForge
instance.

Run this AFTER `npx @truefoundry/trueforge@latest` is up and you've added an
LLM provider key in its Settings -> Models UI, and AFTER `docker compose up`
(or the native equivalent) has the sre-tools MCP server reachable.

Every request shape below (paths, field names, the MCP server `type` enum,
the agent model FQN format, the skill manifest fields) was verified against
a live TrueForge instance's own /api/v1/openapi.json -- not guessed from
docs. Two things TrueForge's docs don't spell out and that verification
caught:
  - MCPServerManifest.type only accepts "remote" (not "streamable-http",
    despite the transport itself being streamable-http under the hood), and
    `auth` must be omitted entirely for an unauthenticated server -- there's
    no `{"type": "none"}` variant.
  - The agent spec's `model.name` must be the FULL "provider/model" string
    exactly as returned by GET /api/v1/models (e.g. "openai/gpt-5.2" or
    "google-gemini/gemini-3-6-flash") -- NOT the bare model name shown in
    the Settings UI, and NOT the separate `model_id` field on that same
    response. Sending the wrong one 422s with "provider not configured"
    rather than a clear "bad format" error, so it's easy to misdiagnose.
    Run `curl -s http://localhost:8790/api/v1/models | python3 -m json.tool`
    to see the exact strings for whatever you've configured, and set
    TRUEFORGE_MODEL to the `name` field (not `model_id`) from there.

If any POST below fails, this script prints the exact JSON payload it tried
so you can paste it into TrueForge's UI by hand instead -- that always works.
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
SKILL_REF = os.environ.get("TRUEFORGE_SKILL_REF", "main")

# Must be the full "provider/model" FQN from GET /api/v1/models's `name`
# field for whatever you configured in Settings -> Models -- see the module
# docstring above. There's no universal default that works for every judge's
# setup, so this intentionally has no fallback -- the script fails loudly
# instead of silently registering an agent that can never run a turn.
MODEL_NAME = os.environ.get("TRUEFORGE_MODEL")

# Only needed for the skill registration below -- the skill registry is
# git-backed, so it needs a real pushed repo, not a local-only folder.
GITHUB_REPO = os.environ.get("GITHUB_REPO_URL", "https://github.com/<you>/<repo>")

# Only needed when TrueForge is running in hosted/Docker mode with auth
# enabled -- the default local `npx` instance doesn't require this. (Flagged
# by Qodo: .env.example documents TRUEFORGE_API_KEY but this script used to
# never read it or attach it to requests, so registration would 401 against
# an authenticated instance.)
API_KEY = os.environ.get("TRUEFORGE_API_KEY", "").strip()
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


def _post(path: str, body: dict) -> tuple[int, dict]:
    try:
        resp = httpx.post(f"{BASE_URL}{path}", json=body, headers=AUTH_HEADERS, timeout=15)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data
    except httpx.HTTPError as exc:
        return -1, {"error": str(exc)}


def register_mcp_server() -> bool:
    """Returns True if the MCP server exists on TrueForge by the time this
    returns (freshly created OR already there from a prior run), False if
    it genuinely doesn't. The agent spec references this server by name, so
    a caller needs to know whether it's safe to proceed."""
    manifest = {
        "type": "remote",
        "name": MCP_SERVER_NAME,
        "url": MCP_SERVER_URL,
        "description": "SRE triage/remediation tools for demo-app (tail_log, "
                        "check_db_health, apply_system_change).",
        # No `auth` key: omit entirely for an unauthenticated server. There
        # is no `{"type": "none"}` variant in TrueForge's MCPServerManifest.
    }
    print(f"-> registering MCP server '{MCP_SERVER_NAME}' at {MCP_SERVER_URL}")
    status, data = _post("/api/v1/settings/mcp-servers", {"manifest": manifest})
    if status and 200 <= status < 300:
        print(f"   OK ({status})")
        return True
    elif status == 409 or "already exists" in str(data.get("error", "")).lower():
        print(f"   already registered -- fine, reusing it. Update it via "
              f"PUT /api/v1/settings/mcp-servers or the UI if you changed the URL/tools.")
        return True
    else:
        print(f"   FAILED ({status}): {data}")
        print("   Fall back to the UI: Settings -> Connectors -> Add server, using:")
        print("   " + json.dumps(manifest, indent=2).replace("\n", "\n   "))
        return False


def register_agent(mcp_server_ready: bool, skill_ready: bool) -> bool:
    """The agent manifest references both the MCP server and the skill by
    name -- TrueForge 422s ("Unknown skill/server ... not configured") if
    either doesn't actually exist yet. Registering the agent anyway in that
    case is a guaranteed cascading failure dressed up as success. (Flagged
    by Qodo: this used to run unconditionally and still print "Done" even
    when the skill step had failed or been skipped.)"""
    if not mcp_server_ready or not skill_ready:
        missing = []
        if not mcp_server_ready:
            missing.append(f"MCP server '{MCP_SERVER_NAME}'")
        if not skill_ready:
            missing.append(f"skill '{SKILL_NAME}'")
        print(f"-> SKIPPING agent registration: {', '.join(missing)} not confirmed "
              f"registered above. The agent spec references both by name and "
              f"TrueForge will reject it (422 'Unknown ... not configured') "
              f"otherwise. Fix whatever failed above and re-run this script.")
        return False

    if not MODEL_NAME:
        print("-> SKIPPING agent registration: TRUEFORGE_MODEL is not set.")
        print("   Run this first to see the exact string TrueForge expects for your")
        print("   configured provider (use the `name` field, not `model_id`):")
        print(f"     curl -s {BASE_URL}/api/v1/models | python3 -m json.tool")
        print("   Then set TRUEFORGE_MODEL in .env (e.g. google-gemini/gemini-3-6-flash)")
        print("   and re-run this script.")
        return False

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
        return True
    elif status == 409 or (data.get("error") and "exists" in str(data.get("error")).lower()):
        print(f"   agent already exists -- update it via PUT /api/v1/agents/{AGENT_NAME} "
              f"or the UI if you changed the spec.")
        return True
    else:
        print(f"   FAILED ({status}): {data}")
        print("   Fall back to the UI (Agent Library -> New agent), using this spec:")
        print("   " + json.dumps(spec, indent=2).replace("\n", "\n   "))
        return False


def register_skill() -> bool:
    """Returns True if the skill exists on TrueForge by the time this
    returns, False otherwise -- see register_mcp_server()'s docstring for
    why the caller needs this."""
    if "<you>/<repo>" in GITHUB_REPO:
        print("-> SKIPPING skill registration: GITHUB_REPO_URL is not set (still the")
        print("   placeholder). The skill registry is git-backed, so it needs a real")
        print("   pushed repo. Set GITHUB_REPO_URL in .env, e.g.:")
        print("     GITHUB_REPO_URL=https://github.com/<you>/incident-responder")
        print("   and re-run this script -- or add it by hand in Settings -> Skills:")
        print(f"     name: {SKILL_NAME}  path: skills/{SKILL_NAME}  ref: {SKILL_REF}")
        return False

    manifest = {
        "type": "git",
        "name": SKILL_NAME,
        "url": GITHUB_REPO,
        "path": f"skills/{SKILL_NAME}",
        "ref": SKILL_REF,
        "description": "SRE operational playbook: triage -> sandbox diagnostic -> "
                        "gated rollback -> verify -> incident report.",
    }
    print(f"-> registering skill '{SKILL_NAME}' from {GITHUB_REPO} :: skills/{SKILL_NAME} @ {SKILL_REF}")
    status, data = _post("/api/v1/settings/skills", {"manifest": manifest})
    if status and 200 <= status < 300:
        print(f"   OK ({status})")
        return True
    elif status == 409 or "already exists" in str(data.get("error", "")).lower():
        print(f"   already registered -- fine, reusing it. Update it via the UI "
              f"(Settings -> Skills) if you changed the path/ref.")
        return True
    else:
        print(f"   FAILED ({status}): {data}")
        print("   Fall back to the UI: Settings -> Skills -> Add skill, using:")
        print("   " + json.dumps(manifest, indent=2).replace("\n", "\n   "))
        return False


if __name__ == "__main__":
    print(f"TrueForge at {BASE_URL} -- make sure it's already running.\n")
    mcp_ok = register_mcp_server()
    print()
    # Skill must be registered BEFORE the agent -- the agent spec references
    # it by name (`skills: [{"name": SKILL_NAME}]`), and TrueForge 422s with
    # "Unknown skill ... not configured" if it doesn't exist yet. (Caught by
    # a live run: the original ordering here registered the agent first.)
    skill_ok = register_skill()
    print()
    agent_ok = register_agent(mcp_server_ready=mcp_ok, skill_ready=skill_ok)

    print()
    if mcp_ok and skill_ok and agent_ok:
        print("Done -- all three registered. Then: ./scripts/run_all.sh "
              "(or docker compose up, if not already running)")
    else:
        failed = [name for name, ok in
                  [("MCP server", mcp_ok), ("skill", skill_ok), ("agent", agent_ok)]
                  if not ok]
        print(f"NOT fully done -- {', '.join(failed)} registration did not succeed. "
              f"Fix the issue(s) above and re-run this script before starting the app.")
        sys.exit(1)
