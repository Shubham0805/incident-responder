"""
backend/main.py -- the orchestrator that wires everything together:

  telemetry webhook -> real TrueForge session/turn -> SSE consumption ->
  in-memory state -> polled by the Streamlit dashboard -> human approval ->
  resume the paused turn.

Deliberately kept to plain FastAPI + a background thread per active turn
(no queue/DB) -- this is a hackathon demo of the harness, not a production
incident-management system.
"""

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import trueforge_client as tf

AGENT_NAME = os.environ.get("TRUEFORGE_AGENT_NAME", "incident-responder")

app = FastAPI(title="incident-responder backend")

_lock = threading.Lock()

STATE: dict[str, Any] = {
    "status": "idle",  # idle | investigating | intervention_required | remediating | remediation_denied | resolved
    "alert": None,
    "session_id": None,
    "turn_id": None,
    "pending_approval": None,
    "final_report": None,
    "events": [],       # transparent, ordered log of every raw SSE event -- shown verbatim to judges
    "error": None,
}
_event_index: dict[str, dict] = {}  # event id -> event, for resolving tool_calls by source_event_id
_seq = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_event(evt: dict) -> None:
    """Called from the SSE-consuming thread for every event -- append to the
    transparent log, index it, and react to the ones that matter."""
    global _seq
    with _lock:
        _seq += 1
        entry = {"seq": _seq, "at": _now(), "type": evt.get("type"), "data": evt}
        STATE["events"].append(entry)

        evt_id = evt.get("id") or evt.get("event_id")
        if evt_id:
            _event_index[evt_id] = evt

        etype = evt.get("type")

        if etype == "turn.created":
            STATE["turn_id"] = evt.get("id") or evt.get("turn_id")

        elif etype == "tool.approval_required":
            call_ref = (evt.get("tool_calls") or [{}])[0]
            tool_name, tool_args = _resolve_tool_call(call_ref)
            STATE["pending_approval"] = {
                "thread_id": evt.get("thread_id") or evt.get("threadId") or "main",
                "tool_call_id": call_ref.get("id"),
                "tool_name": tool_name,
                "arguments": tool_args,
                "raised_at": _now(),
            }
            STATE["status"] = "intervention_required"

        elif etype == "turn.done":
            STATE["final_report"] = _extract_final_report()
            if STATE["status"] == "remediating":
                STATE["status"] = "resolved"
            elif STATE["status"] == "remediation_denied":
                pass  # leave as denied -- the human's decision stands
            elif STATE["status"] == "investigating":
                # The turn ended without ever reaching an approval step --
                # found live: this used to unconditionally flip to
                # "resolved" here, so a turn that actually failed partway
                # through (e.g. the sre-tools MCP server being unreachable,
                # producing only turn.created + turn.done with nothing in
                # between) still showed a false "HEALTHY — resolved" banner.
                STATE["status"] = "error"
                if not STATE.get("error"):
                    STATE["error"] = (
                        "Turn ended without ever reaching a remediation step -- "
                        "the agent likely didn't complete its investigation. "
                        "Check TrueForge's own session/turn detail for the real cause."
                    )
            # else: some other in-between status -- leave it as-is rather
            # than guessing


def _resolve_tool_call(call_ref: dict) -> tuple[str, dict]:
    """tool.approval_required only gives us {id, source_event_id}; the actual
    tool name + arguments live on the model.message event referenced by
    source_event_id. Defensive about the exact tool_calls[] shape since it
    wasn't verbatim-confirmed against a live instance -- see README/VERIFY
    notes in trueforge_client.py."""
    call_id = call_ref.get("id")
    source_id = call_ref.get("source_event_id") or call_ref.get("sourceEventId")
    source_evt = _event_index.get(source_id, {})
    for tc in source_evt.get("tool_calls", []) or []:
        if tc.get("id") != call_id and call_id is not None:
            continue
        # OpenAI-style: {id, type:"function", function:{name, arguments}}
        if "function" in tc:
            fn = tc["function"]
            name = fn.get("name", "apply_system_change")
            raw_args = fn.get("arguments", {})
        else:
            name = tc.get("name", "apply_system_change")
            raw_args = tc.get("arguments", tc.get("input", {}))
        if isinstance(raw_args, str):
            import json
            try:
                raw_args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                raw_args = {"raw": raw_args}
        return name, raw_args or {}
    return "apply_system_change", {}


def _extract_final_report() -> str | None:
    for entry in reversed(STATE["events"]):
        if entry["type"] == "model.message":
            content = entry["data"].get("content")
            if content:
                return content if isinstance(content, str) else str(content)
    return None


