from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.followups import follow_ups
from core.models import Job, State
from core.patterns import failure_reason, pattern_report
from core.queue import Queue

SPEC = {"min_score": 60, "target_titles": ["sales manager"]}
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        # _sync_outcomes opens its own Queue and leaves it open; on Windows
        # that file cannot be deleted until the process exits.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.queue = Queue(Path(self._tmp.name) / "queue.db")
        self.n = 0

    def tearDown(self) -> None:
        self.queue.conn.close()
        self._tmp.cleanup()

    def add(self, state: str, platform: str = "linkedin", notes: str = "",
            applied_days_ago: int | None = None, outcome: str = "",
            source_tag: str = "") -> int:
        self.n += 1
        app_id = self.queue.add_job(Job(
            platform=platform, external_id=str(self.n), title="Sales Manager",
            company=f"Co{self.n}", source_tag=source_tag))
        self.queue.set_state(app_id, state, notes=notes)
        if applied_days_ago is not None:
            self.queue.conn.execute(
                "INSERT INTO submissions_log (platform, app_id, ts) VALUES (?,?,?)",
                (platform, app_id,
                 (NOW - timedelta(days=applied_days_ago)).isoformat()))
        if outcome:
            self.queue.set_outcome(platform, str(self.n), outcome)
        self.queue.conn.commit()
        return app_id


class FollowUpTests(_Base):
    def test_due_cold_and_excluded(self) -> None:
        self.add(State.APPLIED.value, applied_days_ago=3)            # too soon
        self.add(State.APPLIED.value, applied_days_ago=10)           # due
        self.add(State.APPLIED.value, applied_days_ago=20)           # due, older
        self.add(State.APPLIED.value, applied_days_ago=45)           # cold
        self.add(State.APPLIED.value, applied_days_ago=12,
                 outcome="rejected")                                 # replied
        self.add(State.FAILED.value, applied_days_ago=12)            # not applied
        sent = self.add(State.APPLIED.value, applied_days_ago=15)
        self.queue.set_followed_up("linkedin", str(self.n))
        self.assertIsNotNone(sent)

        due, cold = follow_ups(self.queue, now=NOW)
        self.assertEqual([f.days for f in due], [20, 10])
        self.assertEqual(cold, 1)

    def test_set_followed_up_is_idempotent(self) -> None:
        self.add(State.APPLIED.value, applied_days_ago=10)
        self.assertTrue(self.queue.set_followed_up("linkedin", "1"))
        self.assertFalse(self.queue.set_followed_up("linkedin", "1"))
        self.assertFalse(self.queue.set_followed_up("linkedin", "missing"))


class SheetSyncTests(_Base):
    def test_followed_up_checkbox_reaches_the_db(self) -> None:
        from core.tracker_sheet import _sync_outcomes
        self.add(State.APPLIED.value, applied_days_ago=10)
        self.add(State.APPLIED.value, applied_days_ago=10)
        changed = _sync_outcomes(
            {"linkedin:1": {"Followed up": True, "Status": "Applied"},
             "linkedin:2": {"Followed up": False, "Status": "Interview"}},
            self.queue.db_path)
        self.assertEqual(changed, 1)                     # only the outcome counts
        due, _ = follow_ups(self.queue, now=NOW)
        self.assertEqual(due, [])                        # 1 followed up, 2 replied


class PatternReportTests(_Base):
    def test_failure_reason_grouping(self) -> None:
        self.assertEqual(
            failure_reason("transient, will retry: adapter error: TimeoutError: x"
                           " [debug: data/debug/a.png]"), "adapter error")
        self.assertEqual(failure_reason("Unanswered required question: 'CTC?'"),
                         "Unanswered required question")
        self.assertEqual(failure_reason(""), "(no note)")

    def test_pipeline_counts_only_apply_time_expiries(self) -> None:
        self.add(State.APPLIED.value, applied_days_ago=1)
        self.add(State.FAILED.value, notes="Apply button not found [debug: x]")
        self.add(State.EXPIRED.value, notes="posting closed: 'No longer accepting'")
        self.add(State.EXPIRED.value,
                 notes="expired automatically: pending review for more than 14 days")
        text = "\n".join(pattern_report(self.queue, SPEC))
        row = next(l for l in text.splitlines() if l.strip().startswith("linkedin"))
        self.assertEqual(row.split()[1:], ["1", "1", "1", "33%"])
        self.assertIn("Apply button not found", text)

    def test_rates_hidden_below_sample_floor_shown_above(self) -> None:
        for outcome in ("interview", "rejected", "rejected", "rejected"):
            self.add(State.APPLIED.value, applied_days_ago=10, outcome=outcome)
        self.add(State.APPLIED.value, platform="naukri", applied_days_ago=10)
        text = "\n".join(pattern_report(self.queue, SPEC))
        self.assertIn("Outcomes recorded for 4 of 5", text)
        self.assertIn("Rates need 5+", text)
        self.assertNotIn("rate 25%", text)

        self.add(State.APPLIED.value, applied_days_ago=10, outcome="offer")
        text = "\n".join(pattern_report(self.queue, SPEC))
        self.assertNotIn("Rates need 5+", text)
        linkedin = next(l for l in text.splitlines()
                        if l.strip().startswith("linkedin") and "recorded" in l)
        self.assertIn("rate 40%", linkedin)              # 2 positive of 5
        naukri = next(l for l in text.splitlines()
                      if l.strip().startswith("naukri") and "recorded" in l)
        self.assertIn("rate -", naukri)


if __name__ == "__main__":
    unittest.main()
