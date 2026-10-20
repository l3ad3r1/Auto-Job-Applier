from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
from core.matching import is_match, score_job
from core.models import Job, State
from core.queue import Queue
from core.title_rescue import FLAG, MAX_TITLES, rescue_titles

TARGETS = ["key account manager", "sales manager"]
SPEC = {"min_score": 60, "target_titles": TARGETS,
        "preferred_locations": ["hyderabad"]}


def job(title: str, ext: str = "1", **kw) -> Job:
    kw.setdefault("location", "Hyderabad")
    kw.setdefault("easy_apply", True)
    kw.setdefault("posted_age_days", 1)
    return Job(platform="linkedin", external_id=ext, title=title,
               company="Acme", **kw)


class RescueTitlesTests(unittest.TestCase):
    def test_returns_the_titles_the_llm_picked(self) -> None:
        seen = {}

        def fake(system: str, user: str) -> str:
            seen["user"] = user
            return '```json\n{"rescue": [0, 2]}\n```'

        got = rescue_titles(["Corporate Accounts Manager", "Sales Executive",
                             "Sales Head"], TARGETS, fake)
        self.assertEqual(got, {"Corporate Accounts Manager", "Sales Head"})
        self.assertIn("- key account manager", seen["user"])
        self.assertIn("1. Sales Executive", seen["user"])

    def test_bad_output_and_errors_rescue_nothing(self) -> None:
        self.assertEqual(rescue_titles(["X"], TARGETS, lambda s, u: "sure!"), set())
        self.assertEqual(rescue_titles(["X"], TARGETS,
                                       lambda s, u: '{"rescue": [5, "a", -1]}'), set())

        def boom(system: str, user: str) -> str:
            raise RuntimeError("server down")
        self.assertEqual(rescue_titles(["X"], TARGETS, boom), set())

    def test_no_call_without_titles_or_targets(self) -> None:
        def never(system: str, user: str) -> str:
            raise AssertionError("must not call the LLM")
        self.assertEqual(rescue_titles([], TARGETS, never), set())
        self.assertEqual(rescue_titles(["X"], [], never), set())

    def test_batch_is_deduped_and_capped(self) -> None:
        seen = {}

        def fake(system: str, user: str) -> str:
            seen["n"] = user.count("\n") - len(TARGETS) - 2
            return '{"rescue": []}'
        rescue_titles(["A", "A"] + [f"T{i}" for i in range(500)], TARGETS, fake)
        self.assertEqual(seen["n"], MAX_TITLES)


class RescuedScoringTests(unittest.TestCase):
    def test_rescue_tops_up_a_near_miss(self) -> None:
        j = job("Corporate Accounts Manager")
        ok, result = is_match(j, SPEC)
        self.assertFalse(ok)
        self.assertIn("title not in target_titles", result.reasons)
        ok, result = is_match(j, SPEC, title_rescued=True)
        self.assertTrue(ok)
        self.assertIn("title rescued by LLM", result.reasons)

    def test_rescue_does_not_change_a_real_match(self) -> None:
        j = job("Sales Manager")
        self.assertEqual(score_job(j, SPEC).score,
                         score_job(j, SPEC, title_rescued=True).score)

    def test_rescued_junior_title_still_pays_the_seniority_penalty(self) -> None:
        self.assertLess(score_job(job("Sales Executive"), SPEC, True).score,
                        score_job(job("Sales Head"), SPEC, True).score)


class PrepareRescueTests(unittest.TestCase):
    def test_prepare_routes_rescued_titles_to_review_with_a_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Queue(Path(tmp) / "queue.db")
            rescued = queue.add_job(job("Corporate Accounts Manager", "1"))
            ignored = queue.add_job(job("Sales Executive", "2"))
            far = queue.add_job(job("Corporate Accounts Manager", "3",
                                    location="Pune", easy_apply=False,
                                    posted_age_days=60))
            asked = {}

            def fake_rescue(titles, targets):
                asked["titles"] = titles
                return {"Corporate Accounts Manager"}

            profile = {"job_spec": SPEC, "answers": {}}
            with mock.patch.object(app, "Queue", lambda: queue), \
                    mock.patch.object(app, "load_profile", lambda: profile), \
                    mock.patch.object(app, "_rescue_titles", fake_rescue):
                app.cmd_prepare()

            # Only rows that could clear the bar with the rescue are asked about.
            self.assertEqual(sorted(asked["titles"]),
                             ["Corporate Accounts Manager", "Sales Executive"])
            self.assertEqual(queue.get(rescued).application.state,
                             State.PENDING_REVIEW.value)
            self.assertIn(FLAG, queue.get(rescued).application.flags)
            self.assertEqual(queue.get(ignored).application.state,
                             State.SKIPPED.value)
            self.assertEqual(queue.get(far).application.state, State.SKIPPED.value)
            queue.conn.close()

    def test_auto_approve_holds_rescued_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Queue(Path(tmp) / "queue.db")
            app_id = queue.add_job(job("Sales Manager"))
            queue.save_preparation(app_id, {}, [], "")
            queue.add_flag(app_id, f"{FLAG} - test")
            config = {"auto_apply": {"enabled": True, "min_score": 0,
                                     "daily_max": 5, "require_easy_apply": False}}
            with mock.patch.object(app, "Queue", lambda: queue), \
                    mock.patch.object(app, "load_config", lambda: config), \
                    mock.patch.object(app, "load_profile",
                                      lambda: {"job_spec": SPEC}):
                app.cmd_auto_approve()
            self.assertEqual(queue.get(app_id).application.state,
                             State.PENDING_REVIEW.value)
            queue.conn.close()


if __name__ == "__main__":
    unittest.main()
