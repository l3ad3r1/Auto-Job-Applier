"""Job Applier CLI.

  python app.py login <platform>     one-time manual sign-in (saved to profiles/)
  python app.py discover <platform>  scrape matching jobs into the queue
  python app.py prepare              draft answers for discovered jobs
  python app.py review               open the approval dashboard
  python app.py apply <platform>     submit APPROVED applications (paced, capped)
  python app.py status               queue counts
  python app.py doctor               per-platform health canary (read-only)
  python app.py routine              deterministic one-shot daily run (for cron)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from adapters import get_adapter
from core.models import State
from core.profile import load_config, load_profile, match_answer
from core.queue import Queue
from safety import CapReached, Limiter

ROOT = Path(__file__).resolve().parent


def cmd_login(platform: str) -> None:
    get_adapter(platform).interactive_login()


def cmd_discover(platform: str) -> None:
    config = load_config()
    adapter = get_adapter(platform)
    queue = Queue()
    _, page = adapter.open_browser(headless=False)
    try:
        if not adapter.is_logged_in(page):
            sys.exit(f"Not logged in to {platform}. Run: python app.py login {platform}")
        jobs = adapter.search(page, config)
    finally:
        adapter.close_browser()
    new = sum(1 for j in jobs if queue.add_job(j) is not None)
    print(f"Discovered {len(jobs)} jobs, {new} new → queue ({queue.db_path})")


def cmd_prepare() -> None:
    """Score DISCOVERED jobs against job_spec, draft answers for matches.

    Non-matching jobs are auto-skipped with the scoring reason recorded.
    Questions we can't answer from profile.yaml are flagged, not guessed.
    """
    from core.matching import is_match

    profile = load_profile()
    spec = profile.get("job_spec", {})
    queue = Queue()
    items = queue.items(State.DISCOVERED)
    if not items:
        print("Nothing to prepare. Run discover first.")
        return
    skipped = 0
    for item in items:
        ok, result = is_match(item.job, spec)
        if not ok:
            queue.set_state(item.app_id, State.SKIPPED, notes=result.summary)
            skipped += 1
            continue
        # Phase 1 heuristic: common Easy Apply questions; the real form may add
        # more — the apply step aborts safely on anything unanswered.
        canned = ["years of experience", "notice period", "current ctc",
                  "expected ctc", "willing to relocate", "work authorization"]
        answers, unanswered = {}, []
        for question in canned:
            a = match_answer(profile, question)
            if a is not None:
                answers[question] = a
        queue.save_preparation(item.app_id, answers, unanswered,
                               profile.get("resume_path", ""))
    print(f"Prepared {len(items) - skipped} matching applications → pending review "
          f"({skipped} skipped as non-matching). Run: python app.py review")


def cmd_review() -> None:
    import uvicorn
    from dashboard.app import app as dash
    cfg = load_config().get("dashboard", {})
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8377))
    print(f"Dashboard: http://{host}:{port}")
    uvicorn.run(dash, host=host, port=port, log_level="warning")


def cmd_apply(platform: str, limit: int | None = None) -> None:
    config = load_config()
    queue = Queue()
    limiter = Limiter(config, queue)
    items = queue.items(State.APPROVED, platform=platform)
    if not items:
        print("No approved applications. Approve some in the dashboard first.")
        return
    if limit:
        items = items[:limit]
    print(f"{len(items)} approved; {limiter.remaining(platform)} left under today's cap.")

    adapter = get_adapter(platform)
    _, page = adapter.open_browser(headless=False)
    try:
        if not adapter.is_logged_in(page):
            sys.exit(f"Not logged in. Run: python app.py login {platform}")
        for item in items:
            try:
                limiter.check_cap(platform)
            except CapReached as e:
                print(f"STOP: {e}")
                break
            print(f"→ {item.job.title} @ {item.job.company} ... ", end="", flush=True)
            try:
                result = adapter.apply(page, item, limiter)
            except Exception as e:  # one bad job must not kill the batch
                from adapters.base import ApplyResult
                result = ApplyResult(
                    False, f"adapter error: {type(e).__name__}: {str(e)[:120]}")
            if result.ok:
                queue.set_state(item.app_id, State.APPLIED)
                queue.log_submission(platform, item.app_id)
                print("applied ✓")
            else:
                shot = adapter.capture_debug(page, f"app{item.app_id}")
                note = f"{result.note} [debug: {shot}]"
                if _is_transient(result.note) and queue.retry_count(item.app_id) < 2:
                    queue.mark_for_retry(item.app_id, f"transient, will retry: {note}")
                    print(f"failed (will retry next run): {result.note}")
                else:
                    queue.set_state(item.app_id, State.FAILED, notes=note)
                    print(f"failed: {result.note}")
            limiter.application_delay()
    finally:
        adapter.close_browser()


def _is_transient(note: str) -> bool:
    """Failures worth an automatic retry on the next run (vs. real blocks
    like unanswered questions or external-apply jobs)."""
    markers = ("TimeoutError", "adapter error", "no confirmation",
               "never became visible", "never became ready",
               "did not reach submission", "did not reach Submit")
    return any(m in note for m in markers)


def cmd_doctor() -> None:
    """Per-platform health canary: session alive? does search render cards?

    Read-only — never applies. Exit code 1 if any platform is unhealthy,
    so schedulers/agents can alert on it.
    """
    from adapters import ADAPTERS

    config = load_config()
    cfg = config.get("search", {})
    keyword = (cfg.get("keywords") or ["manager"])[0]
    location = (cfg.get("locations") or ["India"])[0]
    failures = []

    # Local LLM reachability (non-fatal: pipeline degrades to flagging)
    llm_cfg = config.get("llm", {})
    if llm_cfg.get("enabled"):
        from llm.client import LLMError, chat
        try:
            chat(llm_cfg.get("base_url", "http://localhost:11434/v1"),
                 llm_cfg.get("model", "gemma4:latest"),
                 "Reply with exactly: ok", "ping", timeout=60)
            print("  llm: OK")
        except LLMError as e:
            print(f"  llm: DEGRADED ({e}) — unmatched questions will be flagged")

    for platform in ADAPTERS:
        adapter = get_adapter(platform)
        try:
            _, page = adapter.open_browser(headless=False)
            try:
                if not adapter.is_logged_in(page):
                    failures.append(f"{platform}: session expired — run:"
                                    f" python app.py login {platform}")
                    print(f"  {platform}: LOGIN NEEDED")
                    continue
                jobs = adapter.search(page, {
                    "search": {**cfg, "keywords": [keyword],
                               "locations": [location]},
                    "limits": {"discover_batch": 5},
                })
                if jobs:
                    print(f"  {platform}: OK ({len(jobs)} cards parsed)")
                else:
                    failures.append(f"{platform}: search rendered 0 parseable"
                                    " cards — selectors may have rotted")
                    adapter.capture_debug(page, "doctor")
                    print(f"  {platform}: NO CARDS (debug captured)")
            finally:
                adapter.close_browser()
        except Exception as e:
            failures.append(f"{platform}: {type(e).__name__}: {str(e)[:100]}")
            print(f"  {platform}: ERROR {type(e).__name__}")

    if failures:
        print("\nUNHEALTHY:")
        for f in failures:
            print(f"  ! {f}")
        sys.exit(1)
    print("\nAll platforms healthy.")


def cmd_status() -> None:
    for state, n in sorted(Queue().counts().items()):
        print(f"  {state:>15}: {n}")


APPLIED_COLUMNS = ["Date Applied", "Platform", "Title", "Company",
                   "Location", "Salary", "URL"]


def _applied_rows() -> list[list[str]]:
    q = Queue()
    rows = []
    for i in sorted(q.items(State.APPLIED), key=lambda x: x.application.updated_at):
        rows.append([
            i.application.updated_at[:10], i.job.platform, i.job.title,
            i.job.company, i.job.location, i.job.salary or "", i.job.url,
        ])
    return rows


def cmd_export() -> None:
    """Write applied jobs to data/applied_jobs.csv (for Sheets/Excel import)."""
    import csv
    out = ROOT / "data" / "applied_jobs.csv"
    rows = _applied_rows()
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(APPLIED_COLUMNS)
        w.writerows(rows)
    print(f"Exported {len(rows)} applied jobs -> {out}")


ALL_PLATFORMS = ("linkedin", "naukri", "indeed")


def _run_step(args: list[str], timeout: int) -> tuple[int, str]:
    """Run an app.py subcommand as an isolated subprocess.

    Isolation matters: a step that calls sys.exit (e.g. 'not logged in')
    or crashes its browser must not take down the whole routine.
    """
    try:
        p = subprocess.run([sys.executable, "app.py", *args], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, f"step {' '.join(args)} timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001 — routine must never raise
        return 1, f"step {' '.join(args)} error: {type(e).__name__}: {e}"


def cmd_routine() -> None:
    """Deterministic one-shot daily routine for unattended (cron) use.

    doctor → discover healthy platforms → prepare → apply approved. Prints a
    concise Telegram-friendly report to stdout and always exits 0 — no LLM
    orchestration, so nothing here can hang or refuse. Screening answers still
    use the local LLM inside `apply`, which degrades gracefully to flagging.
    """
    queue = Queue()
    # ASCII-only report — it traverses Python -> bash -> Hermes -> Telegram;
    # non-ASCII risks mojibake somewhere in that chain.
    report: list[str] = ["Job Applier - daily run"]

    # 1. Health canary — skip unhealthy platforms rather than abort the run.
    _, doc_out = _run_step(["doctor"], timeout=900)
    healthy = [p for p in ALL_PLATFORMS if f"{p}: OK" in doc_out]
    unhealthy = [p for p in ALL_PLATFORMS if p not in healthy]
    if unhealthy:
        for line in doc_out.splitlines():
            if line.strip().startswith("!"):
                report.append(f"[!] {line.strip().lstrip('! ').strip()}")
    if "llm: DEGRADED" in doc_out:
        report.append("[!] local LLM unreachable - unmatched questions flagged")
    if not healthy:
        report.append("[X] No healthy platforms this run. Check sessions/selectors.")
        print("\n".join(report))
        return

    # 2. Discover on each healthy platform (sequential; own browser each).
    for platform in healthy:
        _run_step(["discover", platform], timeout=1200)

    # 3. Score + draft answers.
    _run_step(["prepare"], timeout=600)

    # 4. Submit anything the user already approved (caps enforced inside apply).
    applied_lines: list[str] = []
    for platform in ALL_PLATFORMS:
        approved = queue.items(State.APPROVED, platform=platform)
        if not approved:
            continue
        _, out = _run_step(["apply", platform], timeout=1800)
        for line in out.splitlines():
            if "applied ✓" in line or "failed:" in line or "will retry" in line:
                clean = line.strip().lstrip("→ ").strip().replace("✓", "(applied)")
                applied_lines.append(f"  {clean}")

    # 5. Compose report from the queue's own truth.
    counts = queue.counts()
    pending = counts.get(State.PENDING_REVIEW.value, 0)
    applied_total = counts.get(State.APPLIED.value, 0)
    report.append(f"Platforms run: {', '.join(healthy)}")
    if applied_lines:
        report.append("Submitted this run:")
        report.extend(applied_lines)
    else:
        report.append("No approved items to submit (approve some in the dashboard).")
    report.append(f"{pending} pending your review | {applied_total} applied all-time")
    if pending:
        report.append("Review: python app.py review -> http://127.0.0.1:8377")
    # Final safety net: guarantee ASCII regardless of captured subprocess text.
    print("\n".join(report).encode("ascii", "replace").decode("ascii"))


def main() -> None:
    # Windows consoles often default to cp1252; our output uses arrows/ticks
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("login", "discover", "apply"):
        sp = sub.add_parser(name)
        sp.add_argument("platform")
        if name == "apply":
            sp.add_argument("--limit", type=int, default=None,
                            help="apply to at most N approved jobs this run")
    sub.add_parser("prepare")
    sub.add_parser("review")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("routine")
    sub.add_parser("export")
    args = p.parse_args()

    if args.cmd == "login":
        cmd_login(args.platform)
    elif args.cmd == "discover":
        cmd_discover(args.platform)
    elif args.cmd == "prepare":
        cmd_prepare()
    elif args.cmd == "review":
        cmd_review()
    elif args.cmd == "apply":
        cmd_apply(args.platform, limit=args.limit)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "doctor":
        cmd_doctor()
    elif args.cmd == "routine":
        cmd_routine()
    elif args.cmd == "export":
        cmd_export()


if __name__ == "__main__":
    main()
