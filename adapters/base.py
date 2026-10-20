"""Adapter interface + shared browser session handling.

Each platform adapter owns a persistent Playwright profile under
profiles/<platform>/ — the user signs in manually once via `app.py login`,
and every later run reuses that session. Credentials are never handled here.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import BrowserContext, Error as PWError, Page, sync_playwright

from core.liveness import closed_reason
from core.models import Job, QueuedItem

ROOT = Path(__file__).resolve().parent.parent

_AGE_UNITS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}
_AGE_RE = re.compile(r"(\d+)\+?\s*(minute|hour|day|week|month|year)s?\s+ago", re.I)


def parse_posted_age(text: str) -> int:
    """Days since posting, from wording like "2 weeks ago". -1 when absent.

    Shared by all three adapters because each surfaces the same phrasing in a
    different place: Naukri renders it in the card (.job-post-day), Indeed only
    embeds it in the results JSON (formattedRelativeTime), and LinkedIn mostly
    does not expose it on search cards at all.

    The wordless forms are real values, not defensive padding: across the saved
    Naukri captures, "Today" appears 17 times and "Few hours ago" 6, and an
    earlier digits-only version returned -1 for both -- scoring the freshest
    listings on the board as "age unknown".

    Takes the SMALLEST age present: "Reposted 2 days ago" next to an older
    original date should count as 2 days.
    """
    t = (text or "").strip().lower()
    if not t:
        return -1
    # "Posted today" / "Posted yesterday": Foundit prefixes the wording, so an
    # exact-match test misses it. "a day ago" is one unit, not a missing number.
    if "just now" in t or "few hours" in t or t.split()[-1] == "today":
        return 0
    if t.split()[-1] == "yesterday":
        return 1
    t = re.sub(r"\b(?:a|an)\s+(?=(?:minute|hour|day|week|month|year)s?\s+ago)", "1 ", t)
    ages = [int(n) * _AGE_UNITS[unit] for n, unit in _AGE_RE.findall(t)]
    return min(ages) if ages else -1


def search_city(search_cfg: dict) -> str:
    """The city to search, from config `search.locations`.

    That setting is a free-text address, not a city: the real config holds the
    single string "Hyderabad, Telangana, India". Boards that take one city in
    the URL (Foundit, Shine) were handed the whole string, which Foundit read as
    "anywhere in India" and returned mass postings from other cities. The city
    is the part before the first comma of the first entry.
    """
    first = (search_cfg.get("locations") or ["Hyderabad"])[0]
    return str(first).split(",")[0].strip() or "Hyderabad"


DEBUG_DIR = ROOT / "data" / "debug"


@dataclass
class ApplyResult:
    ok: bool
    note: str = ""
    expired: bool = False         # the posting is closed; not a tool failure


class BaseAdapter(ABC):
    platform: str = ""
    login_url: str = ""
    # Some sites keep the login in a session-only cookie, which Chromium drops
    # when the browser closes - so a login that worked yesterday is gone today.
    # When True, cookies are saved after a run that verified a login and put
    # back at the next launch, and the login itself runs in a Playwright window
    # so there is a run to capture them from. Shine (2026-10-05): the dashboard
    # shell loaded from persistent cookies, then redirected to login.
    persist_session_cookies: bool = False
    _session_ok: bool = False       # set by is_logged_in() on a verified login

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
        self._session_ok = False
        self._restore_session_cookies(ctx)
        return ctx, page

    def _cookie_file(self) -> Path:
        return ROOT / "profiles" / self.platform / "session-cookies.json"

    def _restore_session_cookies(self, ctx: BrowserContext) -> None:
        if not self.persist_session_cookies or not self._cookie_file().exists():
            return
        try:
            ctx.add_cookies(json.loads(self._cookie_file().read_text(encoding="utf-8")))
        except Exception as e:
            print(f"  [{self.platform}] could not restore saved cookies: {e}")

    def _save_session_cookies(self) -> None:
        """Only after a verified login: saving a logged-out cookie jar over a
        good one is how a working session would be lost."""
        if not (self.persist_session_cookies and self._session_ok):
            return
        try:
            self._cookie_file().write_text(
                json.dumps(self._ctx.cookies(), indent=1), encoding="utf-8")
        except Exception as e:
            print(f"  [{self.platform}] could not save cookies: {e}")

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

    def closed_posting(self, page: Page) -> ApplyResult | None:
        """An expired result when the job page says the posting is closed.

        Called only where the apply control is missing, so it can relabel a
        failure but never stop a live application.
        """
        try:
            text = page.inner_text("body", timeout=5000)
        except PWError:
            return None
        reason = closed_reason(text)
        return ApplyResult(False, f"posting closed: {reason!r}", expired=True)             if reason else None

    def close_browser(self) -> None:
        try:
            self._save_session_cookies()
            self._ctx.close()
        finally:
            self._pw.stop()

    def interactive_login(self) -> None:
        """Open the platform's login page and wait for the user to sign in.

        Default: a plain browser, deliberately NOT Playwright-controlled. With CDP attached,
        Foundit's login froze with "Debugger paused in another tab" and the
        Google sign-in popup went black (2026-10-05), and Google refuses
        sign-in in automation-controlled browsers anyway. So this launches the
        same Chromium binary on the same profile dir with nothing attached;
        Playwright reads the saved cookies on its next run.
        """
        if self.persist_session_cookies:
            # Needs a Playwright run to capture the session cookie from, and
            # the user must NOT close the window: that is what drops it.
            ctx, page = self.open_browser(headless=False)
            page.goto(self.login_url)
            print(f"\n  Sign in to {self.platform} in the opened browser window.")
            print("  Do NOT close the window. When you are signed in, press Enter here.")
            input()
            self._session_ok = True
            self.close_browser()
            return
        profile_dir = ROOT / "profiles" / self.platform
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._close_stale_profile_browser(profile_dir)
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
        proc = subprocess.Popen([exe, f"--user-data-dir={profile_dir}",
                                 "--no-first-run", "--no-default-browser-check",
                                 self.login_url])
        print(f"\n  Sign in to {self.platform} in the opened browser window.")
        print("  When you are signed in, CLOSE the browser window, then press "
              "Enter here.\n  (Closing it normally is what saves the session.)")
        input()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            print("  browser still open - closing it")
            proc.terminate()

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
