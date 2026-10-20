"""Foundit (formerly Monster India) adapter: search + Quick Apply.

Discovery is verified against the live public results page (2026-10-05): 15 of
15 cards parsed with id, title and posting age. Search needs no login, but the
adapter still reports logged-out as unhealthy so the routine says so.

Apply is verified against one real application the same day. "Quick Apply"
submits instantly with no questionnaire, then lands on
`/srp/applied-success?id=<job id>` showing "Applied just now" and a status
timeline. Only a job whose MAIN button reads "Quick Apply" is applied to. A main
button reading "Apply Now" redirects to the company's own site, so it is
reported as external and never clicked.

The job page repeats `id="applyBtn"` on SEVEN buttons: the main one, a hidden
sticky copy, and one per "Recommended Jobs" card. A selector built on that id or
on the text "Apply Now" clicks a different job's button - which is exactly what
happened on the first observation attempt (it marked an unrelated Kochi job as
applied in the user's history). The main button is the one whose class contains
`jobMain`, and the click is refused unless exactly one element matches.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from core.models import Job, QueuedItem
from .base import ApplyResult, BaseAdapter, parse_posted_age, search_city

# Foundit serves 15 results a page and about three pages per search.
PAGE_SIZE = 15
MAX_PAGES = 3

# Where a Hyderabad search may legitimately match. Foundit's location filter is
# loose: `locations=hyderabad` also returns India-wide postings whose location
# reads "India, Sindhnur" or "Gurugram, India" (37 of 39 skipped jobs in the
# first run), which then fail the location bar downstream.
_AREA_ALIASES = {"hyderabad": ("secunderabad", "telangana")}


def _in_area(location_text: str, city: str) -> bool:
    text = (location_text or "").lower()
    city = city.lower()
    return city in text or any(a in text for a in _AREA_ALIASES.get(city, ()))


# The main apply button only. `[class*=...]` rather than `.jobMain`: the chained
# class selector matched nothing on the live page even though the class is there.
MAIN_APPLY = "button[class*='jobMain']"

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
        # Logged out, Foundit redirects "/" to its login page, which has no
        # "Login" button - so the button test alone reads that page as signed in
        # (seen 2026-10-05: url .../rio/login/seeker, logged_in=True).
        if "/login" in page.url:
            return False
        return not page.locator("button:has-text('Login'):visible").first.count()

    # -- discovery ---------------------------------------------------------------

    def search(self, page: Page, config: dict) -> list[Job]:
        cfg = config.get("search", {})
        limits = config.get("limits", {})
        # Its own, larger cap. `discover_batch` bounds per-job browser cost on
        # the other boards, but a Foundit page is one cheap load with ~15 jobs
        # and no per-job visits - and at the shared 40 the cap was filled by
        # page 1 of three keywords, so paging never ran (seen live 2026-10-05).
        batch_limit = int(limits.get("foundit_batch",
                                     3 * int(limits.get("discover_batch", 40))))
        exclude = [s.lower() for s in cfg.get("exclude_title", [])]
        keywords = cfg.get("keywords", [])
        location = search_city(cfg)     # a city, not the config's full address
        jobs: dict[str, Job] = {}
        done: set[str] = set()      # keywords with no more worthwhile pages

        # Breadth-first: page 1 of every keyword, then page 2 of every keyword.
        # Depth-first would let the first keyword fill the whole batch and
        # starve the rest.
        for page_no in range(MAX_PAGES):
            for keyword in keywords:
                if len(jobs) >= batch_limit:
                    return list(jobs.values())
                if keyword in done:
                    continue
                page.goto(self._search_url(keyword, location, page_no),
                          wait_until="domcontentloaded")
                page.wait_for_timeout(4500)
                cards = page.locator("div.cardContainer").all()
                kept = 0
                for card in cards:
                    job = self._parse_card(card)
                    if not job or not _in_area(job.location, location):
                        continue
                    if any(x in job.title.lower() for x in exclude):
                        continue
                    if job.external_id not in jobs:
                        jobs[job.external_id] = job
                        kept += 1
                    if len(jobs) >= batch_limit:
                        break
                print(f"  [{keyword} / {location}] page {page_no + 1}: "
                      f"{len(cards)} cards, {kept} in area")
                # Past the last page Foundit returns no cards at all. A page
                # with cards but none in the area means deeper pages are just
                # more India-wide postings, so stop paging this keyword.
                if not cards or not kept:
                    done.add(keyword)
        return list(jobs.values())

    @staticmethod
    def _search_url(keyword: str, location: str, page_no: int) -> str:
        base = ("https://www.foundit.in/srp/results?query="
                f"{quote_plus(keyword)}&locations={quote_plus(location.lower())}")
        if page_no == 0:
            return base
        # Offset paging, verified live 2026-10-05: no searchId needed, page 2
        # shares nothing with page 1, and a page past the end has 0 cards.
        return f"{base}&start={page_no * PAGE_SIZE}&sort=1&limit={PAGE_SIZE}"

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

    def _already_applied(self, page: Page) -> bool:
        # The status timeline ("Your application status") only renders for a job
        # the user has applied to.
        return page.get_by_text("Your application status").first.count() > 0

    def apply(self, page: Page, item: QueuedItem, limiter) -> ApplyResult:
        page.goto(item.job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        if self._already_applied(page):
            return ApplyResult(True, "was already applied on Foundit")

        btn = page.locator(MAIN_APPLY)
        try:
            btn.first.wait_for(state="visible", timeout=15000)
        except PWTimeout:
            return self.closed_posting(page) or ApplyResult(
                False, "Apply button not found")
        if btn.count() != 1:
            # Never guess between buttons: the sidebar cards share the id.
            return ApplyResult(False, f"apply button ambiguous ({btn.count()} matches)")

        label = " ".join((btn.first.inner_text() or "").split())
        if label.lower() != "quick apply":
            return ApplyResult(
                False, f"external application (main button reads {label!r}, "
                       "not Quick Apply)")

        limiter.action_delay()
        btn.first.click(timeout=8000)
        page.wait_for_timeout(6000)

        # Success lands on /srp/applied-success, sometimes in a second tab.
        landed = any("applied-success" in pg.url for pg in page.context.pages)
        for extra in [pg for pg in page.context.pages if pg is not page]:
            try:
                extra.close()
            except Exception:
                pass
        if landed or self._already_applied(page):
            return ApplyResult(True)
        return ApplyResult(False, "clicked Quick Apply, no confirmation")
