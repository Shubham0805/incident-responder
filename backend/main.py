"""
backend/main.py -- the orchestrator that wires everything together:

  telemetry webhook -> real TrueForge session/turn -> SSE consumption ->
  in-memory state -> polled by the Streamlit dashboard -> human approval ->
  resume the paused turn.

Deliberately kept to plain FastAPI + a background thread per active turn
(no queue/DB) -- this is a hackathon demo of the harness, not a production
incident-management system.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import trueforge_client as tf
import deterministic_fallback as fallback
import known_patterns
import pattern_insights

AGENT_NAME = os.environ.get("TRUEFORGE_AGENT_NAME", "incident-responder")
MINER_AGENT_NAME = os.environ.get("TRUEFORGE_MINER_AGENT_NAME", "pattern-miner")
MINING_INTERVAL_SECONDS = int(os.environ.get("PATTERN_MINING_INTERVAL_SECONDS", "900"))
PATTERN_MINING_ENABLED = os.environ.get("PATTERN_MINING_ENABLED", "true").strip().lower() not in ("0", "false", "no")

app = FastAPI(title="incident-responder backend")

_lock = threading.Lock()

# Separate from STATE/_lock on purpose: STATE["events"] is cleared on every
# new incident and on /reset, but mining runs independently of the incident
# lifecycle -- a Pattern Insights panel showing 'no history' every time an
# incident starts would defeat the point. Not persisted to disk (matches
# STATE's own in-memory-only design) -- the durable record of what actually
# changed lives in pattern_insights.json via pattern_insights.py itself.
_mining_lock = threading.Lock()
MAX_MINING_RUNS = 20
MINING_STATE: dict[str, Any] = {
    "enabled": PATTERN_MINING_ENABLED,
    "interval_seconds": MINING_INTERVAL_SECONDS,
    "last_run_at": None,
    "last_status": None,  # completed | failed | skipped_no_change | skipped_empty
    "last_error": None,
    "last_fingerprint": None,
    "runs": [],  # bounded, newest-first log of recent mining runs
}

# Human-readable labels for the dashboard's "Status Orb" -- the coarse
# `status` field below drives color/urgency, `stage` (set from these tables)
# drives the more specific sub-line judges actually read to follow along.
STAGE_LABELS = {
    "idle": "💤 Idle",
    "investigating": "🧠 Analyzing incident",
    "intervention_required": "⏸️ Awaiting human intervention",
    "remediating": "🛠️ Applying approved rollback",
    "remediation_denied": "🚫 Remediation denied",
    "resolved": "✅ Resolved",
    "error": "⚠️ Ended without remediation",
    "deterministic_diagnosis": "🧮 LLM unavailable — running rule-based fallback diagnosis (no LLM)",
}
TOOL_STAGE_LABELS = {
    "tail_log": "📄 Reading application logs",
    "check_db_health": "🩺 Checking DB health (in sandbox)",
    "apply_system_change": "🛠️ Applying rollback",
}

STATE: dict[str, Any] = {
    "status": "idle",  # idle | investigating | intervention_required | remediating | remediation_denied | resolved
    "stage": STAGE_LABELS["idle"],  # finer-grained label for the dashboard's Status Orb
    "alert": None,
    "session_id": None,
    "turn_id": None,
    "pending_approval": None,
    "final_report": None,
    "events": [],       # transparent, ordered log of every raw SSE event -- shown verbatim to judges
    "terminal_log": [],  # best-effort human-readable lines derived from events, for the
                          # dashboard's "Live Sandbox Terminal Stream" panel -- see
                          # _terminal_entry() below for why this is best-effort, not exact
    "error": None,
}
_event_index: dict[str, dict] = {}  # event id -> event, for resolving tool_calls by source_event_id
# Live-verified (Aug 28 dry run): tool.response only carries a bare
# tool_call_id, no source_event_id back-reference to the model.message that
# proposed it -- so instead of trying to look that message back up, index
# every tool_call by its own id the moment a model.message proposes it.
_toolcall_index: dict[str, tuple[str, dict]] = {}  # tool_call id -> (name, args)
# model.message.delta's text arrives in small chunks -- buffered here per
# thread until the next non-delta event, then flushed as one terminal line
# instead of dozens of one-word fragments.
_delta_buffers: dict[str, list[str]] = {}
_seq = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_message_text(content) -> str | None:
    """model.message's `content` wasn't verbatim-confirmed as always a plain
    string -- some agent-harness wire formats use a list of content blocks
    instead (e.g. [{"type": "text", "text": "..."}]), or nest text under a
    dict. Try the plain-string case first (what this project's live dry run
    on Aug 27 showed for the final report extraction below), then the two
    other common shapes, so a differently-shaped model.message doesn't
    silently disappear from the terminal stream instead of just showing
    plainer text than intended."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text") for c in content
                 if isinstance(c, dict) and isinstance(c.get("text"), str)]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else None
    return None


