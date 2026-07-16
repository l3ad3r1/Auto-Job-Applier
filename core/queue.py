"""SQLite-backed application queue.

One `jobs` row per discovered job (deduped on platform+external_id),
one `applications` row tracking its pipeline state.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .models import Application, Job, QueuedItem, State, utcnow

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "queue.db"

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

CREATE INDEX IF NOT EXISTS idx_app_state ON applications(state);
CREATE INDEX IF NOT EXISTS idx_sub_platform_ts ON submissions_log(platform, ts);
"""


class Queue:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Migration: add columns introduced after the first release
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "salary" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN salary TEXT DEFAULT ''")
        self.conn.commit()

    # -- discovery -----------------------------------------------------------

    def add_job(self, job: Job) -> Optional[int]:
        """Insert a job + fresh application row. Returns app id, or None if duplicate."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO jobs
               (platform, external_id, title, company, location, url,
                description, salary, easy_apply, discovered_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (job.platform, job.external_id, job.title, job.company, job.location,
             job.url, job.description, job.salary, int(job.easy_apply),
             job.discovered_at),
        )
        if cur.rowcount == 0:
            return None  # already known
        job_id = cur.lastrowid
        cur = self.conn.execute(
            "INSERT INTO applications (job_id, state, updated_at) VALUES (?,?,?)",
            (job_id, State.DISCOVERED.value, utcnow()),
        )
        self.conn.commit()
        return cur.lastrowid

    # -- state transitions ---------------------------------------------------

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
        )
        app = Application(
            job_id=row["job_id"], state=row["state"],
            answers=json.loads(row["answers"]),
            unanswered=json.loads(row["unanswered"]),
            resume_path=row["resume_path"], notes=row["notes"],
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

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) c FROM applications GROUP BY state")
        return {r["state"]: r["c"] for r in rows}
