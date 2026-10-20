from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
from adapters.base import ApplyResult
from core.models import Job, State
from core.patterns import skip_report, skip_reason
from core.queue import Queue, company_key, same_opening
from tests.test_liveness import _Adapter, _Limiter

SPEC = {"min_score": 0, "target_titles": ["business development manager"]}


def job(platform: str, ext: str, company: str = "Skill2Source",
        title: str = "Business Development Manager",
        location: str = "Hyderabad") -> Job:
    return Job(platform=platform, external_id=ext, title=title,
               company=company, location=location)


class SameOpeningTests(unittest.TestCase):
    def test_company_key_drops_legal_form(self) -> None:
        self.assertEqual(company_key("Skill2Source Pvt. Ltd."), "skill2source")
        self.assertEqual(company_key("The Acme Company"), "acme")

    def test_cross_board_spellings_match(self) -> None:
        self.assertTrue(same_opening(
            job("linkedin", "1", "Skill2Source"),
            job("foundit", "2", "skill2source pvt ltd",
                "Manager - Business Development",
                "Hyderabad, Telangana, India")))

    def test_different_towns_are_different_openings(self) -> None:
        a = job("naukri", "1", "Bajaj Finance", "Branch Manager", "Pune")
        b = job("naukri", "2", "Bajaj Finance", "Branch Manager", "Delhi")
        self.assertFalse(same_opening(a, b))

    def test_missing_town_compares_company_and_title(self) -> None:
        self.assertTrue(same_opening(job("a", "1", location=""), job("b", "2")))
        self.assertFalse(same_opening(job("a", "1"), job("b", "2", title="Sales Manager")))
        self.assertFalse(same_opening(job("a", "1", company=""), job("b", "2", company="")))


class _QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.queue = Queue(Path(self._tmp.name) / "queue.db")

    def tearDown(self) -> None:
        self.queue.conn.close()
        self._tmp.cleanup()

    def add(self, j: Job, state: State) -> int:
        app_id = self.queue.add_job(j)
        self.queue.set_state(app_id, state)
        return app_id


class DuplicateGuardTests(_QueueCase):
    def test_discovery_flags_a_copy_of_an_applied_opening(self) -> None:
        first = self.add(job("linkedin", "1"), State.APPLIED)
        copy = self.queue.add_job(job("foundit", "2"))
        self.assertIn(f"duplicate: same opening applied on linkedin as app {first}",
                      self.queue.get(copy).application.flags)
        other = self.queue.add_job(job("foundit", "3", "Other Co"))
        self.assertEqual(self.queue.get(other).application.flags, "")

    def test_apply_skips_an_opening_already_applied_elsewhere(self) -> None:
        self.add(job("linkedin", "1"), State.APPLIED)
        dup = self.add(job("test", "2"), State.APPROVED)
        fresh = self.add(job("test", "3", "Other Co"), State.APPROVED)
        adapter = _Adapter(ApplyResult(True, ""))
        with mock.patch.object(app, "Queue", lambda: self.queue), \
                mock.patch.object(app, "load_config", lambda: {}), \
                mock.patch.object(app, "Limiter", _Limiter), \
                mock.patch.object(app, "get_adapter", lambda p: adapter):
            app.cmd_apply("test")
        row = self.queue.get(dup).application
        self.assertEqual(row.state, State.SKIPPED.value)
        self.assertTrue(row.notes.startswith("duplicate: same opening applied on linkedin"))
        self.assertEqual(self.queue.get(fresh).application.state, State.APPLIED.value)

    def test_approved_twice_applies_once(self) -> None:
        a = self.add(job("test", "1"), State.APPROVED)
        b = self.add(job("test", "2"), State.APPROVED)
        adapter = _Adapter(ApplyResult(True, ""))
        with mock.patch.object(app, "Queue", lambda: self.queue), \
                mock.patch.object(app, "load_config", lambda: {}), \
                mock.patch.object(app, "Limiter", _Limiter), \
                mock.patch.object(app, "get_adapter", lambda p: adapter):
            app.cmd_apply("test")
        states = sorted(self.queue.get(i).application.state for i in (a, b))
        self.assertEqual(states, [State.APPLIED.value, State.SKIPPED.value])


