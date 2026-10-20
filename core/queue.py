"""SQLite-backed application queue.

One `jobs` row per discovered job (deduped on platform+external_id),
one `applications` row tracking its pipeline state.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .models import Application, Job, QueuedItem, State, utcnow
from .posting_guard import scan

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "queue.db"

# How far back a repost is looked for. Adapted from career-ops
# detect-reposts.mjs (MIT), which uses the same 90-day window.
REPOST_WINDOW_DAYS = 90


def title_identity(title: str) -> frozenset[str]:
    """The SET of title words, so word order and punctuation churn between two
    listings of one role still match, but "Sales Manager - Pune" and
    "Sales Manager - Delhi" (two real openings) do not."""
    return frozenset(re.findall(r"[a-z0-9]+", title.lower()))


def _col(row: sqlite3.Row, name: str, default):
    """Read a column that may predate the caller's migration.

    Rows can come from a DB opened by an older copy of this module (the
    dashboard and a cron run are separate processes), so a missing column must
    degrade to the default rather than raise.
    """
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    platform      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    salary        TEXT DEFAULT '',
    easy_apply    INTEGER DEFAULT 0,
    discovered_at TEXT NOT NULL,
    source_tag    TEXT DEFAULT '',
    source_tagged_at TEXT DEFAULT '',
    UNIQUE (platform, external_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    state       TEXT NOT NULL,
    answers     TEXT DEFAULT '{}',
    unanswered  TEXT DEFAULT '[]',
    resume_path TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions_log (
    id        INTEGER PRIMARY KEY,
    platform  TEXT NOT NULL,
    app_id    INTEGER NOT NULL,
    ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answer_log (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    source    TEXT NOT NULL,          -- 'llm' (map answers are user-authored)
    question  TEXT NOT NULL,
    answer    TEXT NOT NULL,
    job_ref   TEXT DEFAULT ''         -- '<platform>:<external_id> title @ company'
);

CREATE INDEX IF NOT EXISTS idx_app_state ON applications(state);
CREATE INDEX IF NOT EXISTS idx_sub_platform_ts ON submissions_log(platform, ts);
"""


