---
name: job-applier
description: Drive the local job-application pipeline (LinkedIn/Naukri/Indeed) — discover jobs, prepare answers, report the review queue, and submit user-approved applications. Human approval is a hard gate.
version: 1.0.0
author: local
license: MIT
platforms: [windows]
prerequisites:
  commands: []
metadata:
  hermes:
    tags: [Jobs, Automation, Personal]
---

# Job Applier — auto job applications with a human review gate

A local pipeline at `E:\claude-projects\job-applier` that discovers jobs on
LinkedIn, Naukri, and Indeed, scores them against the user's job spec
(profile.yaml), drafts screening answers (profile map +
local Ollama LLM), and — only after the user approves items in the dashboard —
submits applications with human-like pacing and daily caps.

## Commands

Always use the project's venv Python, from the project directory. The
terminal tool runs **bash** — use forward slashes, never backslashes:

```bash
cd "E:/claude-projects/job-applier"
./.venv/Scripts/python.exe app.py status              # queue counts per state
./.venv/Scripts/python.exe app.py discover naukri     # scrape new jobs (also: linkedin, indeed)
./.venv/Scripts/python.exe app.py prepare             # score + draft answers -> pending_review
./.venv/Scripts/python.exe app.py apply naukri --limit 5   # submit APPROVED items only
```

Discovery/apply commands open a real browser and can take several minutes —
use a generous terminal timeout and run them one at a time.

The review dashboard is `python app.py review` → http://127.0.0.1:8377
(FastAPI; leave it to the user — see Hard Rules).

## Daily routine (what "run the job applier" means)

1. `discover linkedin`, `discover naukri`, `discover indeed` — run sequentially,
   never in parallel (each opens a real browser with a persistent profile).
2. `prepare` — new jobs get scored; matches land in pending_review.
3. `status` — then TELL THE USER how many are pending review and how many
   approved items are waiting, with a reminder to open the dashboard.
4. Only if there are APPROVED items: `apply <platform>` for each platform
   that has them. Report per-job results verbatim (applied / failed + reason).

## Hard rules — do not violate

- **NEVER approve or reject queue items yourself.** Do not POST to the
  dashboard's /decide endpoints, do not UPDATE the SQLite DB states to
  'approved'. Approval is the user's decision, made in the dashboard. Your job
  ends at reporting what's pending.
- **NEVER edit** `profile.yaml`, `config.yaml`, or anything under
  `profiles/` (browser sessions). If an apply fails with
  "Unanswered required question: X", report the question to the user and ask
  what the answer should be — they update the profile.
- **Respect caps.** `apply` self-enforces daily caps (20 LinkedIn /
  25 Naukri / 15 Indeed) and stops itself. Never work around a
  "daily cap reached" stop.
- **Never handle credentials.** If a run exits with "Not logged in to
  <platform>", tell the user to run `app.py login <platform>` manually.
- One browser at a time: if a command fails with "profile is already in use",
  a leftover Chromium window holds the profile — ask the user to close it (or
  close the visible window), then retry once.

## Failure notes you may see

- `external 'apply on company site' job` / `No Indeed Apply button` — the
  job needs a manual application on the employer's site; list these for the user.
- `chatbot blocked on: [...]` (Naukri) or `Unanswered question: '...'` —
  a screening question nobody could answer; surface it verbatim.
- `Indeed verification wall` — Cloudflare CAPTCHA; the user must solve it in the
  browser window. Never attempt to bypass it.

## Reporting format

End every run with a short summary: applied N (per platform), failed N with
one-line reasons, pending_review N, caps remaining. Flag any job that looks
like a strong salary jump (posted range well above the profile's min_salary_lpa).
