"""
dashboard/app.py -- "mission control" for judges.

Deliberately shows everything: which MCP tools exist and which one is
currently being invoked, the full raw TrueForge event stream (model
reasoning, tool calls, sandbox activity, approvals), the live error-rate
chart, and the human approval gate itself. Nothing about the harness's
operation is hidden behind a canned "agent is thinking..." spinner.

Run via `streamlit run app.py` (or scripts/run_all.sh which sets env vars).
"""

import os
import time
from datetime import datetime

import httpx
import pandas as pd
import streamlit as st

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8083")
DEMO_APP_URL = os.environ.get("DEMO_APP_URL", "http://localhost:8081")
TRUEFORGE_BASE_URL = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8082/mcp")

st.set_page_config(page_title="Incident Responder — TrueForge", page_icon="🚨", layout="wide")

# --- known tool catalogue (static description of what's registered; the
# "currently active" one is highlighted live from the real event stream) ---
MCP_TOOLS = [
    {"name": "tail_log", "kind": "read-only", "desc": "tail -n N demo-app/app.log"},
    {"name": "check_db_health", "kind": "read-only", "desc": "DB pool config + health (called from the agent's own sandbox script)"},
    {"name": "apply_system_change", "kind": "GATED — destructive", "desc": "roll back the DB DSN (requires human approval)"},
]

STATUS_COLORS = {
    "idle": ("#3b3f46", "IDLE — waiting for an incident"),
    "investigating": ("#c0392b", "INCIDENT — agent investigating"),
    "intervention_required": ("#e2a400", "⚠ INTERVENTION REQUIRED — awaiting human approval"),
    "remediating": ("#2980b9", "REMEDIATING — rollback approved, agent applying fix"),
    "remediation_denied": ("#7f8c8d", "Rollback DENIED by human"),
    "resolved": ("#27ae60", "HEALTHY — resolved"),
}