def _extract_delta_text(evt: dict) -> str | None:
    """model.message.delta's chunk-text field name wasn't verbatim-confirmed
    either -- try a plain 'delta' string, a 'delta' dict with .text, or
    falling back to the same shapes _extract_message_text handles (some
    harnesses reuse 'content'/'text' on delta events too)."""
    delta = evt.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        text = delta.get("text")
        if isinstance(text, str):
            return text
    text = _extract_message_text(evt.get("content"))
    if text:
        return text
    text = evt.get("text")
    return text if isinstance(text, str) else None


def _terminal_entry(evt: dict) -> dict | None:
    """Best-effort: turn one raw SSE event into a single human-readable line
    for the dashboard's "Live Sandbox Terminal Stream" panel. TrueForge's
    event *types* (sandbox.created, tool.response, model.message, ...) are
    confirmed against a live instance -- see trueforge_client.py's module
    docstring -- but the exact field names INSIDE tool.response were never
    verbatim-confirmed, so this tries several plausible shapes and falls
    back to the raw event dict rather than silently dropping the line. If a
    live run shows different field names than guessed here, the full raw
    event log (still shown verbatim elsewhere in the dashboard) has the
    ground truth to fix this against -- same pattern as _resolve_tool_call."""
    etype = evt.get("type")

    if etype == "sandbox.created":
        sid = evt.get("id") or evt.get("sandbox_id") or evt.get("sandboxId") or "?"
        return {"kind": "system", "text": f"isolated Code Mode sandbox created ({sid})"}

    if etype == "model.message":
        text = _extract_message_text(evt.get("content"))
        if not text:
            text = evt.get("text") if isinstance(evt.get("text"), str) else None
        if text and text.strip():
            return {"kind": "agent", "text": text.strip()}
        return None  # a model.message with only tool_calls and no narration text

    if etype == "tool.response":
        # Live-verified (Aug 28 dry run): there's no source_event_id link on
        # this event, just a bare tool_call_id -- resolve it via
        # _toolcall_index (populated from every model.message's tool_calls
        # as they're proposed) instead of the old source-event lookup.
        call_ref = {"id": evt.get("tool_call_id")}
        name, _args = _resolve_tool_call(call_ref, default_name="(unresolved tool)")

        # Live-verified shape: `content` is a JSON-ENCODED STRING (not a
        # nested object), e.g. '{"success":true,"response":{"exitCode":0,
        # "result":"..."}}' -- parse it first, then drill into
        # response.result (or a top-level result), so only the meaningful
        # inner text ends up in the terminal line instead of the whole raw
        # wrapper. Falls back gracefully if a different tool response comes
        # back shaped differently (unparseable string, already a dict, etc).
        raw_content = evt.get("content")
        parsed = None
        if isinstance(raw_content, str):
            try:
                parsed = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                parsed = None
        elif isinstance(raw_content, dict):
            parsed = raw_content

        result = None
        if isinstance(parsed, dict):
            response_field = parsed.get("response")
            if isinstance(response_field, dict):
                result = response_field.get("result")
            if result is None:
                result = parsed.get("result")
        if result is None:
            result = evt.get("result")
        if result is None:
            result = evt.get("output")
        if result is None and parsed is not None:
            result = parsed  # couldn't find a nested result field -- show the whole parsed body
        if result is None:
            result = raw_content if raw_content is not None else {
                k: v for k, v in evt.items()
                if k not in ("type", "id", "event_id", "tool_call_id", "source_event_id")
            }
        body = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
        return {"kind": "tool", "text": f"$ {name}(...)\n{body}"}

    if etype == "human.decision":
        decision = evt.get("decision", "?")
        reason = evt.get("reason")
        text = f"human decision: {decision}" + (f" -- {reason!r}" if reason else "")
        return {"kind": "human", "text": text}

    return None


def _update_stage() -> None:
    """Refine STATE["stage"] beyond the coarse `status` -- e.g. during
    "investigating", show which specific tool the agent is using right now
    instead of a single static "investigating" label the whole time."""
    status = STATE["status"]
    if status != "investigating":
        STATE["stage"] = STAGE_LABELS.get(status, status)
        return
    for entry in reversed(STATE["events"]):
        d = entry["data"]
        if entry["type"] == "model.message":
            calls = d.get("tool_calls") or []
            if calls:
                c0 = calls[0]
                name = (c0.get("function") or {}).get("name") or c0.get("name")
                STATE["stage"] = TOOL_STAGE_LABELS.get(name, f"🧠 Using {name}")
                return
            if (d.get("content") or "").strip():
                STATE["stage"] = "🧠 Reasoning about the incident"
                return
        if entry["type"] == "sandbox.created":
            STATE["stage"] = "📦 Spinning up sandbox"
            return
    STATE["stage"] = STAGE_LABELS["investigating"]


