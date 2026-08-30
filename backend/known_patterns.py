"""
known_patterns.py -- a small, bounded "memory" of past incident outcomes,
used ONLY to inform the deterministic pre-check (see _run_pre_check in
main.py) -- never fed into the LLM's context per incident, so this can grow
indefinitely without ever making a per-incident prompt bigger. Every time a
human approves or denies a deterministic-path fix, the outcome is distilled
into one compact entry here (a signature + counts), not the raw incident.
The more incidents this sees, the more of them get caught by this free,
instant path instead of ever needing an LLM call.

The ONE place this module does get read by an LLM is out-of-band and
periodic: pattern_insights.py's mining agent (see _run_pattern_mining in
main.py) reads the whole store every so often to propose merges, threshold
tuning, and suspicious-pattern flags -- see canonicalize()/merge_signatures()
below for the mechanism those proposals act through. That LLM never sits in
the per-incident path; it only ever curates the data _run_pre_check() reads.
"""

from __future__ import annotations

import json
import pathlib
import threading
from datetime import datetime, timezone

PATTERNS_PATH = pathlib.Path(__file__).resolve().parent / "known_patterns.json"
GROUPS_PATH = pathlib.Path(__file__).resolve().parent / "pattern_groups.json"
MAX_PATTERNS = 50  # bounded -- oldest-by-last-seen entries drop off past this
TRUST_THRESHOLD = 2  # default consecutive clean approvals a pattern needs
                     # before _run_pre_check() will auto-resolve it -- see
                     # is_trusted(); a pattern can override this via its own
                     # "trust_threshold" field (set by an approved mining
                     # proposal -- see pattern_insights.py).
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signature(evidence: dict) -> str:
    """A stable fingerprint for 'the same kind of incident'. Built only
    from the fields diagnose() actually found drifted -- never volatile
    ones like timestamps or line counts, so the same underlying fault
    always hashes the same way. There are two independent fault
    dimensions on the DB config object (dsn, pool_size), so this includes
    only the part(s) that actually differ -- a DSN-only incident, a
    pool_size-only incident, and a compound incident where both drifted
    all get distinct RAW signatures. Two raw signatures can still end up
    sharing one learning history if a mining proposal merges them -- see
    canonicalize()."""
    parts = []
    current_dsn = evidence.get("current_dsn")
    good_dsn = evidence.get("last_known_good_dsn")
    if current_dsn != good_dsn:
        parts.append(f"dsn:{current_dsn}=>{good_dsn}")
    current_pool = evidence.get("current_pool_size")
    good_pool = evidence.get("last_known_good_pool_size")
    if current_pool is not None and good_pool is not None and current_pool != good_pool:
        parts.append(f"pool:{current_pool}=>{good_pool}")
    if not parts:
        # Defensive only -- record_outcome() is only ever called after
        # diagnose() found at least one drifted field, so this shouldn't
        # normally happen.
        parts.append("unknown_drift")
    return "config_drift::" + "|".join(parts)


