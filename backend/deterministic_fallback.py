"""
deterministic_fallback.py -- a NON-LLM fallback investigation for demo-app,
used only when the real TrueForge/LLM-driven turn ends without ever
reaching a remediation step (rate-limited, misconfigured model, harness
error -- doesn't matter which).

This is deliberately dumb: it re-implements the exact same checks the
sre-playbook skill asks the LLM to do (read the log, check DB health,
compare the live config against last-known-good) as plain Python
string/dict comparisons, calling demo-app's own internal HTTP endpoints
directly -- the same endpoints sre_tools_server.py's MCP tools wrap, just
without MCP or an LLM in between.

Recognizes two independent fault dimensions on the same DB config object
-- current_dsn vs last_known_good_dsn, and pool_size vs
last_known_good_pool_size -- and reports whichever one(s) actually
drifted (including both at once, a compound failure). The pool-size
comparison degrades gracefully (silently skipped, not a hard failure) if
demo-app's response doesn't include those fields, so this stays robust
against an older/partial demo-app response shape. If the log/DB-status
shape doesn't match what we expect, or nothing recognizable drifted,
this reports "no clear root cause" rather than guessing -- a wrong
deterministic diagnosis is worse than an honest "I don't know,"
especially since a human still approves before anything is actually
applied.

Existing purely as a resilience net: prefer the real agent whenever it
works. This only runs after it has already failed.
"""

from __future__ import annotations

import os

import httpx

DEMO_APP_URL = os.environ.get("DEMO_APP_URL", "http://localhost:8081")


def _tail_log(lines: int = 100) -> list[str]:
    with httpx.Client(timeout=5) as client:
        resp = client.get(f"{DEMO_APP_URL}/internal/log", params={"lines": lines})
        resp.raise_for_status()
        return resp.json().get("lines", [])


def _check_db_health() -> dict:
    with httpx.Client(timeout=5) as client:
        resp = client.get(f"{DEMO_APP_URL}/internal/db-status")
        resp.raise_for_status()
        return resp.json()


def diagnose() -> dict:
    """Returns a dict. If it found a clear root cause:
        {"root_cause": str, "evidence": {...}, "proposed_dsn": str, "reason": str}
    If not:
        {"root_cause": None, "detail": str, "evidence": {...}}
    Never raises for a normal "couldn't reach demo-app" case -- that's
    reported as detail text, not an exception, since the caller (backend's
    fallback thread) should degrade gracefully either way.
    """
    evidence: dict = {}

    try:
        log_lines = _tail_log(100)
    except Exception as exc:  # noqa: BLE001
        return {"root_cause": None, "detail": f"Could not reach demo-app's log endpoint: {exc}", "evidence": evidence}

    error_lines = [l for l in log_lines if "ERROR" in l]
    deploy_lines = [l for l in log_lines if "DEPLOYMENT" in l.upper()]
    evidence["error_line_count"] = len(error_lines)
    evidence["recent_errors"] = error_lines[-5:]
    evidence["recent_deployment_markers"] = deploy_lines[-3:]

    try:
        db_status = _check_db_health()
    except Exception as exc:  # noqa: BLE001
        return {"root_cause": None, "detail": f"Could not reach demo-app's DB-status endpoint: {exc}", "evidence": evidence}

    current_dsn = db_status.get("current_dsn")
    last_known_good_dsn = db_status.get("last_known_good_dsn")
    healthy = db_status.get("healthy")
    evidence["current_dsn"] = current_dsn
    evidence["last_known_good_dsn"] = last_known_good_dsn
    evidence["reported_healthy"] = healthy

    if not current_dsn or not last_known_good_dsn:
        return {
            "root_cause": None,
            "detail": "DB status response didn't include both current_dsn and last_known_good_dsn -- "
                      "can't compare, so not guessing.",
            "evidence": evidence,
        }

    # pool_size comparison is optional -- only made if demo-app's response
    # actually includes both fields, so an older/partial response shape
    # degrades to "just compare the DSN" rather than raising.
    current_pool_size = db_status.get("pool_size")
    last_known_good_pool_size = db_status.get("last_known_good_pool_size")
    evidence["current_pool_size"] = current_pool_size
    evidence["last_known_good_pool_size"] = last_known_good_pool_size
    pool_comparable = current_pool_size is not None and last_known_good_pool_size is not None

    dsn_drifted = current_dsn != last_known_good_dsn
    pool_drifted = pool_comparable and current_pool_size != last_known_good_pool_size

    if not dsn_drifted and not pool_drifted:
        if healthy:
            return {
                "root_cause": None,
                "detail": "Current config matches last-known-good (DSN"
                          + (" and pool_size" if pool_comparable else "")
                          + ") and the service reports healthy -- no config-drift root cause found. "
                          "(If errors are still occurring, this fallback only recognizes drift on this "
                          "one config object; it won't catch anything else.)",
                "evidence": evidence,
            }
        return {
            "root_cause": None,
            "detail": "Config matches last-known-good but the service still reports unhealthy -- "
                      "not a config-drift scenario this fallback recognizes.",
            "evidence": evidence,
        }

    # Something drifted -- describe exactly what, dsn and/or pool_size, so
    # the human sees the real root cause rather than a generic label.
    drift_parts = []
    if dsn_drifted:
        drift_parts.append(f"current_dsn ({current_dsn!r}) does not match last_known_good_dsn ({last_known_good_dsn!r})")
    if pool_drifted:
        drift_parts.append(
            f"pool_size ({current_pool_size!r}) does not match last_known_good_pool_size ({last_known_good_pool_size!r})"
        )
    return {
        "root_cause": (
            " AND ".join(drift_parts)
            + f". {len(error_lines)} ERROR line(s) found in the last {len(log_lines)} log lines"
            + (f", with a deployment marker nearby: {deploy_lines[-1]!r}" if deploy_lines else "")
            + "."
        ),
        "evidence": evidence,
        "proposed_dsn": last_known_good_dsn,
        "reason": (
            "Deterministic fallback (no LLM): "
            + " and ".join(drift_parts)
            + "; rolling back to last-known-good config."
        ),
    }


def apply_fix(dsn: str, reason: str) -> dict:
    """Same call apply_system_change makes -- POST demo-app's rollback
    endpoint directly. Still only ever invoked after a human approves, same
    as the LLM path's gated tool call."""
    with httpx.Client(timeout=10) as client:
        resp = client.post(f"{DEMO_APP_URL}/internal/rollback", json={"dsn": dsn})
        resp.raise_for_status()
        result = resp.json()
    result["reason"] = reason
    return result
