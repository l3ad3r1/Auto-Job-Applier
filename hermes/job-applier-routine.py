#!/usr/bin/env python3
"""Hermes no_agent cron wrapper for the Auto Job Applier.

Deterministic -- no LLM orchestration. Its stdout is delivered to Telegram.
The real logic lives in `app.py routine` (health-gated discover -> prepare ->
apply approved), which always prints a report and exits 0.

Python, not bash, on purpose. Hermes runs .sh cron scripts via
`shutil.which("bash")`. On this machine the persisted machine PATH has no Git
bash directory (Git/cmd holds git.exe but not bash.exe), so `bash` resolves to
the WSL launcher at C:/Windows/system32/bash.exe. WSL's bash strips the
backslashes out of the Windows script path and dies with exit 127:

    /bin/bash: C:Users<user>AppDataLocalhermes...: No such file or directory

The scheduler's .py branch runs scripts with Hermes's own interpreter and never
consults PATH, so this wrapper is immune to which-bash-wins. Keep it as .py.

Output is ASCII-only: emoji mojibake through the Python -> Hermes -> Telegram
chain (app.py routine coerces its own report the same way).
"""

import subprocess
import sys
from pathlib import Path

REPO = Path("E:/claude-projects/infra/job-applier")
VENV_PYTHON = REPO / ".venv" / "Scripts" / "python.exe"

# Hermes kills the script at 3600s; bail a little earlier so the timeout is
# reported as a readable line in Telegram instead of a bare watchdog alert.
TIMEOUT_SECONDS = 3300


def main() -> int:
    if not REPO.is_dir():
        print(f"Job Applier: repo not found at {REPO}")
        return 0
    if not VENV_PYTHON.exists():
        print(f"Job Applier: venv interpreter missing at {VENV_PYTHON}")
        return 0

    try:
        proc = subprocess.run(
            [str(VENV_PYTHON), "app.py", "routine"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"Job Applier: routine timed out after {TIMEOUT_SECONDS}s.")
        return 0

    report = ((proc.stdout or "") + (proc.stderr or "")).strip()
    print(report if report else "Job Applier: routine produced no output.")
    if proc.returncode != 0:
        print(f"(routine exited with code {proc.returncode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
