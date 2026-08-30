"""
demo-app: a fake production web service used to stage a realistic incident.

It simulates:
  - normal request traffic with a small baseline error rate
  - a "bad deployment" that corrupts the DB connection config, spiking the
    error rate to ~90%
  - the on-disk state (db_config.json) that a rollback restores

Endpoints an operator/telemetry system would use:
  GET  /health            liveness
  GET  /metrics           current error rate + request counts (what the
                           telemetry watcher polls)
  POST /chaos/inject       simulate a bad deployment that corrupts the DB pool
  POST /chaos/reset        hard reset back to healthy (bypasses the agent --
                           only for local testing, never used by the demo flow)

Endpoints the sre-tools MCP server calls on the agent's behalf:
  GET  /internal/db-status  DB pool config + health, for the diagnostic script
  POST /internal/rollback   restore the last-known-good DB config (this is
                             what `apply_system_change` actually executes,
                             AFTER a human has approved it)

Logs every simulated request to app.log, including a realistic stack trace
on failure and a clearly-flagged deployment marker when chaos is injected --
this is what the agent's `tail_log` MCP tool call reads.
"""

import json
import logging
import os
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)
LOG_PATH = BASE_DIR / "app.log"
DB_CONFIG_PATH = STATE_DIR / "db_config.json"
DB_CONFIG_LAST_GOOD_PATH = STATE_DIR / "db_config.last_good.json"

GOOD_DSN = "postgresql://app:S3cure-Pass@db-primary.internal:5432/app_prod?pool=20"
GOOD_POOL_SIZE = 20
BAD_POOL_SIZE = 2               # simulates a misconfigured rollout that shrinks the pool
BASELINE_ERROR_RATE = 0.02      # healthy traffic still has some noise
CORRUPTED_ERROR_RATE = 0.90     # what judges will see spike past the 85% threshold
WINDOW_SECONDS = 20              # rolling window used to compute /metrics error_rate
REQUEST_INTERVAL = 0.15          # simulated request every N seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("demo-app")

app = FastAPI(title="demo-app (simulated production service)")

_lock = threading.Lock()
_events: deque = deque()  # (timestamp, ok: bool)
_state = {
    "corrupted": False,
    "corrupted_since": None,
    "deploy_id": None,
    "kind": None,  # "dsn_typo" | "pool_shrink" | "both" -- which chaos was injected
}


def _write_config(dsn: str, pool_size: int = 20):
    DB_CONFIG_PATH.write_text(json.dumps({"dsn": dsn, "pool_size": pool_size}, indent=2))


def _read_config() -> dict:
    if not DB_CONFIG_PATH.exists():
        _write_config(GOOD_DSN)
    return json.loads(DB_CONFIG_PATH.read_text())


def _save_last_good():
    DB_CONFIG_LAST_GOOD_PATH.write_text(DB_CONFIG_PATH.read_text())


if not DB_CONFIG_PATH.exists():
    _write_config(GOOD_DSN)
if not DB_CONFIG_LAST_GOOD_PATH.exists():
    _save_last_good()


def _simulate_requests():
    """Background traffic generator. Runs forever in its own thread."""
    n = 0
    while True:
        n += 1
        error_rate = CORRUPTED_ERROR_RATE if _state["corrupted"] else BASELINE_ERROR_RATE
        ok = random.random() > error_rate
        now = time.time()
        with _lock:
            _events.append((now, ok))
            cutoff = now - WINDOW_SECONDS
            while _events and _events[0][0] < cutoff:
                _events.popleft()

        if ok:
            log.info("GET /api/orders/%d -> 200 OK (12ms)", n)
        else:
            cfg = _read_config()
            kind = _state.get("kind")
            if kind == "pool_shrink":
                log.error(
                    "GET /api/orders/%d -> 500 Internal Server Error\n"
                    "Traceback (most recent call last):\n"
                    '  File "db/pool.py", line 41, in acquire\n'
                    "    conn = pool.get(timeout=2)\n"
                    "sqlalchemy.exc.TimeoutError: QueuePool limit of size %d overflow 0 "
                    "reached, connection timed out\n"
                    "    pool_size in use: %d (deploy=%s)",
                    n, cfg.get("pool_size"), cfg.get("pool_size"), _state.get("deploy_id"),
                )
            elif kind == "both":
                log.error(
                    "GET /api/orders/%d -> 500 Internal Server Error\n"
                    "Traceback (most recent call last):\n"
                    '  File "db/pool.py", line 88, in acquire\n'
                    "    conn = connect(dsn=DB_DSN)\n"
                    "sqlalchemy.exc.OperationalError: could not connect to server: "
                    "Connection refused\n"
                    "    DSN in use: %s\n"
                    "    pool_size in use: %d (pool exhausted after 3 retries, deploy=%s)",
                    n, cfg.get("dsn"), cfg.get("pool_size"), _state.get("deploy_id"),
                )
            else:
                log.error(
                    "GET /api/orders/%d -> 500 Internal Server Error\n"
                    "Traceback (most recent call last):\n"
                    '  File "db/pool.py", line 88, in acquire\n'
                    "    conn = connect(dsn=DB_DSN)\n"
                    "sqlalchemy.exc.OperationalError: could not connect to server: "
                    "Connection refused\n"
                    "    DSN in use: %s\n"
                    "    (pool exhausted after 3 retries, deploy=%s)",
                    n, cfg.get("dsn"), _state.get("deploy_id"),
                )
        time.sleep(REQUEST_INTERVAL)


threading.Thread(target=_simulate_requests, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "unhealthy" if _state["corrupted"] else "healthy"}


