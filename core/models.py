"""Data models for the application pipeline."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class State(str, Enum):
    """Pipeline states for an application.

    discovered      job scraped, nothing prepared yet
    prepared        answers drafted / resume chosen, ready for human review
    pending_review  surfaced in the dashboard, awaiting decision
    approved        human approved — eligible for submission
    rejected        human rejected — never submit
    applied         submitted successfully
    failed          submission attempted and failed (kept for retry/inspection)
    skipped         adapter decided it can't handle this job (e.g. external apply)
    """

    DISCOVERED = "discovered"
    PREPARED = "prepared"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    platform: str                 # "linkedin", "indeed", ...
    external_id: str              # platform's own job id — dedupe key
    title: str
    company: str
    location: str = ""
    url: str = ""
    description: str = ""
    salary: str = ""              # raw posted salary text, "" when undisclosed
    easy_apply: bool = False
    discovered_at: str = field(default_factory=utcnow)


@dataclass
class Application:
    job_id: int                   # rowid of the job in the DB
    state: str = State.DISCOVERED.value
    # question -> answer map drafted at prepare time; JSON in the DB
    answers: dict = field(default_factory=dict)
    # questions the prepare step could not answer — shown in dashboard
    unanswered: list = field(default_factory=list)
    resume_path: str = ""
    notes: str = ""               # failure reasons, adapter remarks
    updated_at: str = field(default_factory=utcnow)

    def answers_json(self) -> str:
        return json.dumps(self.answers, ensure_ascii=False)

    def unanswered_json(self) -> str:
        return json.dumps(self.unanswered, ensure_ascii=False)


@dataclass
class QueuedItem:
    """A job joined with its application row, as the dashboard/CLI sees it."""
    app_id: int
    job: Job
    application: Application

    def to_dict(self) -> dict:
        d = asdict(self.job)
        d.update(
            app_id=self.app_id,
            state=self.application.state,
            answers=self.application.answers,
            unanswered=self.application.unanswered,
            resume_path=self.application.resume_path,
            notes=self.application.notes,
        )
        return d
