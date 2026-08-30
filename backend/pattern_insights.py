"""
pattern_insights.py -- the review queue the pattern-mining agent's proposals
flow through (see _run_pattern_mining in main.py and skills/pattern-miner).

The mining agent only ever produces PROPOSALS (a JSON array of {type,
confidence, reasoning, payload}); it never touches known_patterns.py's
storage directly. This module is the one place that turns a proposal into
an actual change -- and decides, per proposal, whether that change is safe
enough to apply immediately or needs a human's sign-off first:

  - merge:     auto-applies only at confidence == "high". A merge pools two
               signatures' approve/deny counts for reporting but ALWAYS
               resets the merged group's consecutive-approval streak (see
               known_patterns.merge_signatures) -- so even an auto-applied
               merge can't let a fix skip review it never actually earned.
  - flag:      always auto-applies. Flagging a pattern "suspicious" only
               ever forces MORE human review later (is_trusted() becomes
               unconditionally False for it) -- there's no unsafe direction
               to auto-apply here.
  - threshold: auto-applies only if the proposed threshold is >= the
               pattern's current effective one (more caution, or no
               change). A LOWER threshold -- the one lever that reduces
               how much history a pattern needs before auto-resolving --
               always queues for a human, regardless of stated confidence.
               The direction is computed here from known_patterns' actual
               current value, never trusted from the LLM's own claim.

Nothing here is silent: every proposal, auto-applied or queued, is written
to INSIGHTS_PATH and (via main.py) appended to STATE["events"], so an
auto-applied change is fully visible in the transparent event log -- it
just isn't blocking on a click.
"""

from __future__ import annotations

import json
import pathlib
import threading
import uuid
from datetime import datetime, timezone

import known_patterns as kp