def _run_turn(session_id: str, input_items: list[dict]) -> None:
    try:
        tf.stream_turn(session_id, input_items, _record_event)
    except Exception as exc:  # noqa: BLE001 -- surface any harness/network error to the dashboard
        with _lock:
            STATE["error"] = f"{type(exc).__name__}: {exc}"


class IncidentWebhook(BaseModel):
    alert: str
    error_rate_pct: float
    deploy_id: str | None = None
    detected_at: str | None = None
    source: str | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/incident")
def webhook_incident(payload: IncidentWebhook):
    with _lock:
        if STATE["status"] not in ("idle", "resolved"):
            return {"accepted": False, "reason": f"incident already in progress (status={STATE['status']})"}
        STATE.update(
            status="investigating",
            alert=payload.model_dump(),
            pending_approval=None,
            final_report=None,
            events=[],
            error=None,
        )
        _event_index.clear()
        global _seq
        _seq = 0

    try:
        session = tf.create_session(AGENT_NAME)
    except Exception as exc:  # noqa: BLE001 -- TrueForge unreachable/misconfigured
        with _lock:
            STATE["status"] = "idle"
            STATE["error"] = (
                f"Could not reach TrueForge at {tf.BASE_URL} to create a session "
                f"for agent '{AGENT_NAME}': {type(exc).__name__}: {exc}. Is "
                f"`npx @truefoundry/trueforge@latest` running, and did you run "
                f"scripts/register.py?"
            )
        raise HTTPException(502, STATE["error"])

    session_id = session.get("id") or session.get("session_id") or session.get("data", {}).get("id")
    if not session_id:
        with _lock:
            STATE["status"] = "idle"
            STATE["error"] = f"TrueForge returned no session id: {session}"
        raise HTTPException(502, STATE["error"])
    with _lock:
        STATE["session_id"] = session_id

    intro = (
        f"ALERT: {payload.alert} fired for demo-app. "
        f"error_rate_pct={payload.error_rate_pct}, deploy_id={payload.deploy_id}, "
        f"detected_at={payload.detected_at}. Investigate using the SRE playbook "
        f"and drive this to resolution."
    )
    threading.Thread(
        target=_run_turn, args=(session_id, [tf.user_message(intro)]), daemon=True
    ).start()
    return {"accepted": True, "session_id": session_id}


class Decision(BaseModel):
    reason: str | None = None


@app.post("/approve")
def approve(decision: Decision):
    with _lock:
        pending = STATE.get("pending_approval")
        if not pending:
            raise HTTPException(409, "no pending approval")
        session_id = STATE["session_id"]
        item = tf.tool_approval(
            pending["thread_id"], pending["tool_call_id"], allow=True, reason=decision.reason
        )
        _seq_note = {
            "type": "human.decision",
            "decision": "allow",
            "reason": decision.reason,
            "tool_call_id": pending["tool_call_id"],
        }
        global _seq
        _seq += 1
        STATE["events"].append({"seq": _seq, "at": _now(), "type": "human.decision", "data": _seq_note})
        STATE["pending_approval"] = None
        STATE["status"] = "remediating"

    threading.Thread(target=_run_turn, args=(session_id, [item]), daemon=True).start()
    return {"ok": True}


@app.post("/deny")
def deny(decision: Decision):
    with _lock:
        pending = STATE.get("pending_approval")
        if not pending:
            raise HTTPException(409, "no pending approval")
        session_id = STATE["session_id"]
        item = tf.tool_approval(
            pending["thread_id"], pending["tool_call_id"], allow=False,
            reason=decision.reason or "denied by judge",
        )
        global _seq
        _seq += 1
        note = {
            "type": "human.decision",
            "decision": "deny",
            "reason": decision.reason,
            "tool_call_id": pending["tool_call_id"],
        }
        STATE["events"].append({"seq": _seq, "at": _now(), "type": "human.decision", "data": note})
        STATE["pending_approval"] = None
        STATE["status"] = "remediation_denied"

    threading.Thread(target=_run_turn, args=(session_id, [item]), daemon=True).start()
    return {"ok": True}


@app.get("/state")
def get_state():
    with _lock:
        # shallow copy is enough -- values themselves aren't mutated in place
        # after being appended
        return dict(STATE)


@app.post("/reset")
def reset():
    with _lock:
        STATE.update(
            status="idle",
            alert=None,
            session_id=None,
            turn_id=None,
            pending_approval=None,
            final_report=None,
            events=[],
            error=None,
        )
        _event_index.clear()
        global _seq
        _seq = 0
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("BACKEND_PORT", 8083)))
