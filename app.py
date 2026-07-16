"""Job Applier CLI.

  python app.py login <platform>     one-time manual sign-in (saved to profiles/)
  python app.py discover <platform>  scrape matching jobs into the queue
  python app.py prepare              draft answers for discovered jobs
  python app.py review               open the approval dashboard
  python app.py apply <platform>     submit APPROVED applications (paced, capped)
  python app.py status               queue counts
"""
from __future__ import annotations

import argparse
import sys

from adapters import get_adapter
from core.models import State
from core.profile import load_config, load_profile, match_answer
from core.queue import Queue
from safety import CapReached, Limiter


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
                queue.set_state(item.app_id, State.FAILED, notes=result.note)
                print(f"failed: {result.note}")
            limiter.application_delay()
    finally:
        adapter.close_browser()


def cmd_status() -> None:
    for state, n in sorted(Queue().counts().items()):
        print(f"  {state:>15}: {n}")


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


if __name__ == "__main__":
    main()
