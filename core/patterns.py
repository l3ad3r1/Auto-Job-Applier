"""What is working: where approvals turn into applications, and where
applications turn into interviews.

Two sections, adapted from career-ops analyze-patterns.mjs / calibrate (MIT):

  pipeline  approved rows that ended applied / failed / expired, by platform,
            plus the most common failure reasons. Answers "where is the tool
            losing approvals?" and needs no outcome data.
  outcomes  interview/offer rate among applications with a recorded outcome,
            by platform, current match score band, auto vs manual approval
            and discovery source. Answers "does a higher score actually get
            more interviews?", which is what min_score and the auto-apply bar
            should be set from.
  skips     why discovered jobs were skipped, and the titles missed most.

A rate is printed only when a group has MIN_OUTCOMES recorded outcomes;
below that it shows "-", because 1 interview in 2 is not a 50% rate.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .matching import _title_points, score_job
from .models import State
from .queue import Queue

MIN_OUTCOMES = 5
CLOSED_NOTE = "posting closed"   # ApplyResult note from BaseAdapter.closed_posting
POSITIVE = {"interview", "offer"}
BANDS = ((80, "80+"), (70, "70-79"), (60, "60-69"), (0, "<60"))


def _band(score: int) -> str:
    return next(label for floor, label in BANDS if score >= floor)


def failure_reason(note: str) -> str:
    """Group failure notes: drop the debug path and retry prefix, and keep the
    stable part before any quoted detail ("Unanswered required question")."""
    note = re.sub(r"\s*\[debug:.*", "", note or "")
    note = note.replace("transient, will retry: ", "").strip()
    head = note.split(":")[0].strip()
    return (head if len(head) >= 10 else note[:50]) or "(no note)"


def _pct(num: int, den: int) -> str:
    return f"{100 * num // den}%" if den else "-"


def pattern_report(queue: Queue, spec: dict) -> list[str]:
    done = (State.APPLIED.value, State.FAILED.value, State.EXPIRED.value)
    # EXPIRED also holds pending rows retired unreviewed (retire_stale_pending);
    # only the ones the apply step found closed were ever approved.
    items = [i for s in done for i in queue.items(s)
             if s != State.EXPIRED.value
             or i.application.notes.startswith(CLOSED_NOTE)]
    applied = [i for i in items if i.application.state == State.APPLIED.value]
    lines = ["PIPELINE - what approved jobs ended as"]

    by_platform: dict[str, Counter] = defaultdict(Counter)
    for i in items:
        by_platform[i.job.platform][i.application.state] += 1
    lines.append(f"  {'platform':<10} {'applied':>7} {'failed':>7} {'expired':>7}"
                 f" {'success':>8}")
    for platform, c in sorted(by_platform.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(c.values())
        lines.append(f"  {platform:<10} {c['applied']:>7} {c['failed']:>7}"
                     f" {c['expired']:>7} {_pct(c['applied'], total):>8}")
    reasons = Counter(failure_reason(i.application.notes) for i in items
                      if i.application.state == State.FAILED.value)
    if reasons:
        lines.append("  Top failure reasons:")
        lines += [f"    {n:>4}  {r}" for r, n in reasons.most_common(5)]

    rows = queue.conn.execute(
        """SELECT j.platform, j.external_id, a.outcome, a.auto_approved
             FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.state = ?""", (State.APPLIED.value,))
    meta = {(r["platform"], r["external_id"]): r for r in rows}
    recorded = sum(1 for r in meta.values() if r["outcome"])
    lines += ["", "OUTCOMES - interview/offer rate among recorded outcomes",
              f"  Outcomes recorded for {recorded} of {len(applied)} applications."]
    if recorded < MIN_OUTCOMES:
        lines.append(f"  Rates need {MIN_OUTCOMES}+ recorded outcomes per group: set"
                     " Status (Interview / Rejected / Offer) in the Tracker sheet;"
                     " the next board rebuild records it.")

    groups: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for i in applied:
        r = meta.get((i.job.platform, i.job.external_id))
        if r is None:
            continue
        keys = {"platform": i.job.platform,
                "score band": _band(score_job(i.job, spec).score),
                "approval": "auto" if r["auto_approved"] else "manual",
                "source": i.job.source_tag or "search"}
        for dim, value in keys.items():
            c = groups[dim][value]
            c["applied"] += 1
            if r["outcome"]:
                c["recorded"] += 1
                c["positive"] += r["outcome"] in POSITIVE
    for dim in ("platform", "score band", "approval", "source"):
        lines.append(f"  by {dim}:")
        for value, c in sorted(groups[dim].items(), key=lambda kv: -kv[1]["applied"]):
            rate = (_pct(c["positive"], c["recorded"])
                    if c["recorded"] >= MIN_OUTCOMES else "-")
            lines.append(f"    {value:<22} {c['applied']:>4} applied"
                         f" {c['recorded']:>4} recorded  rate {rate}")
    return lines + [""] + skip_report(queue, spec)


def skip_reason(note: str) -> str:
    """Group the scorer's skip notes ("score 34: title not in target_titles;
    ...") and the apply step's duplicate skips."""
    note = note or ""
    if note.startswith("duplicate"):
        return "duplicate of an applied opening"
    if "dealbreaker" in note:
        return "dealbreaker" + note.split("dealbreaker")[1].split(";")[0]
    if "tops out below" in note:
        return "salary below floor"
    if "title not in target_titles" in note:
        return "title not in target_titles"
    return "score below min_score"


def skip_report(queue: Queue, spec: dict, top: int = 10) -> list[str]:
    """Why discovered jobs never reached review, and the titles missed most.

    Two in three skips are title misses; the most frequent missed titles are
    the candidates for target_titles. A title is listed only if the current
    target_titles still miss it: older skips predate titles added since.
    Adapted from career-ops discard-analytics.mjs (MIT).
    """
    skipped = queue.items(State.SKIPPED)
    lines = [f"SKIPS - why {len(skipped)} discovered jobs never reached review"]
    reasons = Counter(skip_reason(i.application.notes) for i in skipped)
    lines += [f"    {n:>4}  {r}" for r, n in reasons.most_common(8)]
    missed = Counter(i.job.title.strip().lower() for i in skipped
                     if skip_reason(i.application.notes) == "title not in target_titles"
                     and not _title_points(i.job.title.lower(), spec)[0])
    if missed:
        lines.append("  Most-missed titles (add real fits to job_spec.target_titles):")
        lines += [f"    {n:>4}  {t}" for t, n in missed.most_common(top)]
    return lines
