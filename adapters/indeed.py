"""Indeed adapter: search + Indeed Apply (SmartApply).

Selectors mined from _references/beatwad/src/job_manager/indeed/. Uses
in.indeed.com for the Indian market. Indeed runs Cloudflare bot detection —
the persistent, headed profile helps, but if a CAPTCHA/verification wall
appears we stop and tell the user rather than trying to defeat it.

SmartApply is a multi-page wizard (resume selection, location, experience,
employer questions). Phase 1 handles the common pages and fails safe —
anything it can't answer aborts the wizard before submission.
"""
from __future__ import annotations

import re
import urllib.parse

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from core.models import Job, QueuedItem
from core.profile import load_profile
from llm.answers import resolve_answer
from .base import ApplyResult, BaseAdapter, parse_posted_age

BASE = "https://in.indeed.com"
JOB_CARD = "div.job_seen_beacon, div[data-testid='jobcard-wrapper']"


_JOBKEY_RE = re.compile(r'"jobkey"\s*:\s*"([0-9a-f]+)"')
_RELTIME_RE = re.compile(r'"formattedRelativeTime"\s*:\s*"([^"]+)"')
_APPLYABLE_RE = re.compile(r'"indeedApplyEnabled"\s*:\s*(true|false)')


def _owning_key(keys, pos):
    """The job key belonging to the object that contains `pos`.

    The payload orders its fields alphabetically, so within one job object
    "formattedRelativeTime" and "indeedApplyEnabled" both come BEFORE "jobkey".
    The owning key is therefore the first one that follows the value.

    Pairing by smallest absolute distance instead looks reasonable and is
    wrong: "indeedApplyEnabled" sits near the end of its object, so the closest
    key is often the NEXT job's, silently attributing each apply flag to the
    wrong listing.
    """
    for start, key in keys:
        if start > pos:
            return key
    return None


def _meta_by_jobkey(html: str) -> dict[str, dict]:
    """Map job key -> {"age": days, "easy": bool} from the results page JSON.

    Indeed renders neither fact into the card. Verified against a real
    search-results capture: with <script> blocks stripped, there are zero
    "N days ago" strings and zero indeedApply markers -- both live only in the
    embedded jobcards JSON, as "formattedRelativeTime" and "indeedApplyEnabled".

    The old easy-apply check looked for [data-testid='indeedApply'] and .ialbl
    in the card DOM. Neither appears there (the class is gone entirely), so it
    returned False for every job, and the require_easy_apply gate then excluded
    all of Indeed -- including the ~25% that really are applyable.

    Each value is attributed to the job key that follows it -- see _owning_key
    for why "nearest key" is the wrong rule here.
    """
    keys = [(m.start(), m.group(1)) for m in _JOBKEY_RE.finditer(html)]
    if not keys:
        return {}
    out: dict[str, dict] = {}
    for m in _RELTIME_RE.finditer(html):
        key = _owning_key(keys, m.start())
        days = parse_posted_age(m.group(1))
        if key and days >= 0:
            out.setdefault(key, {}).setdefault("age", days)
    for m in _APPLYABLE_RE.finditer(html):
        key = _owning_key(keys, m.start())
        if key:
            out.setdefault(key, {}).setdefault("easy", m.group(1) == "true")
    return out


JOB_TITLE = "h2.jobTitle a, [data-testid='jobTitle'] a, a.jcs-JobTitle"
# Indeed rebuilt the job view as React: every jobsearch-* marker is gone from
# the page (0 occurrences in a captured failure), so the old three selectors
# matched nothing and every attempt reported "external application" -- masking
# stale selectors as a property of the job. The current control is an anchor,
# not a button, hooked by data-testid. Text is the fallback.
APPLY_BTN = ("[data-testid='viewjob-indeed-apply'], "
             "#indeedApplyButton, [data-testid='indeedApplyButton-test'], "
             ".jobsearch-IndeedApplyButton-buttonWrapper button, "
             "a:has-text('Apply now'), button:has-text('Apply now')")

# OneTrust consent banner. It overlays the page and swallows the click on the
# apply control. Reject rather than accept: decline non-essential cookies.
CONSENT_REJECT = "#onetrust-reject-all-handler, .ot-pc-refuse-all-handler"
CONSENT_BANNER = "#onetrust-banner-sdk"
CONTINUE_BTN = re.compile(r"^(continue|next|review your application|submit"
                          r"|submit your application)$", re.I)