def _record_event(evt: dict) -> None:
    """Called from the SSE-consuming thread for every event -- append to the
    transparent log, index it, and react to the ones that matter."""
    global _seq
    run_fallback = False
    with _lock:
        _seq += 1
        entry = {"seq": _seq, "at": _now(), "type": evt.get("type"), "data": evt}
        STATE["events"].append(entry)

        etype = evt.get("type")
        thread_id = evt.get("thread_id") or evt.get("threadId") or "main"

        if etype == "model.message":
            _index_tool_calls(evt)

        if etype == "model.message.delta":
            # Live-verified (Aug 28 dry run): the final model.message often
            # carries NO content at all -- the actual narration text streams
            # in via a burst of these delta events instead. Buffer chunks
            # per-thread and flush as ONE terminal line on the next
            # non-delta event, rather than either dropping the text
            # entirely or spamming one line per token.
            chunk = _extract_delta_text(evt)
            if chunk:
                _delta_buffers.setdefault(thread_id, []).append(chunk)
        else:
            buffered = _delta_buffers.pop(thread_id, None)
            if buffered:
                buffered_text = "".join(buffered).strip()
                if buffered_text:
                    STATE["terminal_log"].append(
                        {"seq": entry["seq"], "at": entry["at"], "kind": "agent", "text": buffered_text}
                    )
            term = _terminal_entry(evt)
            if term is not None:
                STATE["terminal_log"].append({"seq": entry["seq"], "at": entry["at"], **term})

        evt_id = evt.get("id") or evt.get("event_id")
        if evt_id:
            _event_index[evt_id] = evt

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
                # Non-LLM resilience net: don't just report failure, try to
                # actually diagnose it deterministically. Runs after the lock
                # is released below, since it makes blocking HTTP calls.
                run_fallback = True
            # else: some other in-between status -- leave it as-is rather
            # than guessing

        _update_stage()

    if run_fallback:
        threading.Thread(target=_run_deterministic_fallback, daemon=True).start()


def _index_tool_calls(source_evt: dict) -> None:
    """Called for every model.message -- index each tool_call it proposes by
    the tool_call's own id, so tool.response (which only gives us a bare
    tool_call_id, no back-reference -- live-verified Aug 28) and
    tool.approval_required can both resolve the real name/args with a
    direct lookup instead of guessing."""
    for tc in source_evt.get("tool_calls", []) or []:
        tc_id = tc.get("id")
        if not tc_id:
            continue
        if "function" in tc:  # OpenAI-style: {id, type:"function", function:{name, arguments}}
            fn = tc["function"]
            name = fn.get("name")
            raw_args = fn.get("arguments", {})
        else:
            name = tc.get("name")
            raw_args = tc.get("arguments", tc.get("input", {}))
        if not name:
            continue
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                raw_args = {"raw": raw_args}
        _toolcall_index[tc_id] = (name, raw_args or {})


def _resolve_tool_call(call_ref: dict, default_name: str = "apply_system_change") -> tuple[str, dict]:
    """Resolve a tool_call id to (name, args). Tries the direct index first
    (built by _index_tool_calls from every model.message as it's proposed --
    this is the reliable path, live-verified Aug 28). Falls back to the
    older source_event_id-based scan in case a differently-shaped event
    still carries that link, before giving up and returning `default_name`.

    `default_name` is what to report when resolution fails -- callers must
    pick a default that's safe to be WRONG about. tool.approval_required
    only ever fires for apply_system_change (the one gated tool), so
    defaulting to it there is a reasonable guess. Anywhere else (e.g. every
    tool.response, which fires for tail_log/check_db_health too) that same
    default is actively misleading -- see _terminal_entry()'s caller."""
    call_id = call_ref.get("id")
    if call_id and call_id in _toolcall_index:
        return _toolcall_index[call_id]

    source_id = call_ref.get("source_event_id") or call_ref.get("sourceEventId")
    source_evt = _event_index.get(source_id, {})
    for tc in source_evt.get("tool_calls", []) or []:
        if tc.get("id") != call_id and call_id is not None:
            continue
        if "function" in tc:
            fn = tc["function"]
            name = fn.get("name") or default_name
            raw_args = fn.get("arguments", {})
        else:
            name = tc.get("name") or default_name
            raw_args = tc.get("arguments", tc.get("input", {}))
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                raw_args = {"raw": raw_args}
        return name, raw_args or {}
    return default_name, {}


def _extract_final_report() -> str | None:
    for entry in reversed(STATE["events"]):
        if entry["type"] == "model.message":
            text = _extract_message_text(entry["data"].get("content"))
            if text:
                return text
    return None


