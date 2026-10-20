from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from playwright.sync_api import Error as PWError

import app
from adapters.base import ApplyResult, BaseAdapter
from core.liveness import closed_reason
from core.models import Job, State
from core.queue import Queue

# Banners as rendered in saved failure pages (data/debug, 2026-10).
CLOSED_PAGES = {
    "linkedin": "Area Sales Manager\nAcme · Hyderabad\nNo longer accepting applications\n",
    "foundit": "Branch Manager\nThis job is no longer accepting applications\nSimilar jobs",
    "naukri": "Job you are looking for is\n  expired\nSearch similar jobs",
    "indeed": "This job has expired on Indeed\nReasons could include: the employer"
              " is not accepting applications, is not actively hiring",
}


class ClosedReasonTests(unittest.TestCase):
    def test_every_platform_banner_is_recognised(self) -> None:
        for platform, text in CLOSED_PAGES.items():
            with self.subTest(platform=platform):
                self.assertTrue(closed_reason(text))

    def test_naukri_banner_split_across_elements(self) -> None:
        self.assertEqual(closed_reason(CLOSED_PAGES["naukri"]),
                         "Job you are looking for is expired")

    def test_live_posting_is_not_closed(self) -> None:
        live = ("Sales Manager\nEasy Apply\nApplications will be reviewed on a rolling"
                " basis. We are not accepting applications from agencies.\n"
                "Your passport must not have expired.")
        self.assertEqual(closed_reason(live), "")
        self.assertEqual(closed_reason(""), "")


class _FakePage:
    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.text, self.fail = text, fail

    def inner_text(self, selector: str, timeout: int = 0) -> str:
        if self.fail:
            raise PWError("page crashed")
        return self.text


class _Adapter(BaseAdapter):
    platform = "test"

    def __init__(self, result: ApplyResult | None = None) -> None:
        self.result = result
        self.captured = 0

    def is_logged_in(self, page): return True
    def search(self, page, config): return []
    def collect_questions(self, page, item): return []
    def apply(self, page, item, limiter): return self.result
    def open_browser(self, headless=False): return None, _FakePage()
    def close_browser(self): pass

    def capture_debug(self, page, tag):
        self.captured += 1
        return "shot.png"


class ClosedPostingTests(unittest.TestCase):
    def test_closed_page_gives_an_expired_result(self) -> None:
        result = _Adapter().closed_posting(_FakePage(CLOSED_PAGES["linkedin"]))
        self.assertTrue(result.expired)
        self.assertFalse(result.ok)
        self.assertIn("No longer accepting applications", result.note)

    def test_open_or_unreadable_page_gives_none(self) -> None:
        self.assertIsNone(_Adapter().closed_posting(_FakePage("Easy Apply")))
        self.assertIsNone(_Adapter().closed_posting(_FakePage(fail=True)))


class _Limiter:
    def __init__(self, *a) -> None: pass
    def remaining(self, platform): return 10
    def check_cap(self, platform): pass
    def application_delay(self): pass


class ApplyExpiredTests(unittest.TestCase):
    def test_expired_posting_is_marked_expired_without_capture_or_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Queue(Path(tmp) / "queue.db")
            app_id = queue.add_job(Job(platform="test", external_id="1",
                                       title="Sales Manager", company="Acme"))
            queue.set_state(app_id, State.APPROVED)
            adapter = _Adapter(ApplyResult(False, "posting closed: 'x'", expired=True))
            with mock.patch.object(app, "Queue", lambda: queue), \
                    mock.patch.object(app, "load_config", lambda: {}), \
                    mock.patch.object(app, "Limiter", _Limiter), \
                    mock.patch.object(app, "get_adapter", lambda p: adapter):
                app.cmd_apply("test")
            row = queue.get(app_id).application
            self.assertEqual(row.state, State.EXPIRED.value)
            self.assertEqual(row.notes, "posting closed: 'x'")
            self.assertEqual(queue.retry_count(app_id), 0)
            self.assertEqual(adapter.captured, 0)
            queue.conn.close()


if __name__ == "__main__":
    unittest.main()
