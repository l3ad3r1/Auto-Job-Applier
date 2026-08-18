#!/usr/bin/env python3
"""Nightly watchdog for the Auto Job Applier Hermes cron job.

Runs at 21:00 daily as a Hermes no_agent cron script, three hours after the
18:00 application run. Its stdout is delivered to Telegram.

Why this exists: on 2026-08-10 the applier job started failing at exit 127 and
nobody noticed for nine days, because a broken cron is silent apart from a
watchdog alert that is easy to miss. This script makes the failure loud, and
repairs the handful of breakages that have a single known-correct fix.

Deliberately deterministic - no LLM. It auto-fixes only unambiguous config
drift and reports everything else with a diagnosis instead of guessing. It
never touches the review queue, never approves or submits anything, and never
edits profile.yaml.

Python, not bash: Hermes runs .sh cron scripts through shutil.which("bash"),
which on this machine resolves to the WSL launcher and mangles the Windows
script path. See hermes/README.md.
"""

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Derived from the environment rather than hardcoded, so this file carries no
# username into the public repo and still works if HERMES_HOME moves.
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
HERMES_PROFILE = Path(_LOCALAPPDATA) / "hermes" / "profiles" / "project-manager"
JOBS_JSON = HERMES_PROFILE / "cron" / "jobs.json"
EXECUTIONS_DB = HERMES_PROFILE / "cron" / "executions.db"
SCRIPTS_DIR = HERMES_PROFILE / "scripts"

REPO = Path("E:/claude-projects/job-applier")
QUEUE_DB = REPO / "data" / "queue.db"
CONFIG = REPO / "config.yaml"

APPLIER_JOB_ID = "999b6a016167"
WRAPPER = "job-applier-routine.py"
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"

IST = timezone(timedelta(hours=5, minutes=30))

problems: list[str] = []   # needs a human
repairs: list[str] = []    # fixed automatically
notes: list[str] = []      # informational


def _load_jobs() -> dict | None:
    try:
        return json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        problems.append(f"cannot read cron jobs.json ({e})")
        return None


