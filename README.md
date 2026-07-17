# Auto Job Applier

Multi-platform job application pipeline with a **human review gate**: it
discovers jobs on **LinkedIn, Naukri, and Indeed**, scores them against your
job spec (target titles, industries, salary floor), drafts screening answers
(profile map + local-LLM fallback), and submits applications **only after you
approve them** in a local dashboard — with daily caps and human-like pacing.

```
discover → score/prepare → YOU approve in the dashboard → apply (paced, capped)
```

## Features

- **Review queue** — nothing is ever submitted without your explicit approval
- **Job-spec matching** — target titles, industry keywords, dealbreaker
  phrases, and a hard salary floor (`min_salary_lpa`); non-matches are
  auto-skipped with the reason recorded
- **Screening answers** — exact answers from your `profile.yaml` map first,
  then a local LLM (Ollama) grounded strictly in your profile; anything it
  can't answer truthfully gets flagged for you instead of guessed
- **Salary capture** — posted ranges parsed (Lacs P.A., ₹/month, ₹/year) and
  shown in the dashboard
- **Safety rails** — per-platform daily caps, randomized delays, fail-safe
  form handling (any un-answerable form is discarded, never half-submitted),
  no credential handling (you log in manually once per platform)
- **Agent-ready** — drive it from Claude Code, Codex, Cursor, or Hermes
  (see [Using with an agent](#using-with-an-agent))

## Setup

```bash
git clone https://github.com/l3ad3r1/Auto-Job-Applier.git
cd Auto-Job-Applier
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # .venv/bin/pip on Linux/macOS
.venv/Scripts/playwright install chromium

cp config.example.yaml config.yaml      # search keywords, locations, caps
cp profile.example.yaml profile.yaml    # your identity, answers, job spec
mkdir resumes && cp /path/to/your-resume.pdf resumes/default.pdf
```

Both YAML files are gitignored — they hold personal data and never leave your
machine.

**Optional local LLM** (for screening questions your answer map doesn't
cover): install [Ollama](https://ollama.com), pull a model
(`ollama pull gemma4`), and set `llm.enabled: true` in `config.yaml`. Without
it, unmatched questions are simply flagged for review — the pipeline still
works.

## Usage

```bash
python app.py login linkedin      # one-time manual sign-in per platform
python app.py discover linkedin   # scrape matching jobs (also: naukri, indeed)
python app.py prepare             # score vs job spec + draft answers
python app.py review              # dashboard at http://127.0.0.1:8377
python app.py apply linkedin      # submit ONLY what you approved
python app.py status              # queue counts
python app.py export              # applied jobs -> data/applied_jobs.csv
python app.py sync-sheet          # append applied jobs to your Google Sheet
```

### Google Sheet auto-append (optional)

Mirror every application into a Google Sheet with no Google Cloud project or
service account — a small Apps Script webhook does it. The daily routine
appends new applications automatically (and never duplicates). One-time setup:
[hermes/sheets-setup.md](hermes/sheets-setup.md).

(`python` = `.venv/Scripts/python.exe` on Windows, `.venv/bin/python`
elsewhere.)

The login step opens a real browser window; you sign in yourself and the
session persists in `profiles/<platform>/` (gitignored). The tool never sees
or stores passwords.

## Using with an agent

The pipeline is agent-agnostic: the agent just runs the CLI and reports.
[AGENTS.md](AGENTS.md) defines the routine and the hard rules every agent
must follow (never self-approve, never edit your profile, never bypass caps
or CAPTCHAs).

### Claude Code

The repo ships a skill at `.claude/skills/job-applier/`. Open a session in
the repo and say **"run the job applier"** (or `/job-applier`). For a
scheduled run, use your OS scheduler:

```
schtasks /create /tn JobApplier /sc daily /st 09:00 ^
  /tr "cmd /c cd /d C:\path\to\Auto-Job-Applier && claude -p \"Run the job applier routine per AGENTS.md, then summarize.\""
```

### Codex CLI

Codex reads `AGENTS.md` automatically:

```bash
codex exec "Run the job applier routine per AGENTS.md, then summarize."
```

Schedule the same command via cron / Task Scheduler for daily runs.

### Cursor

Cursor's agent also picks up `AGENTS.md`. Open the repo and ask:
*"Run the job applier routine and show me what matched."* Interactive only —
use one of the CLI agents above for scheduled runs.

### Hermes

[hermes/](hermes/) contains a ready-made Hermes skill plus cron + messaging
(e.g. twice-daily runs with Telegram reports). See
[hermes/README.md](hermes/README.md) for deployment and the operational
gotchas (profile-local skills dir, model pinning, bash paths).

> **Scheduling constraint:** browser sessions live on your machine, so
> scheduled runs must execute *locally*. Cloud agent runners (Claude Code web
> routines, Codex cloud tasks) cannot drive this pipeline.

## Layout

- `core/` — models, SQLite queue, profile loader, job-spec matcher
- `adapters/` — one module per platform (LinkedIn, Naukri, Indeed)
- `llm/` — grounded local-LLM answer fallback (OpenAI-compatible, stdlib-only)
- `dashboard/` — FastAPI review UI
- `safety.py` — daily caps and human-like pacing
- `hermes/` — Hermes agent offload (skill + ops notes)

## Disclaimer

Automating applications may violate the terms of service of LinkedIn, Indeed,
and other platforms, and can get accounts restricted. This tool deliberately
keeps a human in the loop, enforces conservative caps, and never bypasses
CAPTCHAs or logins — but you use it at your own risk. It is a personal
productivity tool, not a mass-application service.