class Queue:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        # The dashboard and scheduled runs write concurrently — WAL avoids
        # 'database is locked' between the two processes.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)
        # Migration: add columns introduced after the first release
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "salary" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN salary TEXT DEFAULT ''")
        if "source_tag" not in cols:
            self.conn.execute(
                "ALTER TABLE jobs ADD COLUMN source_tag TEXT DEFAULT ''")
        if "source_tagged_at" not in cols:
            self.conn.execute(
                "ALTER TABLE jobs ADD COLUMN source_tagged_at TEXT DEFAULT ''")
        if "posted_age_days" not in cols:
            # Days since posting, from the search card. -1 = not captured.
            self.conn.execute(
                "ALTER TABLE jobs ADD COLUMN posted_age_days INTEGER DEFAULT -1")
        if "applicants" not in cols:
            self.conn.execute(
                "ALTER TABLE jobs ADD COLUMN applicants INTEGER DEFAULT -1")
        acols = {r["name"] for r in self.conn.execute("PRAGMA table_info(applications)")}
        if "retry_count" not in acols:
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN retry_count INTEGER DEFAULT 0")
        if "synced_at" not in acols:
            # When this application was appended to the Google Sheet (NULL = not yet)
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN synced_at TEXT DEFAULT ''")
        if "auto_approved" not in acols:
            # Set when a row is promoted past human review; holds the match
            # score. Its own column on purpose: this used to live in `notes`,
            # which set_state OVERWRITES, so the marker vanished the moment an
            # application failed or went to retry. That lost the attribution
            # for exactly the rows worth attributing, AND under-counted the
            # daily_max rail, letting extra unreviewed applications through.
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN auto_approved TEXT DEFAULT ''")
        if "outcome" not in acols:
            # What happened AFTER submitting: interview, rejected, offer...
            # Kept out of `state` on purpose — `state` drives the pipeline, and
            # 'applied' has to stay terminal for the caps, dedupe and reporting
            # queries that already depend on it.
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN outcome TEXT DEFAULT ''")
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN outcome_at TEXT DEFAULT ''")
        if "followed_up_at" not in acols:
            # Set from the Tracker sheet's "Followed up" checkbox, so the CLI
            # and the daily report stop listing a follow-up already sent.
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN followed_up_at TEXT DEFAULT ''")
        if "flags" not in acols:
            # Discovery-time warnings (posting guard, repost); see models.
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN flags TEXT DEFAULT ''")
        self.conn.commit()

    # -- discovery -----------------------------------------------------------

    def add_job(self, job: Job) -> Optional[int]:
        """Insert a job + fresh application row. Returns app id, or None if duplicate.

        Posting text is cleaned by the posting guard before it is stored, and
        a new row is checked for being a repost of an earlier listing.
        """
        flags = []
        for attr in ("title", "company", "description"):
            cleaned, report = scan(getattr(job, attr))
            setattr(job, attr, cleaned)
            if report.suspicious:
                flags.append(report.summary)
        tagged_at = job.source_tagged_at or (utcnow() if job.source_tag else "")
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO jobs
               (platform, external_id, title, company, location, url,
                description, salary, easy_apply, discovered_at,
                source_tag, source_tagged_at, posted_age_days, applicants)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.platform, job.external_id, job.title, job.company, job.location,
             job.url, job.description, job.salary, int(job.easy_apply),
             job.discovered_at, job.source_tag, tagged_at,
             job.posted_age_days, job.applicants),
        )
        if cur.rowcount == 0:
            # A normal search may discover a listing before the focused
            # same-day Hiring search. Preserve the original queue row while
            # recording that it also matched the fresh-source query.
            if job.source_tag:
                self.conn.execute(
                    """UPDATE jobs SET source_tag=?, source_tagged_at=?
                       WHERE platform=? AND external_id=?""",
                    (job.source_tag, tagged_at, job.platform, job.external_id),
                )
                self.conn.commit()
            return None  # already known
        job_id = cur.lastrowid
        repost = self._repost_of(job)
        if repost:
            flags.append(repost)
        cur = self.conn.execute(
            "INSERT INTO applications (job_id, state, updated_at, flags)"
            " VALUES (?,?,?,?)",
            (job_id, State.DISCOVERED.value, utcnow(), "; ".join(flags)),
        )
        self.conn.commit()
        return cur.lastrowid

    def _repost_of(self, job: Job) -> str:
        """A flag when the same company listed the same title before, on an
        earlier day, under a different id. Empty string otherwise.

        A repost can mean a ghost posting (kept up to collect CVs) or a role
        nobody accepted. Two constraints, both from career-ops, keep sibling
        openings from reading as reposts: the earlier listing must be from a
        DIFFERENT day (one sweep finding two ids lists parallel openings, not
        a repost), and titles must have the same word set, not merely similar
        ones. Location is compared the same way: Naukri lists one title in many
        towns ("Branch Manager - GL South" x 23 at Bajaj Finance), and those
        are separate openings. Same platform only: one opening cross-posted to LinkedIn and
        Naukri is syndication, not a repost.
        """
        if not job.company.strip():
            return ""
        day = job.discovered_at[:10]
        cutoff = (datetime.fromisoformat(job.discovered_at)
                  - timedelta(days=REPOST_WINDOW_DAYS)).isoformat()
        want = (title_identity(job.title), title_identity(job.location))
        rows = self.conn.execute(
            """SELECT external_id, title, location, discovered_at FROM jobs
               WHERE platform=? AND lower(company)=lower(?) AND external_id<>?
                 AND discovered_at >= ? AND substr(discovered_at, 1, 10) < ?
               ORDER BY discovered_at""",
            (job.platform, job.company.strip(), job.external_id, cutoff, day))
        for row in rows:
            if (title_identity(row["title"]),
                    title_identity(row["location"])) == want:
                return (f"repost: same title listed {row['discovered_at'][:10]}"
                        f" as {row['external_id']} - possible ghost posting")
        return ""

    # -- state transitions ---------------------------------------------------

    def add_flag(self, app_id: int, flag: str) -> None:
        """Append a warning to the row's durable flags column."""
        self.conn.execute(
            "UPDATE applications SET flags = CASE WHEN IFNULL(flags,'')=''"
            " THEN ? ELSE flags || '; ' || ? END WHERE id=?",
            (flag, flag, app_id))
        self.conn.commit()


    def set_state(self, app_id: int, state: State | str, notes: str = "") -> None:
        state = state.value if isinstance(state, State) else state
        self.conn.execute(
            "UPDATE applications SET state=?, notes=CASE WHEN ?='' THEN notes ELSE ? END,"
            " updated_at=? WHERE id=?",
            (state, notes, notes, utcnow(), app_id),
        )
        self.conn.commit()

    def save_preparation(self, app_id: int, answers: dict, unanswered: list,
                         resume_path: str) -> None:
        self.conn.execute(
            """UPDATE applications
               SET answers=?, unanswered=?, resume_path=?, state=?, updated_at=?
               WHERE id=?""",
            (json.dumps(answers, ensure_ascii=False),
             json.dumps(unanswered, ensure_ascii=False),
             resume_path, State.PENDING_REVIEW.value, utcnow(), app_id),
        )
        self.conn.commit()

    def log_submission(self, platform: str, app_id: int) -> None:
        self.conn.execute(
            "INSERT INTO submissions_log (platform, app_id, ts) VALUES (?,?,?)",
            (platform, app_id, utcnow()),
        )
        self.conn.commit()

    def log_answer(self, source: str, question: str, answer: str,
                   job_ref: str = "") -> None:
        self.conn.execute(
            "INSERT INTO answer_log (ts, source, question, answer, job_ref)"
            " VALUES (?,?,?,?,?)",
            (utcnow(), source, question, answer, job_ref))
        self.conn.commit()

    def recent_answers(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, source, question, answer, job_ref FROM answer_log"
            " ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -- retry bookkeeping -----------------------------------------------------

    def mark_for_retry(self, app_id: int, notes: str) -> None:
        """Transient failure: re-arm as APPROVED and count the attempt."""
        self.conn.execute(
            """UPDATE applications SET state=?, notes=?, updated_at=?,
               retry_count = retry_count + 1 WHERE id=?""",
            (State.APPROVED.value, notes, utcnow(), app_id))
        self.conn.commit()

    def retry_count(self, app_id: int) -> int:
        row = self.conn.execute(
            "SELECT retry_count FROM applications WHERE id=?", (app_id,)).fetchone()
        return row["retry_count"] if row else 0

    def mark_auto_approved(self, app_id: int, score: int) -> None:
        """Stamp a row as promoted past human review, permanently.

        Written to its own column so no later set_state can erase it.
        """
        self.conn.execute(
            "UPDATE applications SET auto_approved=? WHERE id=?",
            (f"{utcnow()} score {score}", app_id),
        )
        self.conn.commit()

    def set_outcome(self, platform: str, external_id: str, outcome: str) -> bool:
        """Record what happened after applying. Returns True if it changed."""
        row = self.conn.execute(
            """SELECT a.id, a.outcome FROM applications a
                 JOIN jobs j ON j.id = a.job_id
                WHERE j.platform = ? AND j.external_id = ?""",
            (platform, external_id)).fetchone()
        if row is None or (row["outcome"] or "") == outcome:
            return False
        self.conn.execute(
            "UPDATE applications SET outcome=?, outcome_at=? WHERE id=?",
            (outcome, utcnow(), row["id"]))
        self.conn.commit()
        return True

    def set_followed_up(self, platform: str, external_id: str) -> bool:
        """Record that a follow-up was sent. Returns True if it changed."""
        cur = self.conn.execute(
            """UPDATE applications SET followed_up_at=?
               WHERE IFNULL(followed_up_at,'')='' AND job_id IN (
                   SELECT id FROM jobs WHERE platform=? AND external_id=?)""",
            (utcnow(), platform, external_id))
        self.conn.commit()
        return cur.rowcount > 0

    def outcome_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT outcome, COUNT(*) c FROM applications"
            " WHERE outcome <> '' GROUP BY outcome ORDER BY c DESC")
        return {r["outcome"]: r["c"] for r in rows}

    def auto_approved_today(self, marker: str = "") -> int:
        """How many UNREVIEWED promotions happened today.

        Reads the dedicated column, not `notes`. `notes` is overwritten by
        set_state on every failure and retry, so counting it under-reported the
        day's promotions and leaked the daily_max ceiling.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM applications"
            " WHERE auto_approved <> '' AND auto_approved >= date('now')",
        ).fetchone()
        return row["c"]

    def auto_approved_rows(self) -> list[tuple]:
        """(state, title, company, auto_approved) for every unreviewed row."""
        return [tuple(r) for r in self.conn.execute(
            "SELECT a.state, j.title, j.company, a.auto_approved"
            " FROM applications a JOIN jobs j ON j.id = a.job_id"
            " WHERE a.auto_approved <> '' ORDER BY a.auto_approved DESC")]

    def submissions_today(self, platform: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM submissions_log WHERE platform=? AND ts >= date('now')",
            (platform,),
        ).fetchone()
        return row["c"]

    # -- reads ----------------------------------------------------------------

    def _row_to_item(self, row: sqlite3.Row) -> QueuedItem:
        job = Job(
            platform=row["platform"], external_id=row["external_id"],
            title=row["title"], company=row["company"], location=row["location"],
            url=row["url"], description=row["description"],
            salary=row["salary"] or "",
            easy_apply=bool(row["easy_apply"]), discovered_at=row["discovered_at"],
            posted_age_days=_col(row, "posted_age_days", -1),
            applicants=_col(row, "applicants", -1),
            source_tag=row["source_tag"] or "",
            source_tagged_at=row["source_tagged_at"] or "",
        )
        app = Application(
            job_id=row["job_id"], state=row["state"],
            answers=json.loads(row["answers"]),
            unanswered=json.loads(row["unanswered"]),
            resume_path=row["resume_path"], notes=row["notes"],
            flags=_col(row, "flags", ""),
            updated_at=row["updated_at"],
        )
        return QueuedItem(app_id=row["app_id"], job=job, application=app)

    def items(self, state: State | str | None = None,
              platform: str | None = None) -> list[QueuedItem]:
        q = """SELECT a.id app_id, a.*, j.* FROM applications a
               JOIN jobs j ON j.id = a.job_id WHERE 1=1"""
        params: list = []
        if state is not None:
            q += " AND a.state=?"
            params.append(state.value if isinstance(state, State) else state)
        if platform is not None:
            q += " AND j.platform=?"
            params.append(platform)
        q += " ORDER BY a.updated_at DESC"
        return [self._row_to_item(r) for r in self.conn.execute(q, params)]

    def get(self, app_id: int) -> Optional[QueuedItem]:
        row = self.conn.execute(
            """SELECT a.id app_id, a.*, j.* FROM applications a
               JOIN jobs j ON j.id = a.job_id WHERE a.id=?""", (app_id,),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def unsynced_applied(self) -> list[QueuedItem]:
        """APPLIED items not yet appended to the Google Sheet, oldest first."""
        rows = self.conn.execute(
            """SELECT a.id app_id, a.*, j.* FROM applications a
               JOIN jobs j ON j.id = a.job_id
               WHERE a.state=? AND IFNULL(a.synced_at,'')=''
               ORDER BY a.updated_at ASC""", (State.APPLIED.value,))
        return [self._row_to_item(r) for r in rows]

    def mark_synced(self, app_id: int) -> None:
        self.conn.execute("UPDATE applications SET synced_at=? WHERE id=?",
                          (utcnow(), app_id))
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) c FROM applications GROUP BY state")
        return {r["state"]: r["c"] for r in rows}

    def retire_stale_pending(self, days: int = 14) -> int:
        """Retire old review items without treating them as user rejections.

        Only PENDING_REVIEW rows are eligible. Approved rows are deliberately
        excluded because approval is a human decision and must remain durable.
        """
        if days < 1:
            raise ValueError("days must be at least 1")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds")
        note = f"expired automatically: pending review for more than {days} days"
        cur = self.conn.execute(
            """UPDATE applications
               SET state=?, notes=CASE WHEN notes='' THEN ? ELSE notes || '; ' || ? END,
                   updated_at=?
               WHERE state=? AND job_id IN (
                   SELECT id FROM jobs WHERE discovered_at < ?
               )""",
            (State.EXPIRED.value, note, note, utcnow(),
             State.PENDING_REVIEW.value, cutoff),
        )
        self.conn.commit()
        return cur.rowcount

    def job_board_rows(self) -> list[dict]:
        """Return the complete queue in a stable, spreadsheet-friendly shape."""
        rows = self.conn.execute(
            """SELECT a.id AS app_id, j.external_id, j.platform, a.state,
                      j.title, j.company, j.location, j.salary, j.easy_apply,
                      j.discovered_at, a.updated_at,
                      (SELECT MAX(s.ts) FROM submissions_log s
                       WHERE s.app_id=a.id) AS applied_at,
                      j.description, j.url, a.notes, a.unanswered, a.answers
                      , j.source_tag, j.source_tagged_at
               FROM applications a
               JOIN jobs j ON j.id=a.job_id
               ORDER BY j.discovered_at DESC, a.id DESC"""
        )
        return [dict(row) for row in rows]
