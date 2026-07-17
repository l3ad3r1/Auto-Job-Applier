#!/usr/bin/env bash
# Hermes no_agent cron wrapper for the Auto Job Applier.
# Deterministic — no LLM orchestration. Its stdout is delivered to Telegram.
# The real logic lives in `app.py routine` (health-gated discover → prepare →
# apply approved), which always prints a report and exits 0.
#
# Deploy to the ACTIVE PROFILE's scripts dir (Hermes resolves cron scripts
# there, not root HERMES_HOME):
#   cp hermes/job-applier-routine.sh \
#      "$LOCALAPPDATA/hermes/profiles/<profile>/scripts/job-applier-routine.sh"
set -o pipefail
cd "E:/claude-projects/job-applier" || {
  echo "❌ Job Applier: repo not found at E:/claude-projects/job-applier"
  exit 0
}
./.venv/Scripts/python.exe app.py routine 2>&1
exit 0