def load() -> list[dict]:
    if not PATTERNS_PATH.exists():
        return []
    try:
        patterns = json.loads(PATTERNS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    for p in patterns:
        if "consecutive_approvals" not in p:
            # Backfill for entries written before trust-threshold tracking
            # existed. Exact when the pattern has never been denied
            # (consecutive == total approvals); a conservative 0
            # otherwise, since old entries don't record approve/deny
            # order -- better to require it re-earn trust than assume it.
            p["consecutive_approvals"] = p.get("approved_count", 0) if p.get("denied_count", 0) == 0 else 0
        p.setdefault("variants", [p.get("signature")])
        p.setdefault("trust_threshold", None)  # None -> falls back to TRUST_THRESHOLD
        p.setdefault("suspicious", None)  # None, or {"flag": True, "reason": ..., "set_by": ..., "set_at": ...}
    return patterns


def _save(patterns: list[dict]) -> None:
    PATTERNS_PATH.write_text(json.dumps(patterns, indent=2))


def _load_groups() -> dict:
    """raw_or_merged_signature -> canonical_signature. Only entries that
    have actually been merged into something else appear here; an
    unmerged signature is its own canonical and simply won't be a key."""
    if not GROUPS_PATH.exists():
        return {}
    try:
        return json.loads(GROUPS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_groups(groups: dict) -> None:
    GROUPS_PATH.write_text(json.dumps(groups, indent=2))


def canonicalize(signature: str) -> str:
    """Resolve a signature through the merge-group map to whatever it's
    currently grouped under. Follows chained merges (A merged into B,
    later B merged into C -> canonicalize(A) == C) with a hop cap so a
    corrupt/cyclic groups file can't hang this."""
    groups = _load_groups()
    seen = {signature}
    current = signature
    for _ in range(10):
        nxt = groups.get(current)
        if nxt is None or nxt == current or nxt in seen:
            return current
        seen.add(nxt)
        current = nxt
    return current


def find_match(evidence: dict) -> dict | None:
    """Look up whether this exact incident signature -- or anything it's
    been merged with -- has been seen before. Returns the stored pattern
    entry (with approved/denied counts) or None if this is new."""
    sig = canonicalize(_signature(evidence))
    for p in load():
        if p.get("signature") == sig:
            return p
    return None


def get_pattern(signature: str) -> dict | None:
    """Same lookup as find_match(), but keyed directly by a signature
    string rather than derived from raw evidence -- used by
    pattern_insights.py when applying a proposal that names signatures
    explicitly."""
    sig = canonicalize(signature)
    for p in load():
        if p.get("signature") == sig:
            return p
    return None


def is_trusted(pattern: dict) -> bool:
    """A pattern is trusted enough for _run_pre_check() to auto-resolve
    it without ever spending an LLM call only once it has at least its
    trust threshold's worth of consecutive approvals with no intervening
    denial -- zero denials ever (once approved_count reaches the
    threshold), or the pattern was denied at some point but has since
    been cleanly approved that many times in a row. A pattern that has
    ever been denied and hasn't re-earned that many clean approvals since
    stays untrusted, so _run_pre_check() escalates it to a real
    investigation instead of auto-trusting a shaky history.

    A pattern flagged "suspicious" by the mining agent (see
    pattern_insights.py) is never trusted regardless of its approval
    streak, until a human clears the flag -- an automatic circuit
    breaker that only the mining agent (auto-applied) or a human
    (reviewing a queued proposal) can trip, never something that just
    ages off."""
    if (pattern.get("suspicious") or {}).get("flag"):
        return False
    threshold = pattern.get("trust_threshold") or TRUST_THRESHOLD
    return pattern.get("consecutive_approvals", 0) >= threshold


def record_outcome(evidence: dict, root_cause: str, proposed_fix: dict, decision: str, reason: str | None) -> dict:
    """Upsert one outcome. decision is 'allow' or 'deny'. Returns the
    (new-or-updated) pattern entry. Keeps the list bounded to MAX_PATTERNS
    by dropping the least-recently-seen entries once it grows past that --
    this is meant to stay small forever, distilled, not an ever-growing
    incident log.

    Recorded under the CANONICAL signature -- if this raw signature has
    been merged into a group (see merge_signatures()), the outcome
    strengthens (or resets, on a deny) the shared group entry, not a
    fresh one-off."""
    raw_sig = _signature(evidence)
    sig = canonicalize(raw_sig)
    with _lock:
        patterns = load()
        entry = next((p for p in patterns if p.get("signature") == sig), None)
        if entry is None:
            entry = {
                "signature": sig,
                "variants": [sig],
                "root_cause_summary": root_cause,
                "proposed_fix": proposed_fix,
                "approved_count": 0,
                "denied_count": 0,
                "consecutive_approvals": 0,
                "trust_threshold": None,
                "suspicious": None,
                "first_seen": _now(),
            }
            patterns.append(entry)
        if raw_sig not in entry.get("variants", []):
            entry.setdefault("variants", []).append(raw_sig)
        entry["last_seen"] = _now()
        entry["last_decision_reason"] = reason
        if decision == "allow":
            entry["approved_count"] = entry.get("approved_count", 0) + 1
            # Trust builds only on a clean streak -- a denial anywhere
            # in between resets this back to 0 (see the else branch),
            # so this counts approvals since the last denial, not ever.
            entry["consecutive_approvals"] = entry.get("consecutive_approvals", 0) + 1
        else:
            entry["denied_count"] = entry.get("denied_count", 0) + 1
            entry["consecutive_approvals"] = 0

        patterns.sort(key=lambda p: p.get("last_seen", ""), reverse=True)
        if len(patterns) > MAX_PATTERNS:
            patterns = patterns[:MAX_PATTERNS]

        _save(patterns)
        return entry


def merge_signatures(signature_a: str, signature_b: str, canonical: str | None = None) -> dict | None:
    """Merge two signatures into one shared learning history -- the
    mechanism behind a mining-agent "these are really the same fault"
    proposal (see pattern_insights.py). canonical defaults to
    signature_a. Combines approved/denied counts and variant lists from
    both entries for reporting, but ALWAYS resets consecutive_approvals
    to 0 on the merged entry regardless of either side's prior streak --
    a wrong merge should never let a fix skip review it never actually
    earned, so the merged group has to earn fresh trust after merging,
    every time. Returns the merged canonical entry, or None if neither
    signature had ever been recorded (nothing to merge; the group
    mapping is still written so future incidents on either raw
    signature resolve together)."""
    canonical = canonical or signature_a
    other = signature_b if canonical == signature_a else signature_a
    with _lock:
        canonical = canonicalize(canonical)
        other = canonicalize(other)
        if canonical == other:
            return get_pattern(canonical)  # already merged/same signature -- no-op

        groups = _load_groups()
        groups[other] = canonical
        _save_groups(groups)

        patterns = load()
        entry_c = next((p for p in patterns if p.get("signature") == canonical), None)
        entry_o = next((p for p in patterns if p.get("signature") == other), None)

        if entry_o is None:
            _save(patterns)  # nothing numeric to merge; group mapping alone is enough
            return entry_c

        if entry_c is None:
            # Only the non-canonical side had ever been recorded --
            # promote it to live under the canonical signature instead.
            entry_o["signature"] = canonical
            entry_o["consecutive_approvals"] = 0  # still reset -- see docstring
            _save(patterns)
            return entry_o

        entry_c["approved_count"] = entry_c.get("approved_count", 0) + entry_o.get("approved_count", 0)
        entry_c["denied_count"] = entry_c.get("denied_count", 0) + entry_o.get("denied_count", 0)
        entry_c["consecutive_approvals"] = 0
        entry_c["variants"] = sorted(set(entry_c.get("variants", []) + entry_o.get("variants", [])))
        entry_c["first_seen"] = min(entry_c.get("first_seen", ""), entry_o.get("first_seen", "")) or entry_c.get("first_seen")
        entry_c["last_seen"] = max(entry_c.get("last_seen", ""), entry_o.get("last_seen", ""))
        entry_c["merged_from"] = sorted(set(entry_c.get("merged_from", []) + [other]))

        patterns = [p for p in patterns if p.get("signature") != other]
        _save(patterns)
        return entry_c


def set_trust_threshold(signature: str, threshold: int, source: str) -> dict | None:
    """Set (or clear, if threshold is None) a per-pattern trust-threshold
    override -- how a mining agent's threshold-tuning proposal takes
    effect once applied (auto-applied if it raises the bar, human-
    approved if it lowers it -- see pattern_insights.py). Returns the
    updated entry, or None if this signature has never been recorded."""
    sig = canonicalize(signature)
    with _lock:
        patterns = load()
        entry = next((p for p in patterns if p.get("signature") == sig), None)
        if entry is None:
            return None
        entry["trust_threshold"] = threshold
        entry["threshold_set_by"] = source
        entry["threshold_set_at"] = _now()
        _save(patterns)
        return entry


def set_suspicious(signature: str, flag: bool, reason: str, source: str) -> dict | None:
    """Set or clear the suspicious flag on a pattern -- see is_trusted().
    Returns the updated entry, or None if this signature has never been
    recorded."""
    sig = canonicalize(signature)
    with _lock:
        patterns = load()
        entry = next((p for p in patterns if p.get("signature") == sig), None)
        if entry is None:
            return None
        entry["suspicious"] = (
            {"flag": True, "reason": reason, "set_by": source, "set_at": _now()} if flag else None
        )
        _save(patterns)
        return entry
