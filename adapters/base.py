"""Adapter interface + shared browser session handling.

Each platform adapter owns a persistent Playwright profile under
profiles/<platform>/ — the user signs in manually once via `app.py login`,
and every later run reuses that session. Credentials are never handled here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from core.models import Job, QueuedItem

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ApplyResult:
    ok: bool
    note: str = ""


class BaseAdapter(ABC):
    platform: str = ""
    login_url: str = ""

    # -- browser lifecycle ----------------------------------------------------

    def open_browser(self, headless: bool = False) -> tuple[BrowserContext, Page]:
        """Launch a persistent context bound to this platform's profile dir."""
        profile_dir = ROOT / "profiles" / self.platform
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._ctx = ctx
        return ctx, page

    def close_browser(self) -> None:
        try:
            self._ctx.close()
        finally:
            self._pw.stop()

    def interactive_login(self) -> None:
        """Open the platform's login page and wait for the user to sign in."""
        _, page = self.open_browser(headless=False)
        page.goto(self.login_url)
        print(f"\n  Sign in to {self.platform} in the opened browser window.")
        print("  The session is saved to the local profile dir; you won't need "
              "to do this again.\n  Press Enter here when you're done...")
        input()
        self.close_browser()

    # -- platform behaviour -----------------------------------------------------

    @abstractmethod
    def is_logged_in(self, page: Page) -> bool: ...

    @abstractmethod
    def search(self, page: Page, config: dict) -> list[Job]:
        """Scrape jobs matching config['search']; return Job objects (deduped by queue)."""

    @abstractmethod
    def collect_questions(self, page: Page, item: QueuedItem) -> list[str]:
        """Open the application form and return screening question texts, without submitting."""

    @abstractmethod
    def apply(self, page: Page, item: QueuedItem, limiter) -> ApplyResult:
        """Submit one APPROVED application. Must pace with limiter.action_delay()."""