@app.get("/metrics")
def metrics():
    with _lock:
        total = len(_events)
        failures = sum(1 for _, ok in _events if not ok)
    error_rate_pct = round((failures / total) * 100, 1) if total else 0.0
    return {
        "window_seconds": WINDOW_SECONDS,
        "total_requests": total,
        "failed_requests": failures,
        "error_rate_pct": error_rate_pct,
        "corrupted": _state["corrupted"],
        "deploy_id": _state["deploy_id"],
    }


class ChaosRequest(BaseModel):
    kind: str = "dsn_typo"  # "dsn_typo" | "pool_shrink" | "both"


_BAD_DSN = "postgresql://app:S3cure-Pass@db-primry.internal:5432/app_prod?pool=20"  # typo'd host
_CHAOS_DESCRIPTIONS = {
    "dsn_typo": "updated DB_DSN config (db-primary.internal -> db-primry.internal). "
                "Connection pool will fail to resolve host.",
    "pool_shrink": f"shrank the DB connection pool_size ({GOOD_POOL_SIZE} -> {BAD_POOL_SIZE}). "
                   "Connection pool will exhaust under normal load.",
    "both": "updated DB_DSN config (db-primary.internal -> db-primry.internal) AND shrank "
            f"pool_size ({GOOD_POOL_SIZE} -> {BAD_POOL_SIZE}).",
}


@app.post("/chaos/inject")
def chaos_inject(req: ChaosRequest = ChaosRequest()):
    """Simulate a bad deployment. kind picks which config field(s) drift from
    known-good -- dsn_typo (default, matches the original demo), pool_shrink
    (a different, independent fault the same rollback tool still fixes), or
    both at once (a compound failure)."""
    kind = req.kind if req.kind in ("dsn_typo", "pool_shrink", "both") else "dsn_typo"
    if _state["corrupted"]:
        cfg = _read_config()
        return {
            "injected": False, "deploy_id": _state["deploy_id"], "kind": _state.get("kind"),
            "dsn": cfg["dsn"], "pool_size": cfg["pool_size"],
        }

    _save_last_good()
    deploy_id = "deploy-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    new_dsn = _BAD_DSN if kind in ("dsn_typo", "both") else GOOD_DSN
    new_pool = BAD_POOL_SIZE if kind in ("pool_shrink", "both") else GOOD_POOL_SIZE
    _write_config(new_dsn, pool_size=new_pool)
    _state["corrupted"] = True
    _state["corrupted_since"] = time.time()
    _state["deploy_id"] = deploy_id
    _state["kind"] = kind
    log.error("DEPLOYMENT %s rolled out: %s", deploy_id, _CHAOS_DESCRIPTIONS[kind])
    return {"injected": True, "deploy_id": deploy_id, "kind": kind, "dsn": new_dsn, "pool_size": new_pool}


@app.post("/chaos/reset")
def chaos_reset():
    """Local-testing-only hard reset. The real demo flow uses /internal/rollback
    via the agent's gated apply_system_change tool instead."""
    _write_config(GOOD_DSN, pool_size=GOOD_POOL_SIZE)
    _state["corrupted"] = False
    _state["corrupted_since"] = None
    _state["deploy_id"] = None
    _state["kind"] = None
    log.info("Manual /chaos/reset called -- DB config restored to known-good.")
    return {"reset": True}


@app.get("/internal/log")
def internal_log(lines: int = 100):
    """Tail app.log over HTTP. This is what the sre-tools MCP server's
    tail_log calls -- used instead of direct filesystem access so demo-app
    and the MCP server can run in separate containers (or separate
    machines) without a shared volume, and so this looks like the log APIs
    most real services actually expose."""
    if not LOG_PATH.exists():
        return {"lines": []}
    with LOG_PATH.open("r", errors="replace") as f:
        all_lines = f.readlines()
    return {"lines": [ln.rstrip("\n") for ln in all_lines[-lines:]]}


@app.get("/internal/db-status")
def db_status():
    """Used by the sre-tools MCP server's check_db_health tool (called by the
    agent's own diagnostic script in the sandbox, via Code Mode)."""
    cfg = _read_config()
    last_good = json.loads(DB_CONFIG_LAST_GOOD_PATH.read_text())
    return {
        "current_dsn": cfg["dsn"],
        "pool_size": cfg["pool_size"],
        "last_known_good_dsn": last_good["dsn"],
        "last_known_good_pool_size": last_good.get("pool_size", GOOD_POOL_SIZE),
        "healthy": not _state["corrupted"],
        "corrupted_since": _state["corrupted_since"],
        "deploy_id": _state["deploy_id"],
    }


class RollbackRequest(BaseModel):
    dsn: str | None = None  # if omitted, restores from last-known-good snapshot


@app.post("/internal/rollback")
def rollback(req: RollbackRequest):
    """This is what apply_system_change actually executes -- ONLY reachable
    after TrueForge's approval gate has released the paused tool call."""
    last_good = json.loads(DB_CONFIG_LAST_GOOD_PATH.read_text())
    dsn = req.dsn or last_good["dsn"]
    _write_config(dsn, pool_size=last_good.get("pool_size", 20))
    was_corrupted = _state["corrupted"]
    _state["corrupted"] = False
    _state["corrupted_since"] = None
    log.info("ROLLBACK applied: DB_DSN restored to %s (deploy=%s)", dsn, _state["deploy_id"])
    return {"rolled_back": was_corrupted, "dsn": dsn}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DEMO_APP_PORT", 8081)))