def _save_jobs(data: dict) -> bool:
    """Atomic write, so a concurrent scheduler tick never sees half a file."""
    try:
        tmp = JOBS_JSON.with_suffix(".json.monitor-tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, JOBS_JSON)
        return True
    except Exception as e:
        problems.append(f"could not write jobs.json ({e})")
        return False


def check_job_config() -> dict | None:
    """Inspect the applier job and repair unambiguous config drift."""
    data = _load_jobs()
    if data is None:
        return None
    job = next((j for j in data.get("jobs", []) if j.get("id") == APPLIER_JOB_ID), None)
    if job is None:
        problems.append(f"applier job {APPLIER_JOB_ID} is GONE from jobs.json")
        return None

    dirty = False

    if not job.get("enabled", False):
        job["enabled"] = True
        dirty = True
        repairs.append("job was disabled -> re-enabled")

    if job.get("state") == "paused" or job.get("paused_at"):
        job["state"] = "scheduled"
        job["paused_at"] = None
        job["paused_reason"] = None
        dirty = True
        repairs.append("job was paused -> resumed")

    script = job.get("script") or ""
    deployed = SCRIPTS_DIR / WRAPPER
    if script != WRAPPER:
        if deployed.exists():
            job["script"] = WRAPPER
            dirty = True
            repairs.append(f"script was {script!r} -> repointed to {WRAPPER}")
        else:
            problems.append(f"script is {script!r} and {deployed} is missing")
    elif not deployed.exists():
        # The wrapper is version-controlled; redeploy it rather than nag.
        source = REPO / "hermes" / WRAPPER
        if source.exists():
            try:
                deployed.parent.mkdir(parents=True, exist_ok=True)
                deployed.write_bytes(source.read_bytes())
                repairs.append(f"{WRAPPER} was missing -> redeployed from repo")
            except Exception as e:
                problems.append(f"{WRAPPER} missing and redeploy failed ({e})")
        else:
            problems.append(f"{WRAPPER} missing from both scripts dir and repo")

    if not str(job.get("deliver") or "").startswith("telegram:"):
        problems.append(f"deliver is {job.get('deliver')!r}, expected 'telegram:<chat-id>'")

    if dirty:
        _save_jobs(data)
    return job


def check_last_run(job: dict) -> None:
    """The silent-failure detector: did the applier actually run, and did it work?"""
    status = job.get("last_status")
    if status == "error":
        err = " ".join(str(job.get("last_error") or "").split())
        problems.append(f"last run FAILED: {err[:200]}")
    elif status != "ok":
        notes.append(f"last run status: {status!r}")

    if job.get("last_delivery_error"):
        problems.append(f"telegram delivery failing: {job['last_delivery_error']}")

    raw = job.get("last_run_at")
    if not raw:
        problems.append("job has never run")
        return
    try:
        last = datetime.fromisoformat(raw)
        age = datetime.now(IST) - last
        if age > timedelta(hours=26):
            problems.append(
                f"STALE: last run was {age.days}d {age.seconds // 3600}h ago "
                f"({last.strftime('%Y-%m-%d %H:%M')}), expected two runs a day")
        else:
            notes.append(f"last run {last.strftime('%d %b %H:%M')} ({status})")
    except Exception:
        notes.append(f"last_run_at unparseable: {raw!r}")


def check_recent_executions() -> None:
    """Count consecutive failures, which is what nine silent days looked like."""
    if not EXECUTIONS_DB.exists():
        return
    try:
        con = sqlite3.connect(f"file:{EXECUTIONS_DB}?mode=ro", uri=True)
        rows = list(con.execute(
            "select status from executions where job_id=? order by started_at desc limit 20",
            (APPLIER_JOB_ID,)))
        con.close()
    except Exception as e:
        notes.append(f"could not read executions.db ({e})")
        return
    streak = 0
    for (status,) in rows:
        if status in ("failed", "error"):
            streak += 1
        else:
            break
    if streak >= 2:
        problems.append(f"{streak} consecutive failed runs")


def check_llm() -> None:
    """The gemma4 breakage: config naming a model Ollama no longer has."""
    try:
        import yaml
        cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    except Exception as e:
        problems.append(f"cannot read config.yaml ({e})")
        return
    llm = cfg.get("llm", {}) or {}
    if not llm.get("enabled"):
        return
    want = str(llm.get("model", ""))
    try:
        with urllib.request.urlopen(OLLAMA_TAGS, timeout=10) as r:
            installed = [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        problems.append("Ollama unreachable on :11434 - screening answers will be flagged")
        return
    if want not in installed:
        problems.append(
            f"config wants LLM model {want!r} but Ollama has {installed} "
            "- screening answers will be flagged (pick one and update config.yaml llm.model)")


def check_queue() -> None:
    """Report the backlog and how long it takes to drain at current caps."""
    if not QUEUE_DB.exists():
        problems.append(f"queue db missing at {QUEUE_DB}")
        return
    try:
        con = sqlite3.connect(f"file:{QUEUE_DB}?mode=ro", uri=True)
        counts = dict(con.execute("select state,count(*) from applications group by state"))
        approved = dict(con.execute(
            "select j.platform,count(*) from applications a join jobs j on j.id=a.job_id "
            "where a.state='approved' group by j.platform"))
        today = datetime.now(IST).strftime("%Y-%m-%d")
        sent_today = list(con.execute(
            "select count(*) from submissions_log where ts like ?", (today + "%",)))[0][0]
        con.close()
    except Exception as e:
        problems.append(f"cannot read queue db ({e})")
        return

    notes.append(f"submitted today: {sent_today}")
    notes.append(
        f"queue: {counts.get('pending_review', 0)} pending | "
        f"{counts.get('approved', 0)} approved | {counts.get('applied', 0)} applied | "
        f"{counts.get('failed', 0)} failed")

    if not approved:
        return
    try:
        import yaml
        caps = ((yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {})
                .get("limits", {}).get("daily_applications", {}))
        days = max(-(-n // int(caps.get(p, 10))) for p, n in approved.items())
        notes.append(f"approved backlog drains in ~{days} more day(s) at current caps")
    except Exception:
        pass


def check_failed_applications() -> None:
    """Surface today's apply failures - these are what debug captures are for."""
    if not QUEUE_DB.exists():
        return
    try:
        con = sqlite3.connect(f"file:{QUEUE_DB}?mode=ro", uri=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = list(con.execute(
            "select j.platform, substr(j.title,1,40), substr(coalesce(a.notes,''),1,70) "
            "from applications a join jobs j on j.id=a.job_id "
            "where a.state='failed' and a.updated_at like ?", (today + "%",)))
        con.close()
    except Exception:
        return
    if rows:
        problems.append(f"{len(rows)} application(s) failed today:")
        for plat, title, note in rows[:5]:
            problems.append(f"    {plat}: {title} - {' '.join(str(note).split())}")


def main() -> int:
    lines = ["Job Applier - nightly watchdog"]
    job = check_job_config()
    if job:
        check_last_run(job)
        check_recent_executions()
    check_llm()
    check_queue()
    check_failed_applications()

    if repairs:
        lines.append("FIXED:")
        lines.extend(f"  - {r}" for r in repairs)
    if problems:
        lines.append("NEEDS YOU:")
        lines.extend(f"  - {p}" for p in problems)
    if not problems and not repairs:
        lines.append("All good.")
    if notes:
        lines.append("Status:")
        lines.extend(f"  - {n}" for n in notes)
    if problems:
        lines.append("Dashboard: python app.py review -> http://127.0.0.1:8377")

    # ASCII only: the report crosses Python -> Hermes -> Telegram.
    print("\n".join(lines).encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # a watchdog must never die silently
        print(f"Job Applier watchdog crashed: {type(e).__name__}: {e}")
        sys.exit(0)
