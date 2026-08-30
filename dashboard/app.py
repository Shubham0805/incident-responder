"""
dashboard/app.py -- "Glass-Box" mission control for judges.

Two-column, developer-facing layout: instead of hiding the agent behind a
chat window, this shows every layer of what TrueForge is actually doing --
a Status Orb with a live sub-stage, a terminal-styled stream reconstructed
from the raw SSE events (agent reasoning, tool calls, sandbox activity), the
live error-rate telemetry, a before/after config diff, and the human
approval gate itself, styled as a hard visual interrupt when it fires.
Nothing about the harness's operation is hidden behind a spinner -- the full
raw event log is still available (bottom of the page) for anyone who wants
the ground truth underneath the human-readable version.

Run via `streamlit run app.py` (or scripts/run_all.sh which sets env vars).
"""

import html
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

st.set_page_config(page_title="Groundhog — TrueForge", page_icon="🐿️", layout="wide")

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
    "error": ("#8e44ad", "⚠ TURN ENDED WITHOUT REMEDIATION — see error below"),
    "deterministic_diagnosis": ("#16a085", "🧮 LLM unavailable — running rule-based fallback (no LLM)"),
}

TERMINAL_KIND_STYLE = {
    "agent": ("line-agent", "🧠"),
    "tool": ("line-tool", "🔧"),
    "system": ("line-system", "📦"),
    "human": ("line-human", "🧑‍⚖️"),
}

