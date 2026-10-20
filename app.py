"""Job Applier CLI.

  python app.py login <platform>     one-time manual sign-in (saved to profiles/)
  python app.py discover <platform>  scrape matching jobs into the queue
  python app.py discover linkedin --today-hiring  today's "Hiring" listings
  python app.py prepare              draft answers for discovered jobs
  python app.py review               open the approval dashboard
  python app.py apply <platform>     submit APPROVED applications (paced, capped)
  python app.py status               queue counts
  python app.py doctor               per-platform health canary (read-only)
  python app.py routine              deterministic one-shot daily run (for cron)
  python app.py export               write applied jobs to data/applied_jobs.csv
  python app.py sync-sheet           append new applied jobs to the Google Sheet
  python app.py job-board            rebuild the detailed Excel job board
  python app.py retire-stale         retire pending listings older than 14 days
  python app.py patterns             apply success and interview rate breakdowns
  python app.py followups            applications due a follow-up (7+ days)
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


def cmd_discover(platform: str, today_hiring: bool = False) -> None:
    config = load_config()
    if today_hiring:
        if platform != "linkedin":
            sys.exit("--today-hiring is only supported for LinkedIn")
        from core.discovery import linkedin_today_hiring_config
        config = linkedin_today_hiring_config(config)
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
    label = "LinkedIn today/Hiring" if today_hiring else platform
    print(f"{label}: discovered {len(jobs)} jobs, {new} new -> queue ({queue.db_path})")


AUTO_NOTE = "auto-approved (unreviewed)"


def cmd_auto_approve(dry_run: bool = False) -> None:
    """Promote strong pending matches straight to APPROVED, skipping review.

    This is the one path that submits applications no human has read, so every
    rail is enforced here rather than trusted to the caller:

      * score >= the threshold for that job's platform: auto_apply.min_score,
        overridden per platform by auto_apply.min_score_by_platform. Per
        platform because ban risk is not uniform -- LinkedIn is the account
        worth protecting, so its bar stays high while a safer board can run
        lower without dragging LinkedIn volume up with it.
      * no unanswered screening questions -- never guess on a real application
      * no posting-guard flag (text aimed at an AI, tampered characters)
        and no LLM-rescued title: those always get a human read
      * easy_apply only when require_easy_apply (external ATS just fails)
      * auto_apply.daily_max unreviewed promotions per day, counted from the
        notes marker, on top of the normal per-platform caps in safety.py

    Disabled by default. Marks every row it touches so the dashboard and the
    DB both show what went out unreviewed.
    """
    import json as _json

    from core.matching import score_job
    from core.posting_guard import FLAG as POSTING_GUARD_FLAG
    from core.title_rescue import FLAG as RESCUE_FLAG

    cfg = (load_config().get("auto_apply") or {})
    if not cfg.get("enabled"):
        print("auto-apply disabled (config.yaml auto_apply.enabled)")
        return

    min_score = int(cfg.get("min_score", 8))
    by_platform = {str(k).lower(): int(v)
                   for k, v in (cfg.get("min_score_by_platform") or {}).items()}
    daily_max = int(cfg.get("daily_max", 10))
    need_easy = bool(cfg.get("require_easy_apply", True))

    def threshold_for(platform: str) -> int:
        return by_platform.get(platform.lower(), min_score)

    queue = Queue()
    spec = load_profile().get("job_spec", {})
    used = queue.auto_approved_today(AUTO_NOTE)
    budget = max(0, daily_max - used)

    promoted, held = [], []
    for item in queue.items(State.PENDING_REVIEW):
        job, app = item.job, item.application
        score = score_job(job, spec).score
        try:
            unanswered = _json.loads(app.unanswered or "[]")
        except Exception:
            unanswered = [app.unanswered] if app.unanswered else []

        if score < threshold_for(job.platform):
            continue
        if unanswered:
            held.append((job.title, f"{len(unanswered)} unanswered question(s)"))
            continue
        if POSTING_GUARD_FLAG in app.flags:
            # Text aimed at an AI, or tampered characters: a human reads it.
            held.append((job.title, "posting guard flag"))
            continue
        if RESCUE_FLAG in app.flags:
            held.append((job.title, "title rescued by LLM"))
            continue
        if need_easy and not job.easy_apply:
            held.append((job.title, "not easy-apply"))
            continue
        if len(promoted) >= budget:
            held.append((job.title, f"daily_max {daily_max} reached"))
            continue
        promoted.append((item.app_id, job.platform, job.title, job.company, score))

    label = "WOULD auto-approve" if dry_run else "auto-approved"
    bars = ", ".join(f"{p}>={s}" for p, s in sorted(by_platform.items()))
    bar_text = f"score >= {min_score}" + (f" ({bars})" if bars else "")
    print(f"{label} {len(promoted)} ({bar_text}, "
          f"{used}/{daily_max} of today's unreviewed budget already used)")
    for _, platform, title, company, score in promoted:
        print(f"  score {score}  [{platform}] {title} - {company}")
    for title, why in held[:5]:
        print(f"  held: {title[:44]} ({why})")

    if dry_run or not promoted:
        return
    for app_id, _, _, _, score in promoted:
        queue.set_state(app_id, State.APPROVED, f"{AUTO_NOTE} score {score}")
        # The note is for humans and gets overwritten by later set_state calls
        # (failure reasons, retries). This column is the durable record and is
        # what the daily_max budget counts.
        queue.mark_auto_approved(app_id, score)
    print(f"{len(promoted)} promoted to approved — these will submit "
          "on the next apply step with no human review")


def cmd_referrals(dry_run: bool = False, discover_only: bool = False,
                  send_only: bool = False) -> None:
    """Find 1st-degree connections at target companies and ask for a referral.

    Discovery is read-only. Sending is gated on networking.enabled, capped by
    networking.daily_max, and never messages the same person twice.
    """
    from core import referrals as R

    cfg = (load_config().get("networking") or {})
    queue = Queue()
    profile = load_profile()
    adapter = get_adapter("linkedin")

    con = R.connect(queue.db_path)
    already = R.sent_today(con)
    cap = int(cfg.get("daily_max", 5))
    con.close()

    _, page = adapter.open_browser(headless=False)
    try:
        if not adapter.is_logged_in(page):
            sys.exit("Not logged in to linkedin. Run: python app.py login linkedin")
        if send_only:
            # Skip the ~2 page loads per company that discovery costs. Sending
            # to an already-drafted list needs none of it, and this is the
            # account the user asked to protect.
            print("send-only: skipping discovery")
        else:
            stats = R.discover(page, queue.db_path, profile,
                               companies=int(cfg.get("companies_per_run", 10)))
            print(f"searched {stats['companies']} companies, "
                  f"{stats['skipped_unresolved']} unresolved (skipped), "
                  f"{stats['found']} new contacts, "
                  f"{stats['already_known']} already known")

        con = R.connect(queue.db_path)
        try:
            queued = R.pending(con, cap)
            if dry_run or discover_only or not cfg.get("enabled"):
                why = ("dry run" if dry_run else
                       "discover only" if discover_only else
                       "networking.enabled is false")
                print()
                print(f"NOT SENDING ({why}). {len(queued)} drafted and waiting:")
                for row in queued:
                    print(f"  - {row['name']} ({row['company']}) {row['profile_url']}")
                if queued:
                    print()
                    print("--- example draft ---")
                    print(queued[0]["message"])
                return

            budget = max(0, cap - already)
            print()
            print(f"sending up to {budget} ({already}/{cap} already sent today)")
            sent = 0
            for row in queued[:budget]:
                ok, note = adapter.send_message(page, row["profile_url"],
                                                row["message"], name=row["name"])
                R.mark(con, row["id"], "sent" if ok else "failed", note)
                print(f"  {'sent' if ok else 'FAILED'}: {row['name']} "
                      f"({row['company']}) - {note}")
                sent += 1 if ok else 0
                Limiter(load_config(), queue).application_delay()
            print(f"{sent} referral request(s) sent")
        finally:
            con.close()
    finally:
        adapter.close_browser()


def cmd_prepare() -> None:
    """Score DISCOVERED jobs against job_spec, draft answers for matches.

    Non-matching jobs are auto-skipped with the scoring reason recorded.
    Questions we can't answer from profile.yaml are flagged, not guessed.
    Near-miss titles get one batched local-LLM second look (title rescue).
    """
    from core.matching import RESCUED_TITLE_POINTS, is_match
    from core.title_rescue import FLAG as RESCUE_FLAG

    profile = load_profile()
    spec = profile.get("job_spec", {})
    queue = Queue()
    items = queue.items(State.DISCOVERED)
    if not items:
        print("Nothing to prepare. Run discover first.")
        return
    results = {item.app_id: is_match(item.job, spec) for item in items}
    near_miss = [item.job.title for item in items
                 if not results[item.app_id][0]
                 and "title not in target_titles" in results[item.app_id][1].reasons
                 and results[item.app_id][1].score + RESCUED_TITLE_POINTS
                 >= int(spec.get("min_score", 4))]
    rescued = _rescue_titles(near_miss, spec.get("target_titles", []))
    if near_miss:
        print(f"Title rescue: {len(rescued)} of {len(set(near_miss))} near-miss"
              " title(s) rescued for review")
    skipped = 0
    for item in items:
        ok, result = results[item.app_id]
        if not ok and item.job.title in rescued:
            ok, result = is_match(item.job, spec, title_rescued=True)
            if ok:
                queue.add_flag(item.app_id, f"{RESCUE_FLAG} - not in"
                               " target_titles; never auto-approved")
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


def _rescue_titles(titles: list[str], targets: list[str]) -> set[str]:
    """Run core.title_rescue against the configured local LLM, if enabled."""
    from core.title_rescue import rescue_titles
    from llm.answers import _llm_cfg
    from llm.client import chat

    cfg = _llm_cfg()
    if not titles or not cfg["enabled"]:
        return set()
    return rescue_titles(titles, targets, lambda system, user: chat(
        cfg["base_url"], cfg["model"], system, user))


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
            elif result.expired:
                # Not a tool failure: no debug capture, no retry.
                queue.set_state(item.app_id, State.EXPIRED, notes=result.note)
                print(f"expired: {result.note}")
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

    # Config drift: a search keyword no target_title can match means we pay
    # full browser cost discovering those jobs and then auto-skip every one.
    from core.profile import load_profile, unmapped_search_keywords
    unmapped = unmapped_search_keywords(config, load_profile())
    if unmapped:
        failures.append(
            f"search keywords with no matching target_title: {unmapped} — "
            "every result for them is discovered then auto-skipped; add a "
            "target_titles entry in profile.yaml")
        print(f"  config: DRIFT ({len(unmapped)} unmatched keyword(s))")
    else:
        print("  config: OK (every search keyword maps to a target title)")

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


def cmd_sync_sheet() -> None:
    """Append not-yet-synced applied jobs to the configured Google Sheet."""
    from core.sheets import sync_applied
    print(sync_applied(load_config()))


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


STALE_PENDING_DAYS = 14


def cmd_retire_stale(days: int = STALE_PENDING_DAYS) -> None:
    """Retire old pending-review listings; never touch approved items."""
    retired = Queue().retire_stale_pending(days)
    print(f"Retired {retired} pending listing(s) older than {days} days as expired.")


def cmd_patterns() -> None:
    """Where approvals turn into applications, and applications into interviews."""
    from core.patterns import pattern_report
    spec = load_profile().get("job_spec", {})
    print("\n".join(pattern_report(Queue(), spec)))


def cmd_followups() -> None:
    """Applications with no reply after a week, oldest first."""
    from core.followups import COLD_DAYS, FOLLOW_UP_DAYS, follow_ups
    due, cold = follow_ups(Queue())
    print(f"{len(due)} follow-up(s) due ({FOLLOW_UP_DAYS}-{COLD_DAYS - 1} days, no"
          f" reply); {cold} gone cold ({COLD_DAYS}+ days).")
    for f in due[:25]:
        print(f"  {f.days:>2}d  [{f.platform}] {f.title} - {f.company}\n       {f.url}")
    if len(due) > 25:
        print(f"  ... and {len(due) - 25} more (see the Tracker sheet)")
    if due:
        print("Tick 'Followed up' in the Tracker sheet once sent; the next"
              " board rebuild records it.")


def cmd_job_board() -> None:
    """Rebuild the local Excel kanban board from the complete queue."""
    from core.job_board import BOARD_PATH, build_job_board
    manifest = build_job_board()
    print(f"Excel job board updated: {manifest['total_jobs']} jobs -> {BOARD_PATH}")


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

    # Keep the human review queue useful without ever making approval choices.
    retired = queue.retire_stale_pending(STALE_PENDING_DAYS)
    if retired:
        report.append(
            f"Retired {retired} pending listing(s) older than "
            f"{STALE_PENDING_DAYS} days as expired.")

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
        try:
            from core.job_board import build_job_board
            board = build_job_board(queue)
            report.append(f"Excel board updated: {board['total_jobs']} jobs")
        except Exception as e:  # report the maintenance failure, never hide it
            report.append(f"[!] Excel board update failed: {type(e).__name__}: {e}")
        print("\n".join(report))
        return

    # 2. Discover on each healthy platform (sequential; own browser each).
    for platform in healthy:
        args = ["discover", platform]
        if platform == "linkedin":
            args.append("--today-hiring")
        code, out = _run_step(args, timeout=1200)
        if platform == "linkedin":
            summary = next((line.strip() for line in out.splitlines()
                            if line.strip().startswith("LinkedIn today/Hiring:")), "")
            if summary:
                report.append(summary)
            elif code:
                report.append("[!] LinkedIn today/Hiring search failed")

    # 3. Score + draft answers.
    _run_step(["prepare"], timeout=600)

    # 3b. Promote strong matches straight past review, when auto_apply is on.
    # Listings expire within weeks, so waiting for the next run to submit an
    # already-good match costs real applications. No-op while disabled.
    _, auto_out = _run_step(["auto-approve"], timeout=300)
    for line in auto_out.splitlines():
        if line.startswith("auto-approved ") and " 0 (" not in line:
            report.append(f"[auto] {line.strip()}")

    # 4. Submit anything the user already approved (caps enforced inside apply).
    applied_lines: list[str] = []
    for platform in ALL_PLATFORMS:
        approved = queue.items(State.APPROVED, platform=platform)
        if not approved:
            continue
        _, out = _run_step(["apply", platform], timeout=1800)
        for line in out.splitlines():
            if ("applied ✓" in line or "failed:" in line or "will retry" in line
                    or "expired:" in line):
                clean = line.strip().lstrip("→ ").strip().replace("✓", "(applied)")
                applied_lines.append(f"  {clean}")

    # 5. Append any new applications to the Google Sheet (no-op if unconfigured).
    from core.sheets import sync_applied
    sheet_status = sync_applied(load_config(), queue)
    if "disabled" not in sheet_status and "no webhook" not in sheet_status:
        report.append(sheet_status)

    # 6. Rebuild the complete local Excel board after discovery and apply.
    try:
        from core.job_board import build_job_board
        board = build_job_board(queue)
        report.append(f"Excel board updated: {board['total_jobs']} jobs")
    except Exception as e:  # the Telegram report must make this visible
        report.append(f"[!] Excel board update failed: {type(e).__name__}: {e}")

    # 7. Compose report from the queue's own truth.
    counts = queue.counts()
    pending = counts.get(State.PENDING_REVIEW.value, 0)
    approved_total = counts.get(State.APPROVED.value, 0)
    applied_total = counts.get(State.APPLIED.value, 0)
    report.append(f"Platforms run: {', '.join(healthy)}")
    if applied_lines:
        report.append("Submitted this run:")
        report.extend(applied_lines)
    else:
        report.append("No approved items to submit (approve some in the dashboard).")
    report.append(
        f"{pending} pending review | {approved_total} approved | "
        f"{applied_total} applied all-time")
    # What came back. Without this the report only ever says how much we sent,
    # never whether any of it worked.
    outcomes = queue.outcome_counts()
    if outcomes:
        report.append("Outcomes: " + ", ".join(
            f"{n} {name}" for name, n in outcomes.items()))
    from core.followups import follow_ups
    due, cold = follow_ups(queue)
    if due:
        report.append(f"Follow-ups due: {len(due)} (oldest {due[0].days}d) -"
                      f" python app.py followups")
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
        if name == "discover":
            sp.add_argument("--today-hiring", action="store_true",
                            help="LinkedIn only: keyword Hiring, posted in last 24h")
        if name == "apply":
            sp.add_argument("--limit", type=int, default=None,
                            help="apply to at most N approved jobs this run")
    sub.add_parser("prepare")
    ref = sub.add_parser("referrals")
    ref.add_argument("--dry-run", action="store_true",
                     help="find and draft, send nothing")
    ref.add_argument("--discover-only", action="store_true",
                     help="alias for --dry-run")
    ref.add_argument("--send-only", action="store_true",
                     help="send already-drafted requests, skip discovery")
    auto = sub.add_parser("auto-approve")
    auto.add_argument("--dry-run", action="store_true",
                      help="show what would be submitted unreviewed, change nothing")
    sub.add_parser("review")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("routine")
    sub.add_parser("export")
    sub.add_parser("sync-sheet")
    stale = sub.add_parser("retire-stale")
    stale.add_argument("--days", type=int, default=STALE_PENDING_DAYS)
    sub.add_parser("job-board")
    sub.add_parser("patterns")
    sub.add_parser("followups")
    args = p.parse_args()

    if args.cmd == "login":
        cmd_login(args.platform)
    elif args.cmd == "discover":
        cmd_discover(args.platform, today_hiring=args.today_hiring)
    elif args.cmd == "referrals":
        cmd_referrals(dry_run=args.dry_run,
                      discover_only=args.discover_only,
                      send_only=args.send_only)
    elif args.cmd == "auto-approve":
        cmd_auto_approve(dry_run=args.dry_run)
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
    elif args.cmd == "sync-sheet":
        cmd_sync_sheet()
    elif args.cmd == "retire-stale":
        cmd_retire_stale(args.days)
    elif args.cmd == "job-board":
        cmd_job_board()
    elif args.cmd == "patterns":
        cmd_patterns()
    elif args.cmd == "followups":
        cmd_followups()


if __name__ == "__main__":
    main()