def _run_turn(session_id: str, input_items: list[dict]) -> None:
    try:
        tf.stream_turn(session_id, input_items, _record_event)
    except Exception as exc:  # noqa: BLE001 -- surface any harness/network error to the dashboard
        with _lock:
            STATE["error"] = f"{type(exc).__name__}: {exc}"


def _run_deterministic_fallback() -> None:
    """Runs when the LLM-driven TrueForge turn ends without ever reaching a
    remediation step (rate-limited, misconfigured model, harness error --
    doesn't matter which). Does the SAME investigation the sre-playbook
    skill describes, but as plain deterministic Python calling demo-app
    directly (see deterministic_fallback.py) -- no LLM involved, so it
    can't fail the same way. Purely a resilience net: prefer the real agent
    whenever it works; this only runs after it has already failed."""
    global _seq
    with _lock:
        STATE["status"] = "deterministic_diagnosis"
        STATE["stage"] = STAGE_LABELS["deterministic_diagnosis"]
        _seq += 1
        STATE["events"].append({
            "seq": _seq, "at": _now(), "type": "fallback.started",
            "data": {"type": "fallback.started", "reason": STATE.get("error")},
        })

    result = fallback.diagnose()

    with _lock:
        _seq += 1
        STATE["events"].append({
            "seq": _seq, "at": _now(), "type": "fallback.diagnosis",
            "data": {"type": "fallback.diagnosis", **result},
        })
        if result.get("root_cause"):
            STATE["pending_approval"] = {
                "thread_id": None,
                "tool_call_id": "deterministic-fallback",
                "tool_name": "apply_system_change",
                "arguments": {"dsn": result["proposed_dsn"], "reason": result["reason"]},
                "raised_at": _now(),
                "via": "deterministic_fallback",
                "evidence": result["evidence"],
                "root_cause": result["root_cause"],
            }
            STATE["status"] = "intervention_required"
            STATE["stage"] = STAGE_LABELS["intervention_required"] + " (rule-based fallback, no LLM)"
            STATE["error"] = None
            STATE["final_report"] = (
                "LLM investigation did not complete, so a deterministic rule-based "
                "fallback ran instead (no model involved).\n\n"
                f"Root cause: {result['root_cause']}\n"
                f"Evidence: {result['evidence']}\n"
                f"Proposed fix: roll back to {result['proposed_dsn']}"
            )
        else:
            STATE["status"] = "error"
            STATE["stage"] = STAGE_LABELS["error"]
            STATE["error"] = (
                (STATE.get("error") or "").rstrip(".")
                + " | Deterministic fallback also could not identify a clear root cause: "
                + result.get("detail", "no obvious DSN drift found.")
            )


def _apply_fallback_fix(args: dict) -> None:
    """Companion to _run_deterministic_fallback: actually applies the
    approved fix directly against demo-app, then polls for recovery --
    mirrors what the LLM path's step 6 (verify recovery) does."""
    global _seq
    try:
        result = fallback.apply_fix(args["dsn"], args["reason"])
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _seq += 1
            STATE["events"].append({
                "seq": _seq, "at": _now(), "type": "fallback.apply_failed",
                "data": {"type": "fallback.apply_failed", "error": f"{type(exc).__name__}: {exc}"},
            })
            STATE["status"] = "error"
            STATE["stage"] = STAGE_LABELS["error"]
            STATE["error"] = f"Deterministic fallback's fix attempt failed: {type(exc).__name__}: {exc}"
        return

    healthy = False
    for _ in range(5):
        time.sleep(1)
        try:
            status = fallback._check_db_health()
            healthy = bool(status.get("healthy"))
            if healthy:
                break
        except Exception:  # noqa: BLE001
            pass

    with _lock:
        _seq += 1
        STATE["events"].append({
            "seq": _seq, "at": _now(), "type": "fallback.apply_result",
            "data": {"type": "fallback.apply_result", "result": result, "healthy_after": healthy},
        })
        STATE["status"] = "resolved" if healthy else "error"
        STATE["stage"] = STAGE_LABELS["resolved" if healthy else "error"]
        if not healthy:
            STATE["error"] = "Applied the rollback but demo-app still doesn't report healthy after ~5s."


