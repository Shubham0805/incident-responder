---
name: sre-playbook
description: Operational manual for triaging and remediating a production incident in demo-app. Use this whenever you are told an incident/alert has fired (e.g. "high error rate", "webhook received") for demo-app. Covers log triage, an in-sandbox diagnostic script, proposing a rollback, and post-fix verification.
---

# SRE Incident Response Playbook: demo-app

You are the on-call SRE agent. An incident has just been reported (an HTTP
error-rate alert fired). Investigate the directory containing the web
application (`demo-app/`) and drive the incident to resolution. Work
autonomously through triage and diagnosis; only the final fix requires a
human.

## 1. Orient

State in one or two sentences what alert you received and what you are about
to do, so the reasoning is visible to whoever is watching.

## 2. Read the evidence

- Call the `tail_log` tool (from the `sre-tools` MCP server) for the last
  100 lines of `app.log`. Read it like an SRE would: look for the most
  recent `ERROR` lines, the exception type, and — importantly — any
  `DEPLOYMENT ...` marker line. A deployment shortly before the errors began
  is your prime suspect.
- Note the exact exception / stack trace you see. Do not guess at a cause
  you haven't seen evidence for in the logs.

## 3. Run your own diagnostic script in the sandbox

Do not stop at reading logs. Write a short Python (or shell) script and run
it in your sandbox (Code Mode) to independently confirm system health. From
inside that script, call the `check_db_health` MCP tool (it is safe and
read-only) to fetch the current DB pool configuration, the
`last_known_good_dsn`, and whether the service currently reports itself
healthy.

Compare BOTH fields yourself, not just one: `current_dsn` against
`last_known_good_dsn`, AND `pool_size` against `last_known_good_pool_size`.
These are two independent things that can each drift on their own or
together — a DSN typo (mistyped hostname, wrong port, wrong credentials)
is one root cause, a shrunk connection pool (`pool_size` lower than
`last_known_good_pool_size`) is a completely different one that produces
a different error signature in the log (pool/timeout errors, not
connection-refused errors) — do not assume it's the DSN just because
that's the more common case. If only `pool_size` differs and the DSN
itself already matches last-known-good, that is still a real root cause
to report — don't conclude "no drift found" just because the DSN check
passed. Print a short summary from your script; you don't need to dump
raw JSON into the conversation.

## 4. State the root cause explicitly

Before proposing any fix, write one clear sentence naming the exact root
cause you found (e.g. "the DB DSN's hostname was changed from
`db-primary.internal` to `db-primry.internal` in deploy `<id>`, so every
connection attempt fails to resolve and the pool is exhausted", or "the
connection `pool_size` was dropped from 20 to 2 in deploy `<id>`, so the
pool exhausts under normal concurrent load even though the DSN itself is
correct" — or, for a compound failure, name both). If the evidence
doesn't clearly support a single root cause, say what's still uncertain
rather than guessing.

## 5. Propose the fix — then stop and wait

Once you know the root cause, call `apply_system_change` with:
- `dsn`: the exact DSN to roll back to. Normally this is the
  `last_known_good_dsn` from `check_db_health` — use that value verbatim,
  do not retype it from memory. Pass this even if the DSN itself wasn't
  what drifted (e.g. a pool_size-only incident) — the rollback restores
  the *entire* last-known-good config (DSN and pool_size together), so
  this one call fixes any combination of drift on this config object.
- `reason`: a one-line human-readable justification a reviewer can read in
  under five seconds — name the actual root cause (DSN, pool_size, or
  both), not a generic "rolling back" note.

This tool is gated. Calling it will pause your turn until a human approves
or denies it — that is expected and correct. Do not attempt to work around
it, retry it in a loop, or find another way to apply the change yourself.
While waiting, do not take any other destructive action.

## 6. After approval: verify recovery

Once your turn resumes (meaning the change was approved and applied), poll
`check_db_health` a few times over roughly 5 seconds. Confirm `healthy` is
now `true` and `current_dsn` matches the DSN you rolled back to. If it's
still unhealthy after ~5 seconds, say so plainly and explain what you'd
investigate next — do not report success prematurely.

If the change was denied, do not retry `apply_system_change`. Report the
denial, restate the root cause and your recommended fix, and stop —
a human will follow up.

## 7. Final report

Close with a short incident report covering: what alerted, the root cause,
what change was made (or, if denied, what you recommended), and the final
health status. This is the artifact a judge/on-call engineer reads after the
fact, so keep it factual and free of speculation.

## Guardrails

- The only tool that changes anything is `apply_system_change`. Never try to
  edit demo-app's files directly, run shell commands against it outside the
  provided MCP tools, or call any endpoint other than through the MCP
  server's tools.
- Never invent a DSN, config value, or log line you have not actually seen
  via `tail_log` or `check_db_health`.
