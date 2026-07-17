"""Naukri adapter: search + direct apply.

No open-source prior art — built from scratch. Naukri specifics:
- Direct "Apply" submits IMMEDIATELY on click (no multi-page form), so the
  review queue gate matters even more here: we only ever click Apply on
  items the user approved.
- After applying, a chatbot drawer may ask employer questions; we answer
  best-effort from the profile and record anything left unanswered
  (the application itself is already through at that point).
- "Apply on company site" jobs are skipped as external.
"""
from __future__ import annotations

import re

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from core.models import Job, QueuedItem
from core.profile import load_profile
from llm.answers import resolve_answer
from .base import ApplyResult, BaseAdapter


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _clean_salary(text: str) -> str:
    t = " ".join(text.split())
    return "" if t.lower() in ("", "not disclosed") else t


class NaukriAdapter(BaseAdapter):
    platform = "naukri"
    login_url = "https://www.naukri.com/nlogin/login"

    # -- session ---------------------------------------------------------------

    def is_logged_in(self, page: Page) -> bool:
        page.goto("https://www.naukri.com/mnjuser/profile",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        return "nlogin" not in page.url and "/login" not in page.url

    # -- discovery ---------------------------------------------------------------

    def _search_url(self, keyword: str, location: str, cfg: dict) -> str:
        base = f"https://www.naukri.com/{_slug(keyword)}-jobs-in-{_slug(location)}"
        days = max(1, int(cfg.get("posted_within", 604800)) // 86400)
        params = [f"k={keyword}", f"l={location}", f"jobAge={days}"]
        if cfg.get("experience_years"):
            params.append(f"experience={cfg['experience_years']}")
        return base + "?" + "&".join(params)

    def search(self, page: Page, config: dict) -> list[Job]:
        cfg = config.get("search", {})
        batch_limit = int(config.get("limits", {}).get("discover_batch", 40))
        exclude = [s.lower() for s in cfg.get("exclude_title", [])]
        jobs: dict[str, Job] = {}

        for keyword in cfg.get("keywords", []):
            for location in cfg.get("locations", []):
                if len(jobs) >= batch_limit:
                    break
                page.goto(self._search_url(keyword, location, cfg),
                          wait_until="domcontentloaded")
                page.wait_for_timeout(4500)
                cards = self._job_cards(page)
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

    def _job_cards(self, page: Page) -> list[Locator]:
        for sel in ("div.srp-jobtuple-wrapper", "article.jobTuple",
                    "div[data-job-id]"):
            cards = page.locator(sel).all()
            if cards:
                return cards
        return []

    def _parse_card(self, card: Locator) -> Job | None:
        link = card.locator("a.title, a[title][href*='job-listings']").first
        if not link.count():
            return None
        url = link.get_attribute("href") or ""
        title = (link.inner_text() or "").strip()
        job_id = (card.get_attribute("data-job-id") or "")
        if not job_id:
            m = re.search(r"-(\d{9,})\b", url)  # trailing id in job-listings URL
            job_id = m.group(1) if m else url
        if not (title and url):
            return None

        def text_of(*sels: str) -> str:
            for s in sels:
                loc = card.locator(s).first
                if loc.count():
                    t = (loc.inner_text() or "").strip()
                    if t:
                        return t
            return ""

        return Job(
            platform=self.platform, external_id=str(job_id), title=title,
            company=text_of(".comp-name", "a.subTitle", ".companyInfo"),
            location=text_of(".locWdth", ".loc-wrap", ".location"),
            url=url,
            description=text_of(".job-desc", ".job-description"),
            salary=_clean_salary(text_of(".sal", ".sal-wrap span",
                                         "span[class*='sal']")),
            easy_apply=True,  # refined at apply time (external ones get skipped)
        )

    # -- application ---------------------------------------------------------------

    def collect_questions(self, page: Page, item: QueuedItem) -> list[str]:
        # Naukri asks questions only AFTER the (instant) apply — nothing to
        # collect safely beforehand.
        return []

    def _applied_marker(self, page: Page) -> bool:
        """True when the page shows the job as applied (badge or toast)."""
        if page.locator("#already-applied, .already-applied, .apply-message"
                        ).first.count():
            return True
        # The Apply button turns into a green 'Applied' badge on success
        if page.locator("text=/^\\s*Applied\\s*$/").first.count():
            return True
        try:
            body = page.locator("body").inner_text(timeout=3000).lower()
        except PWTimeout:
            return False
        # The success banner reads: Applied to "<job title>" — the quote after
        # 'applied to' keeps this specific (straight or curly quotes).
        if re.search(r'applied to ["“‘]', body):
            return True
        return "successfully applied" in body or "application sent" in body

    def _apply_button(self, page: Page) -> Locator | None:
        btn = page.locator("#apply-button").first
        if btn.count():
            return btn
        btn = page.get_by_role("button", name=re.compile(r"^\s*Apply\s*$")).first
        return btn if btn.count() else None

    def apply(self, page: Page, item: QueuedItem, limiter) -> ApplyResult:
        profile = load_profile()
        self._chatbot_seen = False   # set by _handle_chatbot if a drawer appears
        page.goto(item.job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        if self._applied_marker(page):
            return ApplyResult(True, "was already applied on Naukri")

        if page.locator("#company-site-button").first.count():
            return ApplyResult(False, "external 'apply on company site' job")

        btn = self._apply_button(page)
        if btn is None:
            return ApplyResult(False, "Apply button not found")
        try:
            btn.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            return ApplyResult(False, "Apply button never became visible")
        limiter.action_delay()
        btn.scroll_into_view_if_needed()
        self._click_apply(btn)
        page.wait_for_timeout(5000)

        # Some jobs apply instantly; others open a pre-apply chatbot whose
        # questions must be answered before the application registers.
        unanswered = self._handle_chatbot(page, profile)
        page.wait_for_timeout(2000)

        if self._applied_marker(page):
            return ApplyResult(True)

        # A screening chatbot appeared but we couldn't complete it
        # automatically (free-text recruiter questions aren't auto-filled, by
        # design — answering them would risk fabrication). Fail safe with an
        # actionable message: the apply is initiated, but the user must answer
        # the recruiter's questions on Naukri to actually submit.
        if self._chatbot_seen or self._chatbot_open(page):
            try:
                page.keyboard.press("Escape")
            except PWTimeout:
                pass
            detail = f" ({'; '.join(unanswered)})" if unanswered else ""
            return ApplyResult(
                False, f"needs manual completion on Naukri - recruiter "
                       f"screening chatbot{detail}")

        # No chatbot and no badge — the click may have been swallowed; retry once.
        btn = self._apply_button(page)
        if btn is not None:
            self._click_apply(btn)
            page.wait_for_timeout(5000)
            self._handle_chatbot(page, profile)
            if self._applied_marker(page):
                return ApplyResult(True)
            if self._chatbot_seen:
                try:
                    page.keyboard.press("Escape")
                except PWTimeout:
                    pass
                return ApplyResult(
                    False, "needs manual completion on Naukri - recruiter "
                           "screening chatbot")
        return ApplyResult(False, "clicked Apply, no confirmation")

    def _click_apply(self, btn: Locator) -> None:
        """Click the Apply button, tolerating the post-click timeout Naukri trips.

        Clicking Apply often opens the screening chatbot, whose overlay then
        covers the button and fails Playwright's post-click actionability
        re-check — raising a 30s timeout even though the click registered.
        Swallow it; the caller's chatbot/applied-marker checks establish truth.
        """
        try:
            btn.click(timeout=8000)
        except PWTimeout:
            pass

    def _chatbot_open(self, page: Page) -> bool:
        return page.locator("div[id*='ChatbotContainer' i], div[class*='chatbotcontainer' i], "
                            "div[class*='chatbot_Drawer' i]").first.is_visible()

    def _handle_chatbot(self, page: Page, profile: dict) -> list[str]:
        """Answer chatbot questions best-effort. Returns unanswered questions.

        The drawer content lives in div[class*='ChatbotContainer']; a sibling
        chatbot_Overlay div blocks pointer events on the page behind it, so
        never try to click page elements while the drawer is open.
        """
        drawer = page.locator("div[id*='ChatbotContainer' i], div[class*='chatbotcontainer' i], "
                              "div[class*='chatbot_Drawer' i]").first
        try:
            drawer.wait_for(state="visible", timeout=5000)
        except PWTimeout:
            return []  # no chatbot — plain apply
        self._chatbot_seen = True  # a screening chatbot definitely appeared

        # Only radio/choice questions are auto-answered (a click on the user's
        # own option). Free-text recruiter questions are NOT auto-filled from
        # the LLM — answering "years in Branch Sales?" or "10th percentage?"
        # on a real application risks fabrication. Those are flagged for the
        # user to complete manually on Naukri.
        unanswered: list[str] = []
        prev_question = None
        for _ in range(10):  # one iteration per question
            page.wait_for_timeout(2000)
            msgs = drawer.locator(".botMsg, [class*='botMsg']").all()
            if not msgs:
                break
            question = " ".join((msgs[-1].inner_text() or "").split())
            if not question or question == prev_question:
                # No new question / our answer didn't advance it — stop looping.
                if question and question not in unanswered:
                    unanswered.append(question)
                break
            prev_question = question

            radios = drawer.locator("input[type='radio']")
            if not radios.count():
                unanswered.append(question)  # free text — user completes it
                break
            option_texts = [t.strip() for t in
                            drawer.locator("label").all_inner_texts()
                            if t.strip()] or None
            answer = resolve_answer(question, profile, options=option_texts)
            if answer is None:
                unanswered.append(question)
                break
            try:
                target = drawer.locator(f"label:has-text('{answer}')").first
                if not target.count():
                    unanswered.append(question)
                    break
                target.click(timeout=6000)
                self._chatbot_send(drawer)
            except PWTimeout:
                unanswered.append(question)
                break
            page.wait_for_timeout(1800)
            try:
                if not drawer.is_visible():
                    break
            except PWTimeout:
                break
        return unanswered

    def _chatbot_send(self, drawer: Locator) -> None:
        """Submit the current chatbot answer (Save button, else Enter)."""
        for sel in (".sendMsg", "button:has-text('Save')", "div[class*='send']"):
            b = drawer.locator(sel).first
            if b.count():
                try:
                    b.click(timeout=6000)
                    return
                except PWTimeout:
                    continue
        try:
            drawer.locator("div[contenteditable='true'], input, textarea").first.press(
                "Enter", timeout=3000)
        except PWTimeout:
            pass
