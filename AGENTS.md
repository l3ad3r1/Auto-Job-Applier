# Agent instructions — Auto Job Applier

These instructions apply to ANY coding agent driving this pipeline (Codex,
Cursor, Claude Code, Hermes, ...). The pipeline itself does the heavy lifting;
your job is to run CLI commands, interpret results, and report.

**New here? Read [CODEX.md](CODEX.md) first** — it explains how the system fits
together, how the Hermes scheduler drives it, the maintenance playbook, and the
credentials model. This file is the operating rules; CODEX.md is the context
behind them. Machine-specific values (cron job IDs, absolute paths) are in
`RUNBOOK.md`, which is gitignored and local-only.

## Commands

Run from the repo root with the venv Python (`python` below means
`.venv/Scripts/python.exe` on Windows, `.venv/bin/python` elsewhere):

```bash
python app.py status                 # queue counts per state
python app.py discover <platform>    # linkedin | naukri | indeed
python app.py discover linkedin --today-hiring # last-24h Hiring listings
python app.py prepare                # score vs job spec + draft answers
python app.py review                 # dashboard at http://127.0.0.1:8377
python app.py apply <platform> [--limit N]   # submit APPROVED items only
python app.py login <platform>       # one-time manual sign-in (user only)
python app.py doctor                 # read-only health canary (exit 1 = unhealthy)
python app.py sync-sheet             # append applied jobs to the Google Sheet
python app.py job-board              # rebuild the detailed local Excel job board
python app.py retire-stale [--days N] # expire old pending-review listings only
```

The daily `routine` auto-appends new applications to the Google Sheet when
`sheets.enabled` is set in config (no-op otherwise); no agent action needed.

Discovery and apply open a real browser and take minutes — run them one at a
time with generous timeouts, never in parallel.

## The routine ("run the job applier")

1. `doctor` — if a platform is reported unhealthy (expired session, rotted
   selectors), SKIP that platform for this run and put the doctor's message
   at the top of your report. Do not abort the whole run for one platform.
2. `discover` each healthy platform, sequentially. LinkedIn scheduled runs use
   `--today-hiring` to prioritize Easy Apply listings mentioning Hiring and
   posted within the last 24 hours.
3. `prepare`.
4. `retire-stale` — retire pending-review listings older than 14 days as
   `expired`; never touch approved items.
5. `status` — report pending_review count; remind the user to approve in the
   dashboard.
6. Only if approved items exist: `apply` per platform; report each result
   verbatim. Failures marked "transient, will retry" are re-attempted
   automatically on the next run (max 2 retries) — report them but don't act.
7. `job-board` — rebuild the Excel board after every 09:00 and 18:00 run.
   The 21:00 watchdog verifies its freshness, structure, and queue totals.

## Hard rules

- **Never approve or reject queue items.** Not via the dashboard API, not via
  the SQLite DB. Note the pipeline itself *does* promote strong matches past
  review when `auto_apply.enabled` is set — that is a configured behaviour with
  rails, not licence for you to approve rows or widen the bars. Dry-run any
  threshold change with `python app.py auto-approve --dry-run`.
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