def _run_pre_check() -> bool:
    """Runs BEFORE any LLM call -- deterministic-first architecture,
    gated by actual TRUSTED history, not just diagnosability. Tries the
    exact same diagnosis the post-failure fallback uses; if it's
    confident AND known_patterns considers this exact signature trusted
    (known_patterns.is_trusted() -- TRUST_THRESHOLD consecutive human
    approvals with no denial since, see known_patterns.py), resolve it
    right here -- zero TrueForge/LLM calls for a recognized, repeatedly-
    approved repeat. If diagnose() can't explain it at all, OR it can
    explain it but this signature is new or hasn't earned that much
    trust yet -- including one that has EVER been denied and hasn't
    since re-earned a clean approval streak -- this escalates to a real
    LLM-driven turn instead. A genuinely novel fault, or one with a
    shaky decision history, always gets a full investigation rather
    than a cold or contested guess dressed up as confidence just
    because the comparison itself was easy. Returns True if this fully
    handled the incident (STATE now has a pending_approval), False if
    it should fall through to a real LLM-driven turn."""
    global _seq
    with _lock:
        _seq += 1
        STATE["events"].append({
            "seq": _seq, "at": _now(), "type": "precheck.started",
            "data": {"type": "precheck.started"},
        })

    result = fallback.diagnose()

    if not result.get("root_cause"):
        with _lock:
            _seq += 1
            STATE["events"].append({
                "seq": _seq, "at": _now(), "type": "precheck.inconclusive",
                "data": {"type": "precheck.inconclusive", **result},
            })
        return False

    match = known_patterns.find_match(result["evidence"])

    if not match or not known_patterns.is_trusted(match):
        # Diagnosable, but either we've never seen this exact signature
        # before, or we have and it just hasn't earned enough trust yet
        # -- not enough clean approvals since its last denial (or ever,
        # if it's never been denied). Escalate to a real investigation
        # rather than auto-resolving on a nonexistent or shaky track
        # record; approve()/deny() will record the outcome once a human
        # actually reviews it, moving this signature closer to (or, on
        # a deny, back away from) being trusted next time.
        with _lock:
            _seq += 1
            STATE["events"].append({
                "seq": _seq, "at": _now(), "type": "precheck.escalating",
                "data": {
                    "type": "precheck.escalating", **result,
                    "known_pattern": match,  # None if genuinely first-seen
                },
            })
        return False

    with _lock:
        _seq += 1
        STATE["events"].append({
            "seq": _seq, "at": _now(), "type": "precheck.diagnosis",
            "data": {"type": "precheck.diagnosis", **result, "known_pattern": match},
        })
        STATE["pending_approval"] = {
            "thread_id": None,
            "tool_call_id": "deterministic-precheck",
            "tool_name": "apply_system_change",
            "arguments": {"dsn": result["proposed_dsn"], "reason": result["reason"]},
            "raised_at": _now(),
            "via": "deterministic_fallback",
            "evidence": result["evidence"],
            "root_cause": result["root_cause"],
        }
        STATE["status"] = "intervention_required"
        confidence_note = (
            f" (trusted pattern: {match['consecutive_approvals']} consecutive approval(s) "
            f"since its last denial, {match['approved_count']} approved / "
            f"{match['denied_count']} denied all-time)"
        )
        STATE["stage"] = (
            STAGE_LABELS["intervention_required"]
            + f" (deterministic pre-check, no LLM needed{confidence_note})"
        )
        STATE["error"] = None
        history_note = (
            f"\n\nTrusted after {match['consecutive_approvals']} consecutive approval(s) "
            f"since its last denial ({match['approved_count']} approved / "
            f"{match['denied_count']} denied all-time)."
        )
        STATE["final_report"] = (
            "This incident matched a known signature deterministically, so no LLM "
            "call was made at all.\n\n"
            f"Root cause: {result['root_cause']}\n"
            f"Evidence: {result['evidence']}\n"
            f"Proposed fix: roll back to {result['proposed_dsn']}"
            + history_note
        )
    return True


def _record_mining_run(status: str, **extra) -> None:
    with _mining_lock:
        MINING_STATE["last_run_at"] = _now()
        MINING_STATE["last_status"] = status
        MINING_STATE["last_error"] = extra.get("error")
        run = {"at": MINING_STATE["last_run_at"], "status": status, **extra}
        MINING_STATE["runs"].insert(0, run)
        MINING_STATE["runs"] = MINING_STATE["runs"][:MAX_MINING_RUNS]


def _build_mining_prompt(patterns: list[dict]) -> str:
    return (
        "Here is the current known_patterns.json snapshot -- each entry has "
        "signature, variants, approved_count, denied_count, "
        "consecutive_approvals, trust_threshold, suspicious, "
        "root_cause_summary, first_seen, last_seen. Follow the pattern-miner "
        "skill and respond with only the JSON proposal array.\n\n"
        + json.dumps(patterns, indent=2)
    )


