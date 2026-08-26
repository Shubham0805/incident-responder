"""
telemetry/watcher.py -- the "mock telemetry system" from the spec.

Polls demo-app's /metrics on a short interval. When error_rate_pct crosses
ERROR_RATE_THRESHOLD it fires an HTTP POST webhook to the backend orchestrator,
exactly like a real monitoring system (Datadog/Prometheus Alertmanager/etc.)
would call an incident-response webhook.

Re-arms automatically once the service recovers, so you can run the chaos ->
approve -> recover loop more than once in a single demo session.
"""

import os
import time
from datetime import datetime, timezone

import httpx

DEMO_APP_URL = os.environ.get("DEMO_APP_URL", "http://localhost:8081")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8083")
THRESHOLD = float(os.environ.get("ERROR_RATE_THRESHOLD", "85"))
POLL_SECONDS = float(os.environ.get("WATCHER_POLL_SECONDS", "2"))

_alerted = False


def poll_once(client: httpx.Client) -> None:
    global _alerted
    try:
        metrics = client.get(f"{DEMO_APP_URL}/metrics", timeout=5).json()
    except httpx.HTTPError as exc:
        print(f"[watcher] could not reach demo-app: {exc}")
        return

    error_rate = metrics.get("error_rate_pct", 0.0)

    if error_rate >= THRESHOLD and not _alerted:
        _alerted = True
        payload = {
            "alert": "HighErrorRate",
            "error_rate_pct": error_rate,
            "deploy_id": metrics.get("deploy_id"),
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "source": "mock-telemetry",
        }
        print(f"[watcher] error_rate={error_rate}% >= {THRESHOLD}% -> firing webhook: {payload}")
        try:
            resp = client.post(f"{BACKEND_URL}/webhook/incident", json=payload, timeout=10)
            print(f"[watcher] backend responded {resp.status_code}: {resp.text}")
        except httpx.HTTPError as exc:
            print(f"[watcher] failed to reach backend: {exc}")
            _alerted = False  # allow retry next tick

    elif error_rate < THRESHOLD and _alerted:
        print(f"[watcher] error_rate={error_rate}% back under threshold -- re-arming watcher")
        _alerted = False


def main():
    print(f"[watcher] watching {DEMO_APP_URL}/metrics every {POLL_SECONDS}s, "
          f"threshold={THRESHOLD}%, backend={BACKEND_URL}")
    with httpx.Client() as client:
        while True:
            poll_once(client)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
