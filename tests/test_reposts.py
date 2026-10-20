from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.models import Job
from core.queue import Queue, title_identity


def ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds")


class RepostTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Queue(Path(self._tmp.name) / "queue.db")

    def tearDown(self) -> None:
        self.queue.conn.close()
        self._tmp.cleanup()

    def add(self, ext: str, title: str, days: int, company: str = "Acme Retail",
            platform: str = "linkedin") -> str:
        app_id = self.queue.add_job(Job(
            platform=platform, external_id=ext, title=title, company=company,
            discovered_at=ago(days)))
        return self.queue.get(app_id).application.flags

    def test_title_identity_ignores_order_and_punctuation(self) -> None:
        self.assertEqual(title_identity("Manager - Business Development"),
                         title_identity("Business Development Manager"))
        self.assertNotEqual(title_identity("Sales Manager - Pune"),
                            title_identity("Sales Manager - Delhi"))

    def test_same_title_on_a_later_day_is_a_repost(self) -> None:
        self.add("1", "Area Sales Manager", days=20)
        flags = self.add("2", "Area Sales Manager", days=0, company="acme retail")
        self.assertIn("repost", flags)
        self.assertIn("as 1", flags)

    def test_same_day_listings_are_parallel_openings(self) -> None:
        self.add("1", "Area Sales Manager", days=0)
        self.assertEqual(self.add("2", "Area Sales Manager", days=0), "")

    def test_different_city_is_a_different_opening(self) -> None:
        self.add("1", "Sales Manager - Pune", days=10)
        self.assertEqual(self.add("2", "Sales Manager - Delhi", days=0), "")

    def test_same_title_in_another_town_is_a_different_opening(self) -> None:
        self.queue.add_job(Job(platform="linkedin", external_id="1",
                               title="Branch Manager", company="Acme Retail",
                               location="Hyderabad, Shamirpet",
                               discovered_at=ago(10)))
        app_id = self.queue.add_job(Job(platform="linkedin", external_id="2",
                                        title="Branch Manager", company="Acme Retail",
                                        location="Hyderabad, Suryapet"))
        self.assertEqual(self.queue.get(app_id).application.flags, "")

    def test_other_platform_is_syndication_not_repost(self) -> None:
        self.add("1", "Area Sales Manager", days=10, platform="naukri")
        self.assertEqual(self.add("2", "Area Sales Manager", days=0), "")

    def test_outside_window_is_ignored(self) -> None:
        self.add("1", "Area Sales Manager", days=120)
        self.assertEqual(self.add("2", "Area Sales Manager", days=0), "")

    def test_flag_survives_state_changes(self) -> None:
        self.add("1", "Area Sales Manager", days=5)
        app_id = self.queue.add_job(Job(
            platform="linkedin", external_id="2", title="Area Sales Manager",
            company="Acme Retail"))
        self.queue.set_state(app_id, "failed", notes="adapter error")
        self.assertIn("repost", self.queue.get(app_id).application.flags)


if __name__ == "__main__":
    unittest.main()
