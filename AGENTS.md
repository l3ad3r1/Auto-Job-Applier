# Agent instructions — Auto Job Applier

These instructions apply to ANY coding agent driving this pipeline (Codex,
Cursor, Claude Code, Hermes, ...). The pipeline itself does the heavy lifting;
your job is to run CLI commands, interpret results, and report.

## Commands

Run from the repo root with the venv Python (`python` below means
`.venv/Scripts/python.exe` on Windows, `.venv/bin/python` elsewhere):

```bash
python app.py status                 # queue counts per state
python app.py discover <platform>    # linkedin | naukri | indeed
python app.py prepare                # score vs job spec + draft answers
python app.py review                 # dashboard at http://127.0.0.1:8377
python app.py apply <platform> [--limit N]   # submit APPROVED items only
python app.py login <platform>       # one-time manual sign-in (user only)
```

Discovery and apply open a real browser and take minutes — run them one at a
time with generous timeouts, never in parallel.

## The routine ("run the job applier")

1. `discover` each configured platform, sequentially.
2. `prepare`.
3. `status` — report pending_review count; remind the user to approve in the
   dashboard.
4. Only if approved items exist: `apply` per platform; report each result
   verbatim.

## Hard rules

- **Never approve or reject queue items.** Not via the dashboard API, not via
  the SQLite DB. Approval is the human's job; the pipeline only submits rows
  the user approved.
- **Never edit `profile.yaml` or `config.yaml`** on your own. When an apply
  fails with an unanswered question, surface the question and let the user
  add the answer.
- **Never bypass daily caps, CAPTCHAs, or login walls.** Caps are enforced in
  `safety.py`; a "cap reached" stop is final for the day. CAPTCHA/verification
  walls are for the user to solve manually.
- **Never touch `profiles/`** (persistent browser sessions) or handle
  credentials. "Not logged in" → tell the user to run `login <platform>`.
- "profile is already in use" → a leftover browser window holds the profile;
  have the user close it, retry once.

## Scheduling (optional)

The pipeline must run on the machine that owns the browser profiles — cloud
agent runners won't work. Schedule a local agent invocation instead, e.g.
Windows Task Scheduler / cron running:

```bash
claude -p "Run the job applier routine per AGENTS.md, then summarize."   # Claude Code
codex exec "Run the job applier routine per AGENTS.md, then summarize."  # Codex CLI
```

A ready-made Hermes agent skill (with cron + Telegram delivery) is in
[hermes/](hermes/).
