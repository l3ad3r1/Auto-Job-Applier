"""Which applications are due a follow-up.

The Tracker sheet already computes a follow-up date (applied + 7 days) with a
"Followed up" checkbox, but only inside Excel, so neither the CLI nor the daily
Telegram report could say what was due. This reads the same rule from SQLite;
the checkbox reaches the DB through tracker_sheet._sync_outcomes.

Cadence adapted from career-ops followup-cadence.mjs (MIT): a reply is due
after a week, and an application silent for a month has gone cold, at which
point another nudge is unlikely to help and it is counted, not listed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .queue import Queue

FOLLOW_UP_DAYS = 7      # same as the Tracker sheet's "Follow-up" column
COLD_DAYS = 30


@dataclass
class FollowUp:
    title: str
    company: str
    platform: str
    url: str
    days: int


def follow_ups(queue: Queue, now: datetime | None = None
               ) -> tuple[list[FollowUp], int]:
    """(due follow-ups oldest first, count gone cold).

    Only applications with no recorded outcome and no follow-up sent count:
    an interview or rejection already is the reply.
    """
    now = now or datetime.now(timezone.utc)
    rows = queue.conn.execute(
        """SELECT j.title, j.company, j.platform, j.url,
                  COALESCE(MIN(s.ts), a.updated_at) AS applied_at
             FROM applications a
             JOIN jobs j ON j.id = a.job_id
             LEFT JOIN submissions_log s ON s.app_id = a.id
            WHERE a.state = 'applied' AND IFNULL(a.outcome, '') = ''
              AND IFNULL(a.followed_up_at, '') = ''
         GROUP BY a.id""")
    due, cold = [], 0
    for row in rows:
        days = (now - datetime.fromisoformat(row["applied_at"])).days
        if days >= COLD_DAYS:
            cold += 1
        elif days >= FOLLOW_UP_DAYS:
            due.append(FollowUp(row["title"], row["company"], row["platform"],
                                row["url"], days))
    due.sort(key=lambda f: -f.days)
    return due, cold