class IndeedAdapter(BaseAdapter):
    platform = "indeed"
    login_url = "https://in.indeed.com/account/login"

    # -- session ---------------------------------------------------------------

    def is_logged_in(self, page: Page) -> bool:
        page.goto(f"{BASE}/?lang=en", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        if self._blocked(page):
            return False
        # Signed-in header exposes the account menu button
        return page.locator("[data-gnav-element-name='AccountMenu'], "
                            "button[aria-label*='account']").first.count() > 0

    def _blocked(self, page: Page) -> bool:
        """Cloudflare / verification wall detection — we never try to bypass."""
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=5000).lower()
        except PWTimeout:
            return False
        return ("verify you are human" in body or "additional verification required" in body
                or page.locator("[data-testid='captcha'], #challenge-form").first.count() > 0)

    def _dismiss_consent(self, page: Page) -> bool:
        """Decline non-essential cookies so the banner stops eating clicks.

        Rejects rather than accepts. Returns True if a banner was dismissed.
        """
        try:
            if page.locator(CONSENT_BANNER).first.count() == 0:
                return False
            reject = page.locator(CONSENT_REJECT).first
            if reject.count() == 0:
                return False
            reject.click(timeout=5000)
            page.wait_for_timeout(1200)
            print("  [indeed] cookie banner dismissed (rejected non-essential)")
            return True
        except Exception:
            return False  # never let consent handling fail an application

    # -- discovery ---------------------------------------------------------------

    def _search_url(self, keyword: str, location: str, cfg: dict) -> str:
        days = max(1, int(cfg.get("posted_within", 604800)) // 86400)
        params = {"q": keyword, "l": location, "fromage": days}
        return f"{BASE}/jobs?" + urllib.parse.urlencode(params)

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
                if self._blocked(page):
                    print("  !! Indeed verification wall — solve it in the browser "
                          "window, then re-run discover")
                    return list(jobs.values())
                meta = _meta_by_jobkey(page.content())
                cards = page.locator(JOB_CARD).all()
                print(f"  [{keyword} / {location}] {len(cards)} cards")
                for card in cards:
                    job = self._parse_card(card)
                    if not job:
                        continue
                    info = meta.get(job.external_id, {})
                    job.posted_age_days = info.get("age", -1)
                    # The JSON is authoritative; the card selector below is only
                    # a fallback for when the payload shape changes again.
                    if "easy" in info:
                        job.easy_apply = info["easy"]
                    if any(x in job.title.lower() for x in exclude):
                        continue
                    jobs[job.external_id] = job
                    if len(jobs) >= batch_limit:
                        break
        return list(jobs.values())

    def _parse_card(self, card: Locator) -> Job | None:
        link = card.locator(JOB_TITLE).first
        if not link.count():
            return None
        title = (link.inner_text() or "").strip()
        jk = link.get_attribute("data-jk") or ""
        if not jk:
            m = re.search(r"jk=([0-9a-f]+)", link.get_attribute("href") or "")
            jk = m.group(1) if m else ""
        if not (jk and title):
            return None

        def text_of(sel: str) -> str:
            loc = card.locator(sel).first
            return (loc.inner_text() or "").strip() if loc.count() else ""

        return Job(
            platform=self.platform, external_id=jk, title=title,
            company=text_of("[data-testid='company-name'], .companyName"),
            location=text_of("[data-testid='text-location'], .companyLocation"),
            url=f"{BASE}/viewjob?jk={jk}",
            description=text_of("[data-testid='jobsearch-jobDescriptionText'],"
                                " .job-snippet"),
            salary=" ".join(text_of(
                "[data-testid='attribute_snippet_testid'], .salary-snippet,"
                " [class*='salary']").split()),
            easy_apply=("easily apply" in (card.inner_text() or "").lower()),
        )

    # -- application ---------------------------------------------------------------

    def collect_questions(self, page: Page, item: QueuedItem) -> list[str]:
        return []  # SmartApply pages are enumerated during apply; fail-safe there

    def apply(self, page: Page, item: QueuedItem, limiter) -> ApplyResult:
        profile = load_profile()
        page.goto(item.job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        if self._blocked(page):
            return ApplyResult(False, "Indeed verification wall — needs manual solve")
        self._dismiss_consent(page)

        btn = page.locator(APPLY_BTN).first
        try:
            btn.wait_for(state="visible", timeout=12000)
        except PWTimeout:
            return ApplyResult(False, "no apply control found on the job page"
                                      " (external application, or markup changed)")
        limiter.action_delay()
        btn.click()
        page.wait_for_timeout(5000)

        # SmartApply may open in the same tab or a popup
        wizard = page
        for p in page.context.pages:
            if "smartapply" in p.url or "m5.apply.indeed.com" in p.url:
                wizard = p
                break

        for _ in range(12):
            # Pages (especially the 'Preparing review' step) render their CTA
            # asynchronously — wait for it before touching the page
            cont = self._wait_continue(wizard)
            if cont is None:
                break
            limiter.action_delay()
            unanswerable = self._fill_wizard_page(wizard, item, profile)
            if unanswerable:
                wizard.close() if wizard is not page else None
                return ApplyResult(False, f"Unanswered question: {unanswerable!r}")
            cont = self._wait_continue(wizard)  # refetch after filling
            if cont is None:
                break
            label = (cont.inner_text() or "").strip().lower()
            cont.click()
            wizard.wait_for_timeout(3500)
            if label.startswith("submit"):
                return ApplyResult(True)
        return ApplyResult(False, "SmartApply wizard did not reach submission")

    def _wait_continue(self, wizard: Page, timeout_ms: int = 45000):
        """Poll for the wizard's Continue/Submit button while pages hydrate."""
        waited = 0
        while waited < timeout_ms:
            btn = wizard.get_by_role("button", name=CONTINUE_BTN).first
            try:
                if btn.count() and btn.is_visible():
                    return btn
            except PWTimeout:
                pass
            wizard.wait_for_timeout(1000)
            waited += 1000
        return None

    def _fill_wizard_page(self, page: Page, item: QueuedItem, profile: dict) -> str | None:
        """Fill the current SmartApply page. Returns unanswerable label or None."""
        for el in page.locator("input:not([type='hidden']):not([type='file'])"
                               ":not([type='radio']):not([type='checkbox']),"
                               " select, textarea").all():
            label = ""
            try:
                label = " ".join(el.evaluate(
                    "e => e.labels && e.labels[0] ? e.labels[0].innerText : ''").split())
            except PWTimeout:
                pass
            if not label:
                continue
            if el.input_value():
                continue
            tag = el.evaluate("e => e.tagName").lower()
            option_texts = None
            if tag == "select":
                option_texts = [t.strip() for t in
                                el.locator("option").all_inner_texts() if t.strip()]
            answer = resolve_answer(label, profile,
                                    prepared=item.application.answers,
                                    options=option_texts, job=item.job)
            if answer is None:
                return label
            if tag == "select":
                try:
                    el.select_option(label=str(answer))
                except Exception:
                    return label
            else:
                el.fill(str(answer))

        # Radio groups (employer questions) — same JS approach as LinkedIn
        for group in self._radio_groups(page):
            if any(o["checked"] for o in group["options"]):
                continue
            question = " ".join(group["question"].split()).rstrip("*").strip()
            if not question:
                continue  # e.g. resume-choice cards default to selected
            option_texts = [o["text"] for o in group["options"] if o["text"]]
            answer = resolve_answer(question, profile,
                                    prepared=item.application.answers,
                                    options=option_texts, job=item.job)
            if answer is None:
                return question
            if not self._click_radio(page, group["name"], str(answer)):
                return question
        return None

    def _radio_groups(self, page: Page) -> list[dict]:
        return page.evaluate("""() => {
          const groups = {};
          [...document.querySelectorAll('input[type=radio]')].forEach((r, idx) => {
            const name = r.name || ('__g' + idx);
            if (!groups[name]) {
              let q = '';
              const fs = r.closest('fieldset');
              if (fs) {
                const legend = fs.querySelector('legend');
                q = legend ? legend.textContent.trim() : '';
              }
              groups[name] = {name, question: q, options: []};
            }
            const lbl = (r.labels && r.labels[0])
                          ? r.labels[0].textContent.trim() : '';
            groups[name].options.push({text: lbl, checked: r.checked});
          });
          return Object.values(groups);
        }""")

    def _click_radio(self, page: Page, name: str, answer: str) -> bool:
        return page.evaluate("""(args) => {
          const radios = [...document.querySelectorAll(
              'input[type=radio][name="' + CSS.escape(args.name) + '"]')];
          const textOf = r => ((r.labels && r.labels[0])
              ? r.labels[0].textContent : '').trim().toLowerCase();
          const want = args.answer.toLowerCase();
          const hit = radios.find(r => textOf(r) === want)
                   || radios.find(r => textOf(r).includes(want));
          if (!hit) return false;
          (hit.labels && hit.labels[0] ? hit.labels[0] : hit).click();
          return true;
        }""", {"name": name, "answer": answer})
