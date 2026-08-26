# Incident Responder — an SRE agent on TrueForge

Built for [The Agent Harness Hackathon](https://www.wemakedevs.org/hackathons/trueforge) (WeMakeDevs × TrueFoundry × Qodo, Aug 24–30 2026).

An autonomous on-call agent that watches a (simulated) production service, triages a
real incident by reading logs and running its own diagnostic script in an isolated
sandbox, proposes a rollback, **stops and waits for a human to approve it**, then
executes the fix and verifies recovery — all visible live in a transparent
Streamlit "mission control" dashboard built for judges to watch, not just a demo
narrator to describe.

It runs on the real [TrueForge](https://github.com/truefoundry/trueforge) agent
harness: real MCP tools, a real Daytona sandbox, and TrueForge's own
`tool.approval_required` human-in-the-loop gate — nothing here fakes the harness.

## Why this satisfies the brief

| Judging category | How this project addresses it |
|---|---|
| Potential Impact | A real on-call pain point: triage + guarded remediation, not just a chatbot. |
| Creativity & Originality | The whole incident is choreographed end-to-end (chaos → telemetry → agent → human gate → verified fix), not a single prompt. |
| Technical Excellence | Real webhook → real TrueForge session/turn → real sandbox script → real gated MCP tool → real resume-on-approval. |
| Use of Sponsor Tools | Every TrueForge primitive in the brief is used on purpose: MCP servers, sandbox, skills, `require_approval_for_tools`, SSE event stream. Qodo reviews the PR that ships this code (see below). |
| Control & Safety | The one destructive action (`apply_system_change`) is MCP-tagged `@destructive` and explicitly gated — the harness *cannot* run it without a human clicking Approve. The agent only ever touches the sandboxed demo app, never anything real. |
| Presentation | The dashboard narrates the harness's own reasoning, tool calls, and sandbox output live, in the judges' own words as the agent produces them. |

## Architecture

```
 ┌───────────────┐   error rate   ┌──────────────────┐   webhook    ┌──────────────────────┐
 │   demo-app     │ ───spikes───► │ telemetry watcher │ ───POST───► │  backend orchestrator │
 │ (fake prod svc)│   85%+        │ (mock monitoring) │             │      (FastAPI)        │
 └───────┬────────┘                └──────────────────┘             └──────────┬────────────┘
         │  logs to app.log                                                    │ creates session,
         │  exposes /metrics, /chaos/inject                                    │ streams SSE turn
         ▼                                                                     ▼
 ┌───────────────┐   tail_log (read)        ┌────────────────────────────────────────────┐
 │  sre-tools     │ ◄────────────────────── │            TrueForge harness                │
 │  MCP server    │   apply_system_change   │  (npx @truefoundry/trueforge, localhost:8790)│
 │ (our code)     │   (GATED, needs human)  │  agent = "incident-responder"                │
 └───────────────┘ ◄────────────────────── │  + SKILL.md "sre-playbook"                   │
                                             │  + Daytona sandbox (agent writes/runs its    │
                                             │    own diagnostic script here)               │
                                             └───────────────┬──────────────────────────────┘
                                                              │ tool.approval_required
                                                              ▼
                                             ┌────────────────────────────────────────────┐
                                             │      Streamlit dashboard (judges watch)      │
                                             │  status banner · live event timeline ·       │
                                             │  MCP/tool panel · error-rate chart ·          │
                                             │  [Approve & Execute Rollback] button          │
                                             └────────────────────────────────────────────┘
```

Everything except the LLM call and the sandbox execution runs on your machine.
No credentials ever live in this repo or in the demo video — you paste your LLM
and Daytona keys directly into TrueForge's own Settings UI at `localhost:8790`.

## Mapping the original 6-step spec to real TrueForge primitives

1. **Failure trigger** — `demo-app/app.py` is a small FastAPI service with fake
   traffic and a `/chaos/inject` endpoint that corrupts its DB pool config string.
   `telemetry/watcher.py` polls `/metrics`; once `error_rate >= ERROR_RATE_THRESHOLD`
   it POSTs a webhook to the backend — this *is* the "mock telemetry system."
2. **Harness init** — `backend/main.py`'s webhook handler calls the real TrueForge
   HTTP API (`POST /api/v1/sessions`) to instantiate a session for the
   `incident-responder` agent, which has `skills: [{name: "sre-playbook"}]` and
   `config.sandbox.enabled: true` — TrueForge provisions the isolated Daytona
   sandbox and mounts `SKILL.md` at `/opt/tfy/skills/sre-playbook`.
3. **Sandbox triage** — the agent follows `skills/sre-playbook/SKILL.md`: it calls
   our `sre-tools` MCP server's `tail_log` tool to read `app.log`, then uses
   TrueForge's Code Mode to **write and run its own diagnostic script** inside the
   sandbox to inspect DB pool state — nothing here is a canned "diagnosis" tool,
   the agent reasons its way to the root cause.
4. **HITL gate** — `apply_system_change` is registered on the `sre-tools` MCP
   server and explicitly listed in `require_approval_for_tools`. The instant the
   agent calls it, TrueForge pauses the turn and emits `tool.approval_required`
   over SSE. The backend relays that into `/state`; the dashboard flips to
   flashing amber and renders the agent's reasoning + the exact patch/command.
5. **Human action** — the judge clicks **Approve & Execute Rollback** in
   Streamlit → the dashboard calls the backend's `/approve` → the backend POSTs
   a new turn with a `user.tool_approval` (`status: allow`) input, which is the
   literal TrueForge API for resuming a gated tool call.
6. **Remediation & verification** — the harness unpauses, the agent runs the
   rollback inside the sandbox, polls the demo app's health for up to ~5s, and
   writes a final incident report as its last message. The dashboard's status
   banner returns to green once `error_rate` is back to 0%.

## Prerequisites (you set these up yourself — see Safety note below)

- Docker + Docker Compose (recommended path below) **or** Python 3.11+ if
  you'd rather run things natively (alternative path below)
- Node.js 22.14+, for TrueForge itself — this always runs directly on your
  host, in or out of Docker (see "Why isn't TrueForge in the compose file"
  below)
- One LLM provider key, added in TrueForge's Settings → Models
- A [Daytona](https://www.daytona.io/) API key, added in TrueForge's
  Settings → Sandbox providers (TrueForge's only sandbox provider today)
- A [Qodo Merge](https://www.qodo.ai/) GitHub App installed on your repo
  (free 14-day trial, no card) — required for hackathon submission

## Setup & running (Docker — recommended, this is what judges should use)

```bash
# 1. Start the harness in its own terminal and leave it running
npx @truefoundry/trueforge@latest

# 2. In TrueForge's UI (localhost:8790): Settings -> Models (add your LLM key)
#    and Settings -> Sandbox providers (add your Daytona key).

# 3. Everything else -- demo-app, sre-tools MCP server, backend, telemetry
#    watcher, dashboard -- in one command
cd incident-responder
cp .env.example .env   # adjust only if you changed a port above
docker compose up --build

# 4. In a second terminal, one-time registration against your running
#    TrueForge instance (needs Python + httpx locally -- this one script is
#    not itself containerized, it just makes two HTTP calls to localhost:8790)
python3 -m pip install httpx --break-system-packages 2>/dev/null || python3 -m pip install httpx
python3 scripts/register.py
```

Open **http://localhost:8501** for the dashboard. `docker compose down` stops
everything (TrueForge, running separately on your host, is unaffected).

### Why isn't TrueForge in the compose file?

TrueForge ships its own official Docker Compose setup, versioned with the
harness itself. Nesting a copy of that inside this repo's compose file would
mean tracking a moving target and getting cross-container networking right
for infrastructure we don't own. Running TrueForge the way its own docs
describe (`npx ...` or its own compose) and letting this repo's containers
reach it over `host.docker.internal` is simpler and won't silently drift out
of date. Every service in `docker-compose.yml` here is still one command.

### Alternative: run natively (no Docker)

```bash
npx @truefoundry/trueforge@latest   # same as step 1 above, own terminal

cd incident-responder
python3 -m venv .venv && source .venv/bin/activate
pip install -r demo-app/requirements.txt -r mcp-server/requirements.txt \
            -r backend/requirements.txt -r dashboard/requirements.txt \
            -r telemetry/requirements.txt
cp .env.example .env

python scripts/register.py
./scripts/run_all.sh
```

Both paths use the exact same env vars (see `.env.example`) and land on the
same ports, so they're interchangeable — use whichever the judge running
this already has installed.

Registering the skill needs `skills/sre-playbook/` to be reachable from a git
remote (TrueForge's Skills registry is git-backed). Push this repo to GitHub
first, then point `scripts/register.py` / TrueForge Settings → Skills at
`https://github.com/<you>/<repo> :: skills/sre-playbook`. See comments in
`scripts/register.py` for the exact call.

Either way, open the dashboard, then either wait for the watcher to notice
organic errors or hit the **Inject Failure** button on the dashboard (calls
`demo-app`'s `/chaos/inject`) to trigger the incident on demand for a live
demo.

## Safety

- The only tool that can change anything (`apply_system_change`) only ever
  touches `demo-app`'s own local sandbox config — never a real system — and it
  is hard-gated by TrueForge's approval mechanism; the harness will not run it
  without a human clicking Approve.
- No API keys or tokens are stored in this repo. `.env` is git-ignored.
  Your LLM and Daytona credentials live only in TrueForge's local Settings UI.

## Hackathon submission checklist

- [ ] Public GitHub repo (this one)
- [ ] All changes after the initial scaffold land via reviewed pull requests
      (branch protection on `main`, no direct pushes)
- [ ] Qodo Merge installed; at least one PR shows a Qodo review with findings
      addressed/dismissed and a follow-up review — link it here once open
- [ ] ~3 minute demo video showing the full 6-step flow, including the
      approval click, with no credentials visible on screen
- [ ] This README kept in sync with whatever actually ships

## Repo layout

```
demo-app/        fake production service + chaos injector (+ Dockerfile)
telemetry/       mock monitoring that fires the incident webhook (+ Dockerfile)
mcp-server/      our sre-tools MCP server (tail_log, apply_system_change) (+ Dockerfile)
skills/sre-playbook/   the SRE operational manual (SKILL.md) the agent follows
backend/         FastAPI orchestrator: webhook -> TrueForge session -> SSE -> /state, /approve (+ Dockerfile)
dashboard/       Streamlit "mission control" UI for judges (+ Dockerfile)
scripts/         one-time registration + local run-everything script (native path)
docker-compose.yml   one command for demo-app + mcp-server + backend + watcher + dashboard
```

Each of the five services above is also a small standalone container — see
`docker-compose.yml` at the repo root for how they're wired together, and
"Setup & running (Docker)" above for the one-command path.
