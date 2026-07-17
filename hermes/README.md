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

Deploy the wrapper to the **active profile's** scripts dir (Hermes resolves
cron scripts there, not root `HERMES_HOME/scripts`):

```bash
cp hermes/job-applier-routine.sh \
   "$LOCALAPPDATA/hermes/profiles/<profile>/scripts/job-applier-routine.sh"
```

Create the job, then flip it to no_agent script mode in the profile's
`cron/jobs.json`:

```jsonc
{
  "id": "<job-id>",
  "schedule": { "kind": "cron", "expr": "0 9,18 * * *" },
  "no_agent": true,
  "script": "job-applier-routine.sh",
  "workdir": "E:/claude-projects/job-applier",
  "provider": null, "model": null, "base_url": null,   // no agent LLM needed
  "delivery": "telegram"
}
```

Hermes delivers the script's stdout to Telegram; if the script exits non-zero
it delivers a "⚠ watchdog failed" alert instead — so a broken run is never
silent. Script timeout is 3600s (ample for three-platform discovery).

## Agent + skill mode

Deploy [SKILL.md](SKILL.md) to the active profile's skills dir (Hermes reads
skills per-profile; keep the root copy in sync too):

```bash
cp hermes/SKILL.md "$LOCALAPPDATA/hermes/skills/productivity/job-applier/SKILL.md"
cp hermes/SKILL.md "$LOCALAPPDATA/hermes/profiles/<profile>/skills/productivity/job-applier/SKILL.md"
```

Then say "run the job applier" in a Hermes session.

## Operational notes (hard-won)

- **Scripts live in the profile's dir**, `profiles/<profile>/scripts/` — not
  root `HERMES_HOME/scripts/`. A script placed only in the root is "not found".
- **Telegram must be configured on the active profile** (`hermes gateway
  setup`), or delivery fails with "no delivery target resolved" and reports
  only land in `profiles/<profile>/cron/output/<job-id>/*.md`. Test cheaply
  with `hermes send -t telegram "test"`.
- **Bash paths only.** Hermes runs `.sh` scripts and its terminal tool with
  bash even on Windows — use forward slashes.
- Trigger a manual run with `hermes cron run <job-id>`; read the result in
  `profiles/<profile>/cron/output/<job-id>/*.md`.
- For **agent mode**: unpinned agent cron jobs are skipped when the global
  inference config drifts (spend guard), so pin `provider`/`model`. no_agent
  mode sidesteps this entirely.
