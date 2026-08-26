# Contributing (hackathon submission rules)

This repo follows the hackathon's process requirements, not just good hygiene:

- **No direct pushes to `main`.** Turn on branch protection on `main`
  (Settings -> Branches -> Add rule -> require a pull request before
  merging) as soon as you create the GitHub repo.
- **Every change lands via a pull request**, reviewed by Qodo Merge /
  PR-Agent (see `.github/workflows/qodo.yml`). At least one PR needs to show:
  a Qodo review comment, the findings addressed or explicitly dismissed with
  a reason, and a follow-up review after that (comment `/review` again).
- **Never commit credentials.** `.env` is git-ignored. Your LLM provider key
  and Daytona key live only in TrueForge's local Settings UI
  (`http://localhost:8790`), never in this repo, and should not appear on
  screen in the demo video either.
- **Pre-hackathon code doesn't count as the project.** This repo was scaffolded
  fresh for the hackathon (Aug 26, 2026) — keep it that way; don't fold in
  unrelated old projects.