def _parse_mining_output(text: str) -> list[dict]:
    """The skill asks for raw JSON only, but models drift toward wrapping
    output in a markdown code fence even when told not to -- tolerate that
    one deviation rather than losing every proposal to a formatting slip.
    Anything else unparseable is treated as zero proposals, not an error;
    a mining pass that produced nothing usable just tries again next cycle."""
    if not text:
        return []
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _run_pattern_mining() -> None:
    """One mining pass: create a session for the pattern-miner agent (see
    skills/pattern-miner), hand it the current known_patterns.json snapshot,
    parse its JSON proposal list, and let pattern_insights.py decide what
    auto-applies vs queues for a human. Safe to call directly (the /mine
    endpoint) or from the periodic loop below -- always runs to completion
    or records a failure, never raises out to its caller."""
    patterns = known_patterns.load()
    if not patterns:
        _record_mining_run("skipped_empty")
        return

    local_events: list[dict] = []
    try:
        session = tf.create_session(MINER_AGENT_NAME)
        session_id = session.get("id") or session.get("session_id") or session.get("data", {}).get("id")
        if not session_id:
            raise RuntimeError(f"TrueForge returned no session id: {session}")

        tf.stream_turn(session_id, [tf.user_message(_build_mining_prompt(patterns))], local_events.append)

        report_text = None
        for evt in reversed(local_events):
            if evt.get("type") == "model.message":
                report_text = _extract_message_text(evt.get("content"))
                if report_text:
                    break

        proposals = _parse_mining_output(report_text or "")
        added = pattern_insights.submit_proposals(proposals)
        _record_mining_run(
            "completed",
            raw_proposal_count=len(proposals),
            accepted_count=len(added),
            insights=added,
        )
    except Exception as exc:  # noqa: BLE001 -- the mining pass must never take the backend down with it
        _record_mining_run("failed", error=f"{type(exc).__name__}: {exc}")


def _pattern_mining_loop() -> None:
    """Background loop: wakes periodically, skips the (paid-in-latency,
    rate-limited) mining turn entirely if known_patterns.json hasn't
    changed since the last pass -- see MINING_STATE['last_fingerprint'] --
    so idle periods cost nothing. First check is short so a demo doesn't
    have to wait a full interval to see it work; every check after that
    waits the configured interval."""
    if not PATTERN_MINING_ENABLED:
        return
    time.sleep(min(60, MINING_INTERVAL_SECONDS))
    while True:
        try:
            patterns = known_patterns.load()
            if not patterns:
                _record_mining_run("skipped_empty")
            else:
                fingerprint = json.dumps(patterns, sort_keys=True)
                if fingerprint == MINING_STATE.get("last_fingerprint"):
                    _record_mining_run("skipped_no_change")
                else:
                    _run_pattern_mining()
                    with _mining_lock:
                        MINING_STATE["last_fingerprint"] = fingerprint
        except Exception as exc:  # noqa: BLE001 -- the loop itself must never die
            _record_mining_run("failed", error=f"{type(exc).__name__}: {exc}")
        time.sleep(MINING_INTERVAL_SECONDS)


class IncidentWebhook(BaseModel):
    alert: str
    error_rate_pct: float
    deploy_id: str | None = None
    detected_at: str | None = None
    source: str | None = None
    force_llm: bool = False  # TEST-ONLY: skip the deterministic pre-check and
                             # always go straight to a real TrueForge/LLM turn,
                             # even for a fault the pre-check could resolve on
                             # its own. Lets you verify the actual agent path
                             # end-to-end (session, sandbox, tool-approval gate)
                             # against a failure the pre-check would otherwise
                             # always intercept first. Never set by the dashboard
                             # or the telemetry watcher -- normal incidents never
                             # set this, so default behavior is unchanged.


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/debug/simulate_llm_failure")
def debug_simulate_llm_failure():
    """TEST-ONLY: simulates the exact condition the deterministic fallback
    reacts to (a TrueForge turn ending in turn.done while STATE['status']
    is still 'investigating', i.e. it never reached tool.approval_required)
    -- WITHOUT ever calling tf.create_session/tf.stream_turn, so this makes
    zero requests to TrueForge or any LLM provider. Lets you verify the
    whole fallback -> pending_approval -> /approve -> apply_fix chain
    against the real demo-app and the real dashboard for free. Make sure
    demo-app is currently in its Inject Failure state first, or the
    fallback will correctly report 'no root cause found' -- that's not a
    bug, it just means there's nothing to diagnose right now."""
    with _lock:
        STATE.update(
            status="investigating", stage=STAGE_LABELS["investigating"],
            alert={"alert": "SimulatedFailure (debug)", "error_rate_pct": 99.9,
                   "deploy_id": "debug", "detected_at": _now()},
            pending_approval=None, final_report=None, events=[], terminal_log=[], error=None,
            session_id="debug-session",
        )
        _event_index.clear()
        _toolcall_index.clear()
        _delta_buffers.clear()
        global _seq
        _seq = 0
    _record_event({"type": "turn.created", "id": "debug-turn"})
    _record_event({"type": "turn.done", "id": "debug-turn-done", "state": {"status": "error"}})
    return {"ok": True, "note": "simulated LLM failure injected -- watch the dashboard, zero API calls made"}


