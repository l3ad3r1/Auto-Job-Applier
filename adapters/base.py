"""Adapter interface + shared browser session handling.

Each platform adapter owns a persistent Playwright profile under
profiles/<platform>/ — the user signs in manually once via `app.py login`,
and every later run reuses that session. Credentials are never handled here.
"""
from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import BrowserContext, Error as PWError, Page, sync_playwright

from core.models import Job, QueuedItem

ROOT = Path(__file__).resolve().parent.parent
DEBUG_DIR = ROOT / "data" / "debug"


@dataclass
class ApplyResult:
    ok: bool
    note: str = ""


class BaseAdapter(ABC):
    platform: str = ""
    login_url: str = ""

    # -- browser lifecycle ----------------------------------------------------

    def open_browser(self, headless: bool = False) -> tuple[BrowserContext, Page]:
        """Launch a persistent context bound to this platform's profile dir.

        If a stale Chromium from a crashed run still holds the profile,
        close it and retry once.
        """
        profile_dir = ROOT / "profiles" / self.platform
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        for attempt in range(2):
            try:
                ctx = self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless,
                    viewport={"width": 1440, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                break
            except PWError as e:
                if attempt == 0 and "already in use" in str(e):
                    print(f"  [{self.platform}] profile locked by a stale browser"
                          " — closing it and retrying")
                    self._close_stale_profile_browser(profile_dir)
                    time.sleep(3)
                    continue
                self._pw.stop()
                raise
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._ctx = ctx
        return ctx, page

    @staticmethod
    def _close_stale_profile_browser(profile_dir: Path) -> None:
        """Terminate Chromium processes holding this profile dir (Windows)."""
        marker = str(profile_dir).replace("/", "\\")
        ps = (
            "$procs = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" |"
            f" Where-Object {{ $_.CommandLine -like '*{marker}*' }};"
            " foreach ($p in $procs) {"
            "   try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop }"
            "   catch {} }"
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=30)
        except Exception as e:
            print(f"  could not close stale browser: {e}")

    def capture_debug(self, page: Page, tag: str) -> str:
        """Save a screenshot + HTML snapshot of the current page state.

        Returns a short relative path recorded in failure notes so problems
        are diagnosable without reproducing them.
        """
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = DEBUG_DIR / f"{stamp}_{self.platform}_{tag}"
        try:
            page.screenshot(path=f"{base}.png", full_page=False)
            with open(f"{base}.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            return str(base.relative_to(ROOT)) + ".png"
        except Exception as e:
            return f"(debug capture failed: {type(e).__name__})"

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