class AutoApproveCompanyCapTests(_QueueCase):
    def run_auto_approve(self, **cfg) -> None:
        config = {"auto_apply": {"enabled": True, "min_score": 0, "daily_max": 50,
                                 "require_easy_apply": False, **cfg}}
        with mock.patch.object(app, "Queue", lambda: self.queue), \
                mock.patch.object(app, "load_config", lambda: config), \
                mock.patch.object(app, "load_profile", lambda: {"job_spec": SPEC}):
            app.cmd_auto_approve()

    def pending(self, j: Job) -> int:
        app_id = self.queue.add_job(j)
        self.queue.save_preparation(app_id, {}, [], "")
        return app_id

    def state(self, app_id: int) -> str:
        return self.queue.get(app_id).application.state

    def test_cap_counts_history_and_this_run(self) -> None:
        self.add(job("naukri", "0", "Bajaj Finance Ltd", "Branch Manager", "Agra"),
                 State.APPLIED)
        ids = [self.pending(job("naukri", str(n), "Bajaj Finance", "Branch Manager",
                                town)) for n, town in enumerate(
                                    ["Pune", "Delhi", "Surat"], start=1)]
        other = self.pending(job("naukri", "9", "Acme", "Branch Manager", "Pune"))
        self.run_auto_approve(max_per_company=2)
        self.assertEqual([self.state(i) for i in ids],
                         [State.APPROVED.value, State.PENDING_REVIEW.value,
                          State.PENDING_REVIEW.value])
        self.assertEqual(self.state(other), State.APPROVED.value)

    def test_cap_zero_disables(self) -> None:
        ids = [self.pending(job("naukri", str(n), "Bajaj Finance", "Branch Manager",
                                town)) for n, town in enumerate(
                                    ["Pune", "Delhi", "Surat", "Agra"])]
        self.run_auto_approve(max_per_company=0)
        self.assertTrue(all(self.state(i) == State.APPROVED.value for i in ids))

    def test_holds_an_opening_approved_on_another_board(self) -> None:
        self.add(job("linkedin", "1"), State.APPROVED)
        copy = self.pending(job("foundit", "2"))
        self.run_auto_approve()
        self.assertEqual(self.state(copy), State.PENDING_REVIEW.value)


class SkipReportTests(_QueueCase):
    def test_reasons_and_most_missed_titles(self) -> None:
        notes = ["score 30: title not in target_titles; age unknown",
                 "score 31: title not in target_titles",
                 "dealbreaker: 'telecaller'",
                 "salary '2-3 Lacs P.A.' tops out below 6 LPA",
                 "score 50: title~'sales manager'",
                 "duplicate: same opening applied on linkedin as app 4"]
        notes.append("score 20: title not in target_titles")  # since added
        titles = ["Key Account Manager", "key account manager", "Telecaller",
                  "Sales Manager", "Sales Manager", "Sales Manager",
                  "Business Development Manager"]
        for n, (title, note) in enumerate(zip(titles, notes)):
            app_id = self.queue.add_job(job("naukri", str(n), f"Co{n}", title))
            self.queue.set_state(app_id, State.SKIPPED, notes=note)
        self.assertEqual(skip_reason(notes[2]), "dealbreaker: 'telecaller'")
        text = "\n".join(skip_report(self.queue, SPEC))
        self.assertIn("why 7 discovered jobs", text)
        self.assertIn("3  title not in target_titles", text)
        self.assertNotIn("business development manager", text)
        self.assertIn("1  salary below floor", text)
        self.assertIn("1  score below min_score", text)
        self.assertIn("1  duplicate of an applied opening", text)
        self.assertIn("2  key account manager", text)


if __name__ == "__main__":
    unittest.main()