@app.post("/webhook/incident")
def webhook_incident(payload: IncidentWebhook):
    with _lock:
        if STATE["status"] not in ("idle", "resolved"):
            return {"accepted": False, "reason": f"incident already in progress (status={STATE['status']})"}
        STATE.update(
            status="investigating",
            stage=STAGE_LABELS["investigating"],
            alert=payload.model_dump(),
            pending_approval=None,
            final_report=None,
            events=[],
            terminal_log=[],
            error=None,
        )
        _event_index.clear()
        _toolcall_index.clear()
        _delta_buffers.clear()
        global _seq
        _seq = 0

    # Deterministic-first: try the free, instant check before ever
    # spending an LLM call. Only escalate to a real TrueForge/LLM-driven
    # investigation if this can't confidently diagnose it -- or if the
    # caller explicitly asked to bypass it (force_llm, test-only).
    if not payload.force_llm and _run_pre_check():
        return {"accepted": True, "session_id": None, "via": "deterministic_precheck"}

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
    global _seq
    with _lock:
        pending = STATE.get("pending_approval")
        if not pending:
            raise HTTPException(409, "no pending approval")

        if pending.get("via") == "deterministic_fallback":
            # No live TrueForge turn to resume -- this decision applies the
            # fix directly against demo-app instead. Still gated on this
            # same human approval, same as the LLM path's tool call.
            args = pending["arguments"]
            _seq += 1
            STATE["events"].append({
                "seq": _seq, "at": _now(), "type": "human.decision",
                "data": {"type": "human.decision", "decision": "allow",
                         "reason": decision.reason, "via": "deterministic_fallback"},
            })
            if pending.get("evidence") and pending.get("root_cause"):
                known_patterns.record_outcome(
                    pending["evidence"], pending["root_cause"], args, "allow", decision.reason,
                )
            STATE["pending_approval"] = None
            STATE["status"] = "remediating"
            STATE["stage"] = STAGE_LABELS["remediating"] + " (rule-based fallback, no LLM)"
            STATE["error"] = None
            threading.Thread(target=_apply_fallback_fix, args=(args,), daemon=True).start()
            return {"ok": True}

        session_id = STATE.get("session_id")
        if not session_id:
            # Live-verified bug (Aug 28 dry run): this used to fall through
            # and start a background thread that POSTed to
            # ".../sessions/None/turns" -- a confusing 404 discovered only
            # after the human had already clicked Approve/Deny. Fail loudly
            # and immediately instead, and don't touch pending_approval/
            # status, so the UI still shows a real pending decision rather
            # than a fake "remediating" state that's actually going nowhere.
            STATE["error"] = (
                "Cannot resume the TrueForge turn: no session id is recorded "
                "in STATE right now, so this would 404 against "
                "'.../sessions/None/turns'. This shouldn't happen mid-incident "
                "-- check backend.log for how session_id was cleared (a stray "
                "Reset Demo click, or a second backend process). Try Reset "
                "Demo + Inject Failure for a clean run."
            )
            raise HTTPException(409, STATE["error"])
        item = tf.tool_approval(
            pending["thread_id"], pending["tool_call_id"], allow=True, reason=decision.reason
        )
        _seq_note = {
            "type": "human.decision",
            "decision": "allow",
            "reason": decision.reason,
            "tool_call_id": pending["tool_call_id"],
        }
        _seq += 1
        STATE["events"].append({"seq": _seq, "at": _now(), "type": "human.decision", "data": _seq_note})
        if pending.get("tool_name") == "apply_system_change":
            # Feed this real-agent-path decision into the same learning
            # loop the deterministic path uses. Re-diagnose fresh right
            # now -- demo-app is still in its corrupted state (the fix
            # below hasn't actually run yet), so this captures the same
            # live evidence _run_pre_check() would have. Only records if
            # it still resolves to a recognizable signature; if the LLM
            # fixed something this deterministic check can't explain,
            # this silently skips recording rather than inventing one.
            llm_result = fallback.diagnose()
            if llm_result.get("root_cause"):
                known_patterns.record_outcome(
                    llm_result["evidence"], llm_result["root_cause"],
                    pending["arguments"], "allow", decision.reason,
                )
        STATE["pending_approval"] = None
        STATE["status"] = "remediating"
        STATE["stage"] = STAGE_LABELS["remediating"]
        STATE["error"] = None  # clear any stale earlier error now that this step is actually proceeding

    threading.Thread(target=_run_turn, args=(session_id, [item]), daemon=True).start()
    return {"ok": True}


