# Hermes offload

The daily routine runs unattended on the local [hermes-agent](https://github.com/NousResearch/hermes-agent)
via a cron job that delivers a report to Telegram.

## Two ways to run it

**1. no_agent script mode (recommended for scheduled runs).** A deterministic
shell wrapper runs `app.py routine` and Hermes delivers its stdout verbatim to
Telegram — **no LLM in the orchestration path**. This is the robust production
setup: it can't hit connection errors, tool-approval blocks, or weak-model
mistakes, because there's no model driving it. The pipeline still uses the
local LLM *inside* `apply` for screening answers, which degrades gracefully.

**2. Agent + skill mode (for interactive/manual use).** [SKILL.md](SKILL.md)
teaches a Hermes agent to drive the CLI conversationally ("run the job
applier"). Fine when you're present; not recommended for cron (see below).

> **Why no_agent for cron:** an LLM-driven scheduled run failed two ways in
> practice — the agent's own inference call to a cold local model returned a
> connection error, and on another run the model chose a tool that unattended
> cron mode blocks. The routine is deterministic (five CLI steps); driving it
> with an LLM only adds failure modes.

## Setup — no_agent script mode

Deploy both wrappers to the active profile's scripts directory. Hermes 0.21+
stores the default profile directly under `HERMES_HOME`; named profiles use
`HERMES_HOME/profiles/<profile>`:

```bash
cp hermes/job-applier-routine.py hermes/job-applier-monitor.py \
   "$LOCALAPPDATA/hermes/scripts/"
```

> **Use the `.py` wrapper, not the `.sh` one.** Hermes picks a cron
> script's interpreter by extension, and for `.sh` it runs
> `shutil.which("bash")`. The gateway starts at boot with the persisted
> machine PATH, which on Windows often has no Git bash directory
> (`Git/cmd` ships `git.exe` but not `bash.exe`), so `bash` resolves to
> the WSL launcher at `C:/Windows/system32/bash.exe`. WSL's bash strips
> the backslashes out of the Windows script path and exits 127:
> `/bin/bash: C:Users<user>AppDataLocalhermes...: No such file or directory`.
> The `.py` branch runs the script with Hermes's own interpreter and
> never consults PATH. `job-applier-routine.sh` is kept for reference only.

Create the job, then flip it to no_agent script mode in the profile's
`cron/jobs.json`:

```jsonc
{
  "id": "<job-id>",
  "schedule": { "kind": "cron", "expr": "0 9,18 * * *" },
  "no_agent": true,
  "script": "job-applier-routine.py",
  "workdir": "E:/claude-projects/infra/job-applier",
  "provider": null, "model": null, "base_url": null,   // no agent LLM needed
  "delivery": "telegram"
}
```

Hermes delivers the script's stdout to Telegram; if the script exits non-zero
it delivers a "⚠ watchdog failed" alert instead — so a broken run is never
silent. Script timeout is 3600s (ample for three-platform discovery).

Set `deliver` to an **explicit** target (`"telegram:<chat-id>"`); a bare
`"telegram"` fails with "no delivery target resolved".

The watchdog alert is the only signal a broken run produces, so if runs
go quiet, check the job's `last_status` and `last_error` in the profile's
`cron/jobs.json`.

## Nightly watchdog

A second no_agent job runs at 21:00, three hours after the evening applier run,
and reports to the same Telegram target:

```bash
hermes cron create "0 21 * * *" --name "Job applier nightly watchdog"   --script job-applier-monitor.py --no-agent   --deliver "telegram:<chat-id>" --workdir /path/to/job-applier
```

It exists because a broken cron is otherwise silent — an earlier outage ran for
nine days unnoticed. [job-applier-monitor.py](job-applier-monitor.py) checks the
applier job's last status, consecutive failures, staleness (no run in 26h),
delivery errors, whether Ollama actually has the configured model, and today's
failed applications.

It auto-repairs only drift with a single known-correct fix — job disabled or
paused, `script` pointing at the wrong file, wrapper missing from the scripts
dir (redeployed from this directory). Everything else is reported with a
diagnosis rather than guessed at. It never touches the review queue, never
approves or submits, and never exits non-zero.

## Agent + skill mode

Deploy [SKILL.md](SKILL.md) to the active profile's skills directory:

```bash
cp hermes/SKILL.md "$LOCALAPPDATA/hermes/skills/productivity/job-applier/SKILL.md"
```

Then say "run the job applier" in a Hermes session.

## Operational notes (hard-won)

- **Scripts live in the active profile's directory.** For the Hermes 0.21+
  default profile this is `HERMES_HOME/scripts/`; named profiles use
  `HERMES_HOME/profiles/<profile>/scripts/`.
- **Telegram must be configured on the active profile** (`hermes gateway
  setup`), or delivery fails with "no delivery target resolved" and reports
  only land in `cron/output/<job-id>/*.md`. Test cheaply
  with `hermes send -t telegram "test"`.
- **Bash paths only.** Hermes runs `.sh` scripts and its terminal tool with
  bash even on Windows — use forward slashes.
- Trigger a manual run with `hermes cron run <job-id>`; read the result in
  `cron/output/<job-id>/*.md`.
- For **agent mode**: unpinned agent cron jobs are skipped when the global
  inference config drifts (spend guard), so pin `provider`/`model`. no_agent
  mode sidesteps this entirely.