st.markdown(
    """
    <style>
    @keyframes blink { 0% {opacity: 1;} 50% {opacity: 0.45;} 100% {opacity: 1;} }
    .blink { animation: blink 1s linear infinite; }
    .status-banner {
        padding: 18px 24px; border-radius: 10px; color: white;
        font-size: 1.4rem; font-weight: 700; margin-bottom: 1rem;
    }
    .tool-card {
        border: 1px solid #444; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px;
    }
    .tool-card.active { border-color: #e2a400; box-shadow: 0 0 8px #e2a400; }
    .event-row { font-family: monospace; font-size: 0.82rem; padding: 4px 0; border-bottom: 1px solid #2a2a2a; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _get(url: str, default=None):
    try:
        r = httpx.get(url, timeout=4)
        r.raise_for_status()
        return r.json()
    except Exception:
        return default


def _post(url: str, json=None):
    try:
        r = httpx.post(url, json=json or {}, timeout=8)
        return r.status_code, (r.json() if r.content else {})
    except Exception as exc:
        return None, {"error": str(exc)}


state = _get(f"{BACKEND_URL}/state", default={}) or {}
metrics = _get(f"{DEMO_APP_URL}/metrics", default={}) or {}

status = state.get("status", "idle")
color, label = STATUS_COLORS.get(status, ("#3b3f46", status))
is_healthy_signal = metrics.get("error_rate_pct", 0) < 5 and not metrics.get("corrupted")

# --- header / controls -------------------------------------------------
top_l, top_r = st.columns([3, 1])
with top_l:
    st.title("🚨 Incident Responder")
    st.caption(
        f"Powered by [TrueForge]({TRUEFORGE_BASE_URL}) — agent `incident-responder` · "
        f"skill `sre-playbook` · MCP server `sre-tools`"
    )
with top_r:
    c1, c2 = st.columns(2)
    if c1.button("💥 Inject Failure", use_container_width=True):
        _post(f"{DEMO_APP_URL}/chaos/inject")
        st.rerun()
    if c2.button("↺ Reset Demo", use_container_width=True):
        _post(f"{BACKEND_URL}/reset")
        _post(f"{DEMO_APP_URL}/chaos/reset")
        st.rerun()

blink_class = "blink" if status == "intervention_required" else ""
st.markdown(
    f'<div class="status-banner {blink_class}" style="background:{color};">{label}</div>',
    unsafe_allow_html=True,
)

if state.get("error"):
    st.error(f"Backend/harness error: {state['error']}")

# --- metrics row ---------------------------------------------------------
m1, m2, m3, m4 = st.columns(4)
m1.metric("Error rate", f"{metrics.get('error_rate_pct', 0)}%")
m2.metric("Requests (window)", metrics.get("total_requests", 0))
m3.metric("Deploy", metrics.get("deploy_id") or "—")
m4.metric("TrueForge session", (state.get("session_id") or "—")[:12])

# error-rate history kept client-side across reruns via session_state
if "history" not in st.session_state:
    st.session_state.history = []
st.session_state.history.append(
    {"t": datetime.now().strftime("%H:%M:%S"), "error_rate_pct": metrics.get("error_rate_pct", 0)}
)
st.session_state.history = st.session_state.history[-120:]
hist_df = pd.DataFrame(st.session_state.history).set_index("t")
st.line_chart(hist_df, height=180)

st.divider()

left, right = st.columns([2, 1])

# --- intervention card ---------------------------------------------------
with left:
    pending = state.get("pending_approval")
    if pending:
        with st.container(border=True):
            st.subheader("⚠️ Human approval required")
            st.write(f"**Tool:** `{pending.get('tool_name')}`  ·  **thread:** `{pending.get('thread_id')}`")
            st.json(pending.get("arguments", {}))
            reason = st.text_input("Note (optional)", key="approve_reason")
            c1, c2 = st.columns(2)
            if c1.button("✅ Approve & Execute Rollback", type="primary", use_container_width=True):
                _post(f"{BACKEND_URL}/approve", {"reason": reason or None})
                st.rerun()
            if c2.button("⛔ Deny", use_container_width=True):
                _post(f"{BACKEND_URL}/deny", {"reason": reason or None})
                st.rerun()

    if state.get("final_report"):
        with st.container(border=True):
            st.subheader("📋 Incident report")
            st.markdown(state["final_report"])

    st.subheader("Live event stream (raw, from TrueForge)")
    events = state.get("events", [])
    if not events:
        st.caption("No session yet — click **Inject Failure** or wait for the telemetry watcher to fire.")
    for e in reversed(events[-80:]):
        etype = e.get("type", "?")
        data = e.get("data", {})
        icon = {
            "model.message": "🧠",
            "tool.approval_required": "⏸️",
            "tool.response": "🔧",
            "tool.response_required": "❓",
            "sandbox.created": "📦",
            "turn.created": "▶️",
            "turn.done": "🏁",
            "human.decision": "🧑‍⚖️",
        }.get(etype, "•")
        with st.expander(f"{icon} [{e.get('seq')}] {etype} — {e.get('at', '')[11:19]}", expanded=(etype in ("tool.approval_required", "human.decision"))):
            st.json(data)

# --- MCP servers / tools panel -------------------------------------------
with right:
    st.subheader("MCP servers")
    st.caption(f"`sre-tools` @ {MCP_SERVER_URL}")

    active_tool = None
    for e in reversed(state.get("events", [])):
        d = e.get("data", {})
        if e.get("type") == "model.message":
            calls = d.get("tool_calls") or []
            if calls:
                c0 = calls[0]
                active_tool = (c0.get("function") or {}).get("name") or c0.get("name")
                break
        if e.get("type") == "tool.approval_required":
            active_tool = "apply_system_change"
            break

    for tool in MCP_TOOLS:
        active = tool["name"] == active_tool
        cls = "tool-card active" if active else "tool-card"
        badge = " 🔴 ACTIVE" if active else ""
        st.markdown(
            f'<div class="{cls}"><b>{tool["name"]}</b>{badge}<br>'
            f'<span style="opacity:.7">{tool["kind"]} — {tool["desc"]}</span></div>',
            unsafe_allow_html=True,
        )

    st.subheader("Filesystem / logs")
    st.caption("Agent tails demo-app's log via `tail_log` -> GET /internal/log (HTTP, not a shared filesystem) — shown live below.")
    tail = _get(f"{DEMO_APP_URL}/metrics")
    with st.expander("Show demo-app metrics JSON"):
        st.json(metrics)

    st.subheader("Skill")
    st.caption("`sre-playbook` (SKILL.md) — SRE triage → sandbox diagnostic → gated rollback → verify → report")

# --- auto refresh ----------------------------------------------------------
auto = st.toggle("Live auto-refresh", value=True)
interval = 1.2 if status in ("investigating", "intervention_required", "remediating") else 3.0
if auto:
    time.sleep(interval)
    st.rerun()
