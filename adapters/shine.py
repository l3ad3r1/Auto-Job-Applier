"""Shine adapter: discovery now, apply once verified.

Discovery is verified against the live public results page (2026-10-05): 20 of
20 cards parsed with id, title and posting age. Shine's CSS-module class names
carry a rotating hash (`result-card_card__p42UB`), so every selector here
matches the stable prefix, never the full class. The schema.org `itemprop`
hooks (`name`, `url`) are the most durable anchors on the card.

`apply()` does NOT click anything yet, for the same reason as Foundit: the
post-click flow needs a logged-in session to observe, and guessing a success
marker would record applications that never happened. Until one real apply has
been watched end to end, the job is left for the user to submit by hand.
"""
from __future__ import annotations

import re

from playwright.sync_api import Locator, Page

from core.models import Job, QueuedItem
from .base import ApplyResult, BaseAdapter, parse_posted_age

SALARY_HINT = re.compile(r"lakh|lac|lpa|crore", re.I)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class ShineAdapter(BaseAdapter):
    platform = "shine"
    login_url = "https://www.shine.com/pages/myshine/login"

    # -- session ---------------------------------------------------------------

    def is_logged_in(self, page: Page) -> bool:
        page.goto("https://www.shine.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        if "shine" not in (page.title() or "").lower():
            return False
        if "/login" in page.url:       # redirected to the sign-in page
            return False
        # Logged out, the header shows a Login link. The signup link is NOT a
        # signal: a promo "Register" link stays visible on the signed-in
        # dashboard (seen 2026-10-05), which made a good session read as out.
        return not page.locator("a[href*='myshine/login']:visible").first.count()

    # -- discovery ---------------------------------------------------------------

    def search(self, page: Page, config: dict) -> list[Job]:
        cfg = config.get("search", {})
        batch_limit = int(config.get("limits", {}).get("discover_batch", 40))
        exclude = [s.lower() for s in cfg.get("exclude_title", [])]
        # First configured location only: the URL takes a single city, and
        # "india" would return the whole country.
        locations = (cfg.get("locations") or ["Hyderabad"])[:1]
        jobs: dict[str, Job] = {}

        for keyword in cfg.get("keywords", []):
            for location in locations:
                if len(jobs) >= batch_limit:
                    break
                page.goto("https://www.shine.com/job-search/"
                          f"{_slug(keyword)}-jobs-in-{_slug(location)}",
                          wait_until="domcontentloaded")
                page.wait_for_timeout(4500)
                cards = page.locator("article[itemtype*='ListItem']").all()
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

        url_meta = card.locator("meta[itemprop='url']").first
        url = (url_meta.get_attribute("content") or "") if url_meta.count() else ""
        m = re.search(r"/(\d+)/?$", url)
        title = text_of("h3[itemprop='name']")
        if not (m and title):
            return None

        # Meta items are positional: experience, salary, location.
        meta = [" ".join(t.split()) for t in
                card.locator("[class*='result-card_meta-item']").all_inner_texts()]
        salary = next((x for x in meta if SALARY_HINT.search(x)), "")
        location = meta[-1] if meta and not SALARY_HINT.search(meta[-1]) else ""

        return Job(
            platform=self.platform, external_id=m.group(1), title=title,
            company=text_of("[class*='result-card_company']"),
            location=location, url=url, salary=salary,
            # The card says only "Apply" for every job, so nothing here
            # distinguishes one-step apply from an external redirect. Left
            # False: auto-approve never promotes a Shine job on its own.
            easy_apply=False,
            posted_age_days=parse_posted_age(text_of("[class*='result-card_posted']")),
        )

    # -- application ---------------------------------------------------------------

    def collect_questions(self, page: Page, item: QueuedItem) -> list[str]:
        return []

    def apply(self, page: Page, item: QueuedItem, limiter) -> ApplyResult:
        # Deliberately inert: see the module docstring. No click, no navigation.
        return ApplyResult(
            False, "shine apply is not verified yet - open the job and apply "
                   f"by hand: {item.job.url}")
