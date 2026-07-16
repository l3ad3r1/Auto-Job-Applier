# Hermes offload

The daily routine is offloaded to the local hermes-agent via a skill + cron job.

## Deploy the skill

The canonical copy is [SKILL.md](SKILL.md). Hermes reads skills from the
**active profile's** skills dir — deploy to BOTH locations and keep them in sync:

```bash
cp hermes/SKILL.md "$LOCALAPPDATA/hermes/skills/productivity/job-applier/SKILL.md"
cp hermes/SKILL.md "$LOCALAPPDATA/hermes/profiles/project-manager/skills/productivity/job-applier/SKILL.md"
```

## Cron job

Job `<job-id>` "Job applier twice-daily", schedule `0 9,18 * * *`,
delivery `telegram` (DM). Created with:

```bash
hermes cron create "0 9,18 * * *" "<routine prompt>" --name "Job applier twice-daily" --deliver telegram
```

Hard-won operational notes:

- **Pin the model.** Unpinned cron jobs are skipped when the global inference
  config drifts (spend guard). This job is pinned in the profile's
  `cron/jobs.json` to `provider: custom`, `model: gemma4:latest`,
  `base_url: http://localhost:11434/v1` (local Ollama).
- **Bash paths only.** Hermes's terminal tool is bash even on Windows —
  skill commands must use forward slashes.
- **Telegram must be configured on the active profile** (`hermes gateway
  setup`), or delivery fails with "no delivery target resolved" and reports
  only land in `profiles/<profile>/cron/output/<job-id>/*.md`.
- Test delivery cheaply with `hermes send -t telegram "test"`.
- Trigger a manual run with `hermes cron run <job-id>`.
