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

- Python 3.11+ (the `mcp` package this project depends on requires it — on
  macOS the system default `python3` is often older, e.g. 3.9; check with
  `python3 --version` and install a newer one via Homebrew if needed:
  `brew install python@3.11`)
- Node.js 22.14+, for TrueForge itself
- Docker + Docker Compose — optional, only needed if you want `demo-app` and
  the `sre-tools` MCP server containerized (see below); everything else runs
  natively either way
- One LLM provider key, added in TrueForge's Settings → Models
- A [Daytona](https://www.daytona.io/) API key, added in TrueForge's
  Settings → Sandbox providers (TrueForge's only sandbox provider today)
- A [Qodo Merge](https://www.qodo.ai/) GitHub App installed on your repo
  (free 14-day trial, no card) — required for hackathon submission

## Setup & running

This is the path actually verified end-to-end against a live TrueForge
instance — every process (demo-app, the sre-tools MCP server, the backend
orchestrator, the telemetry watcher, and the dashboard) runs directly on
your machine, talking to each other and to TrueForge over plain
`localhost`. No container networking involved anywhere.

```bash
# 1. Start the harness in its own terminal and leave it running
npx @truefoundry/trueforge@latest

# 2. In TrueForge's UI (localhost:8790): Settings -> Models (add your LLM key)
#    and Settings -> Sandbox providers (add your Daytona key).

# 3. In a second terminal
cd incident-responder
python3 -m venv .venv && source .venv/bin/activate   # use python3.11+ here, see Prerequisites
pip install --upgrade pip
pip install -r demo-app/requirements.txt -r mcp-server/requirements.txt \
            -r backend/requirements.txt -r dashboard/requirements.txt \
            -r telemetry/requirements.txt
cp .env.example .env
# Fill in TRUEFORGE_MODEL (exact "provider/model" string -- see the comment
# in .env.example) and, once you've pushed this repo, GITHUB_REPO_URL.

python scripts/register.py   # registers the MCP server, skill, and agent --
                              # re-run any time you change the manifests
./scripts/run_all.sh
```

Open **http://localhost:8501** for the dashboard, then either wait for the
watcher to notice organic errors or hit **Inject Failure** (calls
`demo-app`'s `/chaos/inject`) to trigger the incident on demand for a live
demo. `Ctrl+C` in that terminal stops demo-app/mcp-server/backend/watcher/
dashboard together; stop TrueForge separately with `Ctrl+C` in its own
terminal.

Registering the skill needs `skills/sre-playbook/` to be reachable from a git
remote (TrueForge's Skills registry is git-backed) — push this repo to
GitHub before running `scripts/register.py`, and set `GITHUB_REPO_URL` in
`.env` to it.

### Optional: containerize demo-app and the MCP server

```bash
cd incident-responder
docker compose up --build
```

This brings up `demo-app` and `sre-tools` (the two services that only ever
talk to each other over Docker's own internal network, never to your host)
in containers instead, publishing them on the same `8081`/`8082` ports the
native versions use. `backend`, the watcher, and the dashboard still need
to run natively either way — `docker-compose.yml` has the full explanation
of why those three aren't containerized: an earlier attempt at bridging a
container to a host-bound TrueForge process (an auto-discovered gateway IP
paired with a local proxy) turned out not to reliably work on Docker
Desktop, and carried a real security exposure besides, so it was removed
rather than patched further once the fully-native path was proven to work
instead.

Do the venv/pip-install/`.env`/`register.py` setup from step 3 above as
normal, but when it comes to starting things, run **only** the remaining
three natively — don't run plain `./scripts/run_all.sh` on its own, since
it starts its own demo-app/mcp-server too and they'll fail to bind (Compose
is already holding those ports):

```bash
SKIP_DEMO_APP=1 SKIP_MCP_SERVER=1 ./scripts/run_all.sh
```

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
telemetry/       mock monitoring that fires the incident webhook
mcp-server/      our sre-tools MCP server (tail_log, apply_system_change) (+ Dockerfile)
skills/sre-playbook/   the SRE operational manual (SKILL.md) the agent follows
backend/         FastAPI orchestrator: webhook -> TrueForge session -> SSE -> /state, /approve
dashboard/       Streamlit "mission control" UI for judges
scripts/         one-time registration + local run-everything script (native path)
docker-compose.yml   containerizes demo-app + mcp-server only -- see below
```

`demo-app` and `mcp-server` are also small standalone containers — see
`docker-compose.yml` at the repo root for how they're wired together, and
"Optional: containerize demo-app and the MCP server" above for that path.
backend, telemetry, and dashboard all need to reach TrueForge and/or each
other across the host boundary, so they run natively; see the "Why the
split" comment at the top of `docker-compose.yml`. Their `Dockerfile`s
(where present) are leftovers from an earlier all-Docker layout and aren't
referenced by `docker-compose.yml` anymore.