INSIGHTS_PATH = pathlib.Path(__file__).resolve().parent / "pattern_insights.json"
MAX_INSIGHTS = 200  # bounded, oldest-by-created_at drop off past this
VALID_TYPES = {"merge", "threshold", "flag"}
VALID_CONFIDENCE = {"high", "medium", "low"}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_insights() -> list[dict]:
    if not INSIGHTS_PATH.exists():
        return []
    try:
        return json.loads(INSIGHTS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_insights(insights: list[dict]) -> None:
    INSIGHTS_PATH.write_text(json.dumps(insights, indent=2))


def _normalize(raw: dict) -> dict | None:
    """Defensively coerce one raw proposal from the LLM's JSON output into
    a well-formed shape, or return None if it's unusable. Never trust the
    model's JSON blindly -- a malformed proposal is dropped, not crashed
    on, so one bad entry in the mining agent's output doesn't lose the
    other good ones."""
    if not isinstance(raw, dict):
        return None
    ptype = raw.get("type")
    if ptype not in VALID_TYPES:
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return None
    confidence = raw.get("confidence")
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"  # missing/invalid confidence defaults conservative
    reasoning = raw.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        reasoning = "(mining agent did not provide reasoning)"

    if ptype == "merge":
        sig_a, sig_b = payload.get("signature_a"), payload.get("signature_b")
        if not sig_a or not sig_b or not isinstance(sig_a, str) or not isinstance(sig_b, str):
            return None
        canonical = payload.get("canonical") if payload.get("canonical") in (sig_a, sig_b) else sig_a
        payload = {"signature_a": sig_a, "signature_b": sig_b, "canonical": canonical}
    elif ptype == "threshold":
        sig, new_t = payload.get("signature"), payload.get("new_threshold")
        if not sig or not isinstance(sig, str) or not isinstance(new_t, int) or new_t < 1:
            return None
        payload = {"signature": sig, "new_threshold": new_t}
    else:  # flag
        sig = payload.get("signature")
        if not sig or not isinstance(sig, str):
            return None
        flag_reason = payload.get("reason")
        if not isinstance(flag_reason, str) or not flag_reason.strip():
            flag_reason = reasoning
        payload = {"signature": sig, "reason": flag_reason}

    return {"type": ptype, "confidence": confidence, "reasoning": reasoning.strip(), "payload": payload}


def _is_duplicate(candidate: dict, existing: list[dict]) -> bool:
    """Skip re-proposing something already pending, already applied, or
    already a no-op given the store's current state -- the mining agent
    sees the whole store every run, so without this it would re-propose
    the same merge/flag/threshold change every single cycle."""
    ptype, payload = candidate["type"], candidate["payload"]

    if ptype == "merge":
        sig_a = kp.canonicalize(payload["signature_a"])
        sig_b = kp.canonicalize(payload["signature_b"])
        if sig_a == sig_b:
            return True  # already merged (or always were the same signature)
        if kp.get_pattern(sig_a) is None or kp.get_pattern(sig_b) is None:
            return True  # miner hallucinated a signature we don't actually have -- drop it
    elif ptype == "threshold":
        pattern = kp.get_pattern(payload["signature"])
        if pattern is None:
            return True  # miner hallucinated a signature we don't have -- drop it
        current = pattern.get("trust_threshold") or kp.TRUST_THRESHOLD
        if current == payload["new_threshold"]:
            return True  # no-op
    else:  # flag
        pattern = kp.get_pattern(payload["signature"])
        if pattern is None:
            return True
        if (pattern.get("suspicious") or {}).get("flag"):
            return True  # already flagged

    for e in existing:
        if e.get("status") != "pending" or e.get("type") != ptype:
            continue
        if e.get("payload") == payload:
            return True
    return False


def _decide_auto_apply(candidate: dict) -> bool:
    """The actual safety gate -- see module docstring. Computed from
    known_patterns' real current state, never from anything the LLM
    claims about direction or confidence beyond the plain confidence
    label itself."""
    ptype, payload = candidate["type"], candidate["payload"]
    if ptype == "flag":
        return True
    if ptype == "merge":
        return candidate["confidence"] == "high"
    if ptype == "threshold":
        pattern = kp.get_pattern(payload["signature"])
        current = (pattern or {}).get("trust_threshold") or kp.TRUST_THRESHOLD
        return payload["new_threshold"] >= current  # raise or unchanged -> safe; lower -> never auto
    return False


def _execute(candidate: dict, source: str) -> None:
    """Actually perform the known_patterns.py mutation for an approved
    (auto or human) proposal."""
    ptype, payload = candidate["type"], candidate["payload"]
    if ptype == "merge":
        kp.merge_signatures(payload["signature_a"], payload["signature_b"], canonical=payload["canonical"])
    elif ptype == "threshold":
        kp.set_trust_threshold(payload["signature"], payload["new_threshold"], source=source)
    else:
        kp.set_suspicious(payload["signature"], True, payload["reason"], source=source)


def submit_proposals(raw_proposals: list[dict]) -> list[dict]:
    """Entry point called after a mining turn completes with the model's
    parsed JSON output. Normalizes, dedupes, auto-applies what's safe to,
    queues the rest, persists all of it, and returns just the newly added
    insight records (for the caller to log into STATE['events'])."""
    with _lock:
        existing = load_insights()
        added: list[dict] = []
        for raw in raw_proposals if isinstance(raw_proposals, list) else []:
            candidate = _normalize(raw)
            if candidate is None or _is_duplicate(candidate, existing):
                continue
            record = {
                "id": uuid.uuid4().hex[:12],
                "created_at": _now(),
                "resolved_at": None,
                "resolved_by": None,
                **candidate,
                "status": "pending",
            }
            if _decide_auto_apply(candidate):
                _execute(candidate, source="pattern-miner (auto)")
                record["status"] = "auto_approved"
                record["resolved_at"] = _now()
                record["resolved_by"] = "auto"
            existing.append(record)
            added.append(record)

        if added:
            existing.sort(key=lambda e: e.get("created_at", ""), reverse=True)
            if len(existing) > MAX_INSIGHTS:
                existing = existing[:MAX_INSIGHTS]
            _save_insights(existing)
        return added


def resolve_insight(insight_id: str, decision: str, reason: str | None = None) -> dict | None:
    """Human review of a queued proposal. decision is 'approve' or
    'reject'. Returns the updated record, or None if insight_id doesn't
    match a pending proposal."""
    with _lock:
        insights = load_insights()
        record = next((e for e in insights if e.get("id") == insight_id), None)
        if record is None or record.get("status") != "pending":
            return None
        if decision == "approve":
            _execute(record, source="human")
            record["status"] = "approved"
        else:
            record["status"] = "rejected"
        record["resolved_at"] = _now()
        record["resolved_by"] = "human"
        if reason:
            record["resolution_reason"] = reason
        _save_insights(insights)
        return record