@app.post("/deny")
def deny(decision: Decision):
    global _seq
    with _lock:
        pending = STATE.get("pending_approval")
        if not pending:
            raise HTTPException(409, "no pending approval")

        if pending.get("via") == "deterministic_fallback":
            _seq += 1
            STATE["events"].append({
                "seq": _seq, "at": _now(), "type": "human.decision",
                "data": {"type": "human.decision", "decision": "deny",
                         "reason": decision.reason, "via": "deterministic_fallback"},
            })
            if pending.get("evidence") and pending.get("root_cause"):
                known_patterns.record_outcome(
                    pending["evidence"], pending["root_cause"], pending["arguments"], "deny", decision.reason,
                )
            STATE["pending_approval"] = None
            STATE["status"] = "remediation_denied"
            STATE["stage"] = STAGE_LABELS["remediation_denied"] + " (rule-based fallback, no LLM)"
            STATE["error"] = None
            return {"ok": True}

        session_id = STATE.get("session_id")
        if not session_id:
            # Same guard as approve() -- see its comment for the live-verified
            # bug this prevents (a confusing ".../sessions/None/turns" 404
            # discovered only after the human had already clicked Deny).
            STATE["error"] = (
                "Cannot resume the TrueForge turn to record this denial: no "
                "session id is recorded in STATE right now, so this would "
                "404 against '.../sessions/None/turns'. This shouldn't happen "
                "mid-incident -- check backend.log for how session_id was "
                "cleared. Try Reset Demo + Inject Failure for a clean run."
            )
            raise HTTPException(409, STATE["error"])
        item = tf.tool_approval(
            pending["thread_id"], pending["tool_call_id"], allow=False,
            reason=decision.reason or "denied by judge",
        )
        _seq += 1
        note = {
            "type": "human.decision",
            "decision": "deny",
            "reason": decision.reason,
            "tool_call_id": pending["tool_call_id"],
        }
        STATE["events"].append({"seq": _seq, "at": _now(), "type": "human.decision", "data": note})
        if pending.get("tool_name") == "apply_system_change":
            # Same learning-loop hookup as approve() above -- see its
            # comment. Nothing gets applied on a deny either way, so
            # demo-app is still corrupted and diagnose() finds the same
            # evidence it would have before the fix.
            llm_result = fallback.diagnose()
            if llm_result.get("root_cause"):
                known_patterns.record_outcome(
                    llm_result["evidence"], llm_result["root_cause"],
                    pending["arguments"], "deny", decision.reason,
                )
        STATE["pending_approval"] = None
        STATE["status"] = "remediation_denied"
        STATE["stage"] = STAGE_LABELS["remediation_denied"]
        STATE["error"] = None  # clear any stale earlier error now that this step is actually proceeding

    threading.Thread(target=_run_turn, args=(session_id, [item]), daemon=True).start()
    return {"ok": True}


@app.post("/mine")
def mine_now():
    """DEMO/TEST: trigger a pattern-mining pass immediately instead of
    waiting for the periodic loop. Runs in a background thread -- this
    returns right away, watch /insights or the dashboard for the result."""
    if not known_patterns.load():
        return {"accepted": False, "reason": "known_patterns.json is empty -- nothing to mine yet"}
    threading.Thread(target=_run_pattern_mining, daemon=True).start()
    return {"accepted": True}


@app.get("/patterns")
def get_patterns():
    """Read-only view of the deterministic pre-check's learned knowledge
    base -- what the dashboard's Pattern Insights panel renders as a
    table alongside the mining agent's proposals."""
    return {"patterns": known_patterns.load()}


@app.get("/insights")
def get_insights():
    return {"insights": pattern_insights.load_insights(), "mining": dict(MINING_STATE)}


@app.post("/insights/{insight_id}/approve")
def approve_insight(insight_id: str, decision: Decision):
    record = pattern_insights.resolve_insight(insight_id, "approve", decision.reason)
    if record is None:
        raise HTTPException(409, "no pending insight with that id")
    return {"ok": True, "insight": record}


@app.post("/insights/{insight_id}/reject")
def reject_insight(insight_id: str, decision: Decision):
    record = pattern_insights.resolve_insight(insight_id, "reject", decision.reason)
    if record is None:
        raise HTTPException(409, "no pending insight with that id")
    return {"ok": True, "insight": record}


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
            stage=STAGE_LABELS["idle"],
            alert=None,
            session_id=None,
            turn_id=None,
            pending_approval=None,
            final_report=None,
            events=[],
            terminal_log=[],
            error=None,
        )
        _event_index.clear()
        _toolcall_index.clear()
        _delta_buffers.clear()
        global _seq
        _seq = 0
    return {"ok": True}


if PATTERN_MINING_ENABLED:
    threading.Thread(target=_pattern_mining_loop, daemon=True).start()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("BACKEND_PORT", 8083)))
