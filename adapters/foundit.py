"""Foundit (formerly Monster India) adapter: discovery now, apply once verified.

Discovery is verified against the live public results page (2026-10-05): 15 of
15 cards parsed with id, title and posting age. Search needs no login, but the
adapter still reports logged-out as unhealthy so the routine says so.

`apply()` does NOT click anything yet. The post-click flow (instant submit,
questionnaire, or redirect to the company site) can only be observed with a
logged-in session, and a guessed success marker would record applications that
never happened. Until one real apply has been watched end to end, apply returns
a fail-safe result and the job is left for the user to submit by hand. Nothing
here is auto-approved either: see `easy_apply` below.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus

from playwright.sync_api import Locator, Page

from core.models import Job, QueuedItem
from .base import ApplyResult, BaseAdapter, parse_posted_age

SALARY_HINT = re.compile(r"lpa|lakh|lac|inr|₹|\bper\b", re.I)


class FounditAdapter(BaseAdapter):
    platform = "foundit"
    login_url = "https://www.foundit.in/rio/login/seeker"

    # -- session ---------------------------------------------------------------

    def is_logged_in(self, page: Page) -> bool:
        page.goto("https://www.foundit.in/", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        # Title guards against a blank/blocked page reading as "no Login
        # button, therefore logged in".
        if "foundit" not in (page.title() or "").lower():
            return False
        return not page.locator("button:has-text('Login'):visible").first.count()

    # -- discovery ---------------------------------------------------------------

    def search(self, page: Page, config: dict) -> list[Job]:
        cfg = config.get("search", {})
        batch_limit = int(config.get("limits", {}).get("discover_batch", 40))
        exclude = [s.lower() for s in cfg.get("exclude_title", [])]
        # First configured location only. Foundit takes a city; the trailing
        # "Telangana" / "India" entries would widen the search to the whole
        # country and bury the results in other cities' jobs.
        locations = (cfg.get("locations") or ["Hyderabad"])[:1]
        jobs: dict[str, Job] = {}

        for keyword in cfg.get("keywords", []):
            for location in locations:
                if len(jobs) >= batch_limit:
                    break
                page.goto("https://www.foundit.in/srp/results?query="
                          f"{quote_plus(keyword)}&locations={quote_plus(location.lower())}",
                          wait_until="domcontentloaded")
                page.wait_for_timeout(4500)
                cards = page.locator("div.cardContainer").all()
                print(f"  [{keyword} / {location}] {len(cards)} cards")
                for card in cards:
                    job = self._parse_card(card)
                    if not job:
                        continue
                    if any(x in job.title.lower() for x in exclude):
                        continue
                    jobs[job.external_id] = job
                    if len(jobs) >= batch_limit:
                        break
        return list(jobs.values())

    def _parse_card(self, card: Locator) -> Job | None:
        def text_of(sel: str) -> str:
            loc = card.locator(sel).first
            return " ".join((loc.inner_text() or "").split()) if loc.count() else ""

        job_id = card.get_attribute("id") or ""
        title = text_of(".jobTitle")
        if not (job_id.isdigit() and title):
            return None

        details = [" ".join(t.split()) for t in
                   card.locator(".experienceSalary .details").all_inner_texts()]
        salary = next((d for d in details if SALARY_HINT.search(d)), "")
        tags = " ".join(card.locator(".jobTags").all_inner_texts()).lower()

        return Job(
            platform=self.platform, external_id=job_id, title=title,
            company=text_of(".companyName"),
            location=text_of(".location"),
            # The card has no anchor; /job/<id> redirects to the canonical page.
            url=f"https://www.foundit.in/job/{job_id}",
            salary=salary,
            # "Quick Apply" is Foundit's own one-step-apply tag. Without it the
            # job is not marked easy-apply, so auto-approve never promotes it.
            easy_apply="quick apply" in tags,
            posted_age_days=parse_posted_age(text_of(".timeText")),
        )

    # -- application ---------------------------------------------------------------

    def collect_questions(self, page: Page, item: QueuedItem) -> list[str]:
        return []

    def apply(self, page: Page, item: QueuedItem, limiter) -> ApplyResult:
        # Deliberately inert: see the module docstring. No click, no navigation.
        return ApplyResult(
            False, "foundit apply is not verified yet - open the job and apply "
                   f"by hand: {item.job.url}")