st.markdown(
    """
    <style>
    @keyframes blink { 0% {opacity: 1;} 50% {opacity: 0.45;} 100% {opacity: 1;} }
    @keyframes pulse-amber {
        0% { box-shadow: 0 0 6px 0 rgba(226,164,0,0.5); }
        50% { box-shadow: 0 0 22px 6px rgba(226,164,0,0.85); }
        100% { box-shadow: 0 0 6px 0 rgba(226,164,0,0.5); }
    }
    .blink { animation: blink 1s linear infinite; }
    .status-banner {
        padding: 18px 24px; border-radius: 10px; color: white;
        font-size: 1.4rem; font-weight: 700; margin-bottom: 0.2rem;
    }
    .stage-line {
        font-family: 'Courier New', monospace; font-size: 0.95rem; opacity: 0.85;
        margin-bottom: 1rem; padding-left: 4px;
    }
    .tool-card {
        border: 1px solid #444; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px;
    }
    .tool-card.active { border-color: #e2a400; box-shadow: 0 0 8px #e2a400; }
    .event-row { font-family: monospace; font-size: 0.82rem; padding: 4px 0; border-bottom: 1px solid #2a2a2a; }

    .terminal {
        background: #0b0f0b; color: #33ff66; font-family: 'Courier New', monospace;
        font-size: 0.8rem; padding: 14px; border-radius: 8px; height: 420px;
        overflow-y: auto; white-space: pre-wrap; word-break: break-word;
        border: 1px solid #1c3d1c;
    }
    .terminal .line { margin-bottom: 6px; }
    .terminal .line-agent { color: #7fd1ff; }
    .terminal .line-tool { color: #33ff66; }
    .terminal .line-system { color: #e2a400; }
    .terminal .line-human { color: #ffffff; font-weight: bold; }
    .terminal .ts { opacity: 0.55; }

    .intercept-gate {
        padding: 16px 22px; border-radius: 10px; background: #3a2900;
        border: 2px solid #e2a400; color: #ffcf4d; font-weight: 700;
        font-size: 1.1rem; margin-bottom: 0.8rem;
        animation: pulse-amber 1.4s ease-in-out infinite;
    }

    .diff-box {
        font-family: 'Courier New', monospace; font-size: 0.82rem; padding: 12px;
        border-radius: 6px; white-space: pre-wrap; word-break: break-all;
        min-height: 70px;
    }
    .diff-before { background: #3a1414; border: 1px solid #7a2020; color: #ff9d9d; }
    .diff-after { background: #123a18; border: 1px solid #2b7a3a; color: #9dffab; }
    .diff-label { font-family: monospace; font-size: 0.75rem; opacity: 0.7; margin-bottom: 4px; }
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


def _terminal_html(entries: list[dict]) -> str:
    """Render backend/main.py's best-effort `terminal_log` as a dark,
    monospaced terminal block. See _terminal_entry() in backend/main.py for
    why some lines fall back to raw JSON -- not every TrueForge event field
    name was verbatim-confirmed against a live instance."""
    if not entries:
        return '<div class="terminal">(no sandbox/tool activity yet — inject a failure to start)</div>'
    lines = []
    for e in entries[-80:]:
        cls, icon = TERMINAL_KIND_STYLE.get(e.get("kind"), ("", "•"))
        ts = (e.get("at") or "")[11:19]
        text = html.escape(e.get("text", "")).replace("\n", "<br>")
        lines.append(f'<div class="line {cls}"><span class="ts">[{ts}]</span> {icon} {text}</div>')
    return '<div class="terminal">' + "".join(lines) + '</div>'


state = _get(f"{BACKEND_URL}/state", default={}) or {}
metrics = _get(f"{DEMO_APP_URL}/metrics", default={}) or {}
db_status = _get(f"{DEMO_APP_URL}/internal/db-status", default={}) or {}

status = state.get("status", "idle")
stage = state.get("stage") or ""
color, label = STATUS_COLORS.get(status, ("#3b3f46", status))
is_intervention = status == "intervention_required"

# --- header / controls -------------------------------------------------
top_l, top_r = st.columns([3, 1])
with top_l:
    st.title("🐿️ Groundhog")
    st.caption("Stop reliving the same incident.")
    st.caption(
        f"Powered by [TrueForge]({TRUEFORGE_BASE_URL}) — agent `incident-responder` · "
        f"skill `sre-playbook` · MCP server `sre-tools`"
    )
with top_r:
    FAILURE_KINDS = {
        "DSN typo (bad host)": "dsn_typo",
        "Pool size shrink": "pool_shrink",
        "Both at once": "both",
    }
    kind_label = st.selectbox(
        "Failure to inject", list(FAILURE_KINDS.keys()), label_visibility="collapsed",
    )
    c1, c2 = st.columns(2)
    if c1.button("💥 Inject Failure", use_container_width=True):
        _post(f"{DEMO_APP_URL}/chaos/inject", json={"kind": FAILURE_KINDS[kind_label]})
        st.rerun()
    if c2.button("↺ Reset Demo", use_container_width=True):
        _post(f"{BACKEND_URL}/reset")
        _post(f"{DEMO_APP_URL}/chaos/reset")
        st.rerun()

# --- Status Orb: coarse status (color/urgency) + live sub-stage --------
blink_class = "blink" if is_intervention else ""
st.markdown(
    f'<div class="status-banner {blink_class}" style="background:{color};">{label}</div>',
    unsafe_allow_html=True,
)
if stage:
    st.markdown(f'<div class="stage-line">→ {stage}</div>', unsafe_allow_html=True)

if state.get("error"):
    st.error(f"Backend/harness error: {state['error']}")

# --- metrics row ---------------------------------------------------------
m1, m2, m3, m4 = st.columns(4)
m1.metric("Error rate", f"{metrics.get('error_rate_pct', 0)}%")
m2.metric("Requests (window)", metrics.get("total_requests", 0))
m3.metric("Deploy", metrics.get("deploy_id") or "—")
m4.metric("TrueForge session", (state.get("session_id") or "—")[:12])

st.divider()

left, right = st.columns([3, 2])

# =========================================================================
# LEFT COLUMN -- Infrastructure & Sandbox Monitor
# =========================================================================
with left:
    st.subheader("🖥️ Live sandbox / agent terminal")
    st.caption(
        "Reconstructed from TrueForge's raw event stream — agent reasoning, "
        "tool calls (tail_log / check_db_health / apply_system_change), and "
        "sandbox lifecycle, in the order they actually happened."
    )
    st.markdown(_terminal_html(state.get("terminal_log", [])), unsafe_allow_html=True)

    st.subheader("📈 Live telemetry")
    if "history" not in st.session_state:
        st.session_state.history = []
    st.session_state.history.append(
        {
            "t": datetime.now().strftime("%H:%M:%S"),
            "error_rate_pct": metrics.get("error_rate_pct", 0),
            "alert_threshold": 85,
        }
    )
    st.session_state.history = st.session_state.history[-120:]
    hist_df = pd.DataFrame(st.session_state.history).set_index("t")
    st.line_chart(hist_df, height=200, color=["#e74c3c", "#7f8c8d"])
    st.caption("Red line: live error rate. Grey line: the 85% threshold that fires the telemetry webhook.")

# =========================================================================
# RIGHT COLUMN -- TrueForge Interaction Hub
# =========================================================================
with right:
    st.subheader("🔀 Config: before vs. proposed")
    pending = state.get("pending_approval")
    proposed_dsn = (pending or {}).get("arguments", {}).get("dsn")
    before_dsn = db_status.get("current_dsn", "—")
    after_dsn = proposed_dsn or db_status.get("last_known_good_dsn", "—")
    d1, d2 = st.columns(2)
    with d1:
        st.markdown('<div class="diff-label">CURRENT (as deployed)</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="diff-box diff-before">{html.escape(str(before_dsn))}</div>', unsafe_allow_html=True)
    with d2:
        label = "PROPOSED (pending approval)" if proposed_dsn else "LAST-KNOWN-GOOD (target)"
        st.markdown(f'<div class="diff-label">{label}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="diff-box diff-after">{html.escape(str(after_dsn))}</div>', unsafe_allow_html=True)

    st.divider()

    # --- Intercept Gate -----------------------------------------------
    if pending:
        st.markdown(
            '<div class="intercept-gate">🔓 INTERCEPT GATE — TrueForge has paused the turn. '
            'A human must decide before it resumes.</div>',
            unsafe_allow_html=True,
        )
        with st.container(border=True):
            st.write(f"**Tool:** `{pending.get('tool_name')}`  ·  **thread:** `{pending.get('thread_id')}`")
            st.json(pending.get("arguments", {}))

            st.caption("Quick decision:")
            qc1, qc2, qc3 = st.columns(3)
            if qc1.button("✅ Approve rollback", type="primary", use_container_width=True):
                _post(f"{BACKEND_URL}/approve", {"reason": "Approved via dashboard: rollback to proposed DSN."})
                st.rerun()
            if qc2.button("📝 Approve w/ note", use_container_width=True):
                st.session_state["show_custom_reason"] = True
            if qc3.button("⛔ Deny", use_container_width=True):
                _post(f"{BACKEND_URL}/deny", {"reason": "Denied by judge via dashboard."})
                st.rerun()

            if st.session_state.get("show_custom_reason"):
                reason = st.text_input("Approval note", key="approve_reason_custom")
                if st.button("Confirm approval with note", use_container_width=True):
                    _post(f"{BACKEND_URL}/approve", {"reason": reason or None})
                    st.session_state["show_custom_reason"] = False
                    st.rerun()
    elif state.get("final_report"):
        with st.container(border=True):
            st.subheader("📋 Incident report")
            st.markdown(state["final_report"])
    else:
        st.caption("No pending approval right now.")

    st.divider()

    # --- MCP tools panel -------------------------------------------------
    st.subheader("🧰 MCP servers")
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

    st.caption("**Skill:** `sre-playbook` (SKILL.md) — triage → sandbox diagnostic → gated rollback → verify → report")

st.divider()

# =========================================================================
# PATTERN INSIGHTS -- the pattern-miner agent's curation of known_patterns
# =========================================================================
st.subheader("🧠 Pattern Insights — mining agent")
st.caption(
    "A second agent periodically reads the whole known_patterns.json store and "
    "proposes merging near-duplicate signatures, tuning per-pattern trust "
    "thresholds, or flagging suspicious ones. High-confidence, more-cautious "
    "proposals apply automatically; anything that could reduce future human "
    "oversight always waits for a person here."
)

insights_data = _get(f"{BACKEND_URL}/insights", default={}) or {}
mining = insights_data.get("mining", {})
all_insights = insights_data.get("insights", [])
pending_insights = [i for i in all_insights if i.get("status") == "pending"]
resolved_insights = [i for i in all_insights if i.get("status") != "pending"]

mstatus = mining.get("last_status") or "never run yet"
mlast = mining.get("last_run_at")
mi1, mi2 = st.columns([4, 1])
with mi1:
    st.caption(
        f"Agent `pattern-miner` · last run: **{mstatus}**"
        + (f" at {mlast[11:19]} UTC" if mlast else "")
        + f" · checks every {mining.get('interval_seconds', '—')}s (skips if nothing changed)"
    )
    if mining.get("last_error"):
        st.caption(f"⚠️ last error: {mining['last_error']}")
with mi2:
    if st.button("⛏️ Mine now", use_container_width=True):
        _post(f"{BACKEND_URL}/mine")
        st.rerun()

if pending_insights:
    st.markdown(f"**{len(pending_insights)} proposal(s) awaiting your review:**")
    for ins in pending_insights:
        with st.container(border=True):
            st.write(f"**{ins.get('type', '?').upper()}**  ·  confidence: `{ins.get('confidence', '?')}`")
            st.caption(ins.get("reasoning", ""))
            st.json(ins.get("payload", {}))
            b1, b2 = st.columns(2)
            if b1.button("✅ Approve", key=f"approve_insight_{ins['id']}", type="primary", use_container_width=True):
                _post(f"{BACKEND_URL}/insights/{ins['id']}/approve", {"reason": "Approved via dashboard"})
                st.rerun()
            if b2.button("⛔ Reject", key=f"reject_insight_{ins['id']}", use_container_width=True):
                _post(f"{BACKEND_URL}/insights/{ins['id']}/reject", {"reason": "Rejected via dashboard"})
                st.rerun()
else:
    st.caption("No proposals awaiting review right now.")

if resolved_insights:
    status_badge = {
        "auto_approved": "🤖 auto-applied",
        "approved": "🧑‍⚖️ human-approved",
        "rejected": "🧑‍⚖️ rejected",
    }
    with st.expander(f"📜 Auto-applied / resolved proposals ({len(resolved_insights)})"):
        for ins in resolved_insights[:30]:
            badge = status_badge.get(ins.get("status"), ins.get("status", "?"))
            st.markdown(f"**{ins.get('type', '?')}** — {badge} — _{ins.get('reasoning', '')}_")
            st.json(ins.get("payload", {}))

patterns = (_get(f"{BACKEND_URL}/patterns", default={}) or {}).get("patterns", [])
with st.expander(f"📚 Known patterns — the deterministic pre-check's knowledge base ({len(patterns)})"):
    if not patterns:
        st.caption("No patterns recorded yet — approve or deny a deterministic-path fix to start building this.")
    else:
        rows = []
        for p in patterns:
            suspicious = (p.get("suspicious") or {}).get("flag")
            rows.append({
                "signature": p.get("signature", "")[:70],
                "variants": len(p.get("variants") or [p.get("signature")]),
                "approved": p.get("approved_count", 0),
                "denied": p.get("denied_count", 0),
                "consecutive": p.get("consecutive_approvals", 0),
                "threshold": p.get("trust_threshold") or "default",
                "trusted now": "✅" if not suspicious and p.get("consecutive_approvals", 0) >= (p.get("trust_threshold") or 2) else "—",
                "suspicious": "🚩" if suspicious else "",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()
with st.expander("🔬 Full raw SSE event log (advanced — the ground truth every panel above is derived from)"):
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
            "precheck.started": "⚡",
            "precheck.inconclusive": "🤷",
            "precheck.escalating": "🚀",
            "precheck.diagnosis": "🎯",
            "fallback.started": "🧮",
            "fallback.diagnosis": "🔍",
            "fallback.apply_result": "✅",
            "fallback.apply_failed": "❌",
        }.get(etype, "•")
        with st.expander(f"{icon} [{e.get('seq')}] {etype} — {e.get('at', '')[11:19]}", expanded=False):
            st.json(data)

# --- auto refresh ----------------------------------------------------------
auto = st.toggle("Live auto-refresh", value=True)
interval = 1.2 if status in ("investigating", "intervention_required", "remediating") else 3.0
if auto:
    time.sleep(interval)
    st.rerun()
