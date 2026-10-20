"""LinkedIn adapter: job search + Easy Apply submission.

LinkedIn serves (at least) two DOMs: a legacy one with semantic classes
(.jobs-apply-button, .fb-dash-form-element) and a new one with hashed class
names where only roles, aria-labels, and <label> associations are stable.
Everything here prefers aria/role/label selectors and keeps legacy class
selectors only as fallbacks.
"""
from __future__ import annotations

import re
import urllib.parse

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from core.models import Job, QueuedItem
from core.profile import load_profile
from llm.answers import resolve_answer
from .base import ApplyResult, BaseAdapter, parse_posted_age

JOB_CARD_SELECTORS = [
    ".scaffold-layout__list [data-view-name='job-card'][data-job-id]",
    ".scaffold-layout__list div[data-job-id]",
    "div[data-occludable-job-id]",
    ".job-card-container",
]

_APPLICANTS_RE = re.compile(r"(\d+)\s+applicants?", re.I)


def _parse_applicants(text: str) -> int:
    m = _APPLICANTS_RE.search(text or "")
    return int(m.group(1)) if m else -1


NEXT_TEXTS = ("next", "review", "continue", "continue to next step")
SUBMIT_TEXTS = ("submit application", "submit")


def _clean(text: str) -> str:
    """Normalize a label: collapse whitespace, drop the required-* marker."""
    return " ".join(text.split()).rstrip("*").strip()


class LinkedInAdapter(BaseAdapter):
    platform = "linkedin"
    login_url = "https://www.linkedin.com/login"

    # -- session ---------------------------------------------------------------

    def is_logged_in(self, page: Page) -> bool:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        return "/login" not in page.url and "authwall" not in page.url

    # -- discovery ---------------------------------------------------------------

    def _search_url(self, keyword: str, location: str, cfg: dict) -> str:
        params = {
            "keywords": keyword,
            "location": location,
            "f_TPR": f"r{cfg.get('posted_within', 604800)}",
        }
        if cfg.get("easy_apply_only", True):
            params["f_AL"] = "true"
        return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)

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
                page.wait_for_timeout(4000)
                self._scroll_results(page)

                cards = self._job_cards(page)
                print(f"  [{keyword} / {location}] {len(cards)} cards")
                for card in cards:
                    job = self._parse_card(card)
                    if not job:
                        continue
                    job.source_tag = str(cfg.get("source_tag", ""))
                    if any(x in job.title.lower() for x in exclude):
                        continue
                    jobs[job.external_id] = job
                    if len(jobs) >= batch_limit:
                        break
        return list(jobs.values())

    def _job_cards(self, page: Page) -> list[Locator]:
        for sel in JOB_CARD_SELECTORS:
            cards = page.locator(sel).all()
            if cards:
                return cards
        return []

    def _scroll_results(self, page: Page) -> None:
        """Scroll the virtualized results list until no new cards render."""
        cards = self._job_cards(page)
        if not cards:
            return
        try:
            cards[0].hover()
        except PWTimeout:
            return
        last_count = len(cards)
        for _ in range(15):
            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(900)
            count = len(self._job_cards(page))
            if count <= last_count:
                break
            last_count = count

    def _parse_card(self, card: Locator) -> Job | None:
        job_id = ""
        for attr in ("data-occludable-job-id", "data-job-id"):
            v = card.get_attribute(attr) or ""
            if v.isdigit():
                job_id = v
                break
        if not job_id:
            href = ""
            link = card.locator("a[href*='/jobs/view/']").first
            if link.count():
                href = link.get_attribute("href") or ""
            m = re.search(r"/jobs/view/(\d+)", href)
            if not m:
                return None
            job_id = m.group(1)

        def text_of(*sels: str) -> str:
            for s in sels:
                loc = card.locator(s).first
                if loc.count():
                    t = (loc.inner_text() or "").strip().split("\n")[0]
                    if t:
                        return t
            return ""

        title = text_of("a[href*='/jobs/view/'] strong", ".job-card-list__title",
                        "a[href*='/jobs/view/']", ".artdeco-entity-lockup__title")
        company = text_of(".artdeco-entity-lockup__subtitle",
                          ".job-card-container__primary-description",
                          ".job-card-container__company-name")
        location = text_of(".artdeco-entity-lockup__caption",
                           ".job-card-container__metadata-item")
        if not title:
            return None

        # Everything below comes from text already rendered in the card, so it
        # costs no extra page loads and adds no platform traffic.
        #
        # Posting age is usually ABSENT here, unlike Naukri and Indeed. Verified
        # against a real search-results capture: of ~13 cards, one carried a
        # date and nine were "Promoted", which never do. The age is only on the
        # job's own page, and fetching that per listing is exactly the traffic
        # that risks the LinkedIn account. So age stays -1 for most LinkedIn
        # rows and scores neutral by design -- see _freshness_points.
        try:
            card_text = (card.inner_text() or "")
        except Exception:
            card_text = ""

        # easy_apply used to be hardcoded True for every LinkedIn result. That
        # is why "Easy Apply button/modal not found" was the single largest
        # failure reason: external-apply jobs sailed through the require_easy_apply
        # gate and only failed once a browser had driven to the apply button.
        easy = "easy apply" in card_text.lower()

        return Job(
            platform=self.platform, external_id=job_id, title=title,
            company=company, location=location,
            url=f"https://www.linkedin.com/jobs/view/{job_id}",
            easy_apply=easy,
            posted_age_days=parse_posted_age(card_text),
            applicants=_parse_applicants(card_text),
        )

    # -- referral messaging ---------------------------------------------------------

    def send_message(self, page: Page, profile_url: str, text: str,
                      name: str = "") -> tuple[bool, str]:
        """Send one LinkedIn message from the person's own profile page.

        Recipient correctness is guaranteed structurally: we navigate to the
        profile, confirm the URL is still that profile, click the profile's
        own Message BUTTON, and confirm the compose panel that opens is
        addressed to that person before typing anything.

        Never match anchors. A profile page carries `a:has-text('Message')`
        links for the "People also viewed" sidebar -- probing one profile found
        three, labelled "Message Sonia S", "Message Shreya Roy", "Message Shiv
        Purohit". Clicking one of those sends the referral request to a
        completely different person. Likewise the send control is matched by
        class, because `button:has-text('Send')` matches two elements.

        `button:has-text('Message')` is also not safe unscoped: Playwright's
        `:has-text` is a case-insensitive substring match, and every LinkedIn
        page carries a global "Compose message" button in the chat dock --
        "Compose message" contains "message". On a profile with no real
        Message button, that global control silently satisfied the selector
        and opened a blank draft with no recipient (Send stayed disabled, for
        a reason that took three live attempts to find). Scoping to the
        profile's own intro section, and checking the button's aria-label
        names the person, makes that failure mode impossible instead of rare.

        Returns (ok, note) and never raises: a failed send must not take down a
        run, and must never be retried blindly into a duplicate message.
        """
        slug = profile_url.rstrip("/").rsplit("/in/", 1)[-1].lower()
        first_name = (name.split() or [""])[0].lower()
        try:
            page.goto(profile_url, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

            if slug and slug not in page.url.lower():
                return False, f"redirected away from the profile ({page.url[:60]})"

            card = page.locator("main section").first
            btn = card.locator("button:has-text('Message')").first
            if not btn.count() or not btn.is_visible():
                return False, "no Message button (not a 1st-degree connection?)"
            label = (btn.get_attribute("aria-label") or "").lower()
            if first_name and first_name not in label:
                return False, f"Message button doesn't name {name!r} (aria-label={label!r})"
            btn.click()
            page.wait_for_timeout(3000)

            header = page.locator(
                ".msg-overlay-bubble-header__title, "
                ".msg-overlay-conversation-bubble__link, "
                ".msg-thread__link-to-profile").first
            recipient = ""
            try:
                recipient = (header.inner_text(timeout=5000) or "").strip()
            except Exception:
                pass
            if not recipient or (first_name and first_name not in recipient.lower()):
                return False, f"compose opened without {name!r} as recipient (saw {recipient!r})"

            box = page.locator(
                "div[contenteditable='true'][role='textbox'], "
                "div.msg-form__contenteditable[contenteditable='true']").first
            try:
                box.wait_for(state="visible", timeout=12000)
            except PWTimeout:
                return False, "message box did not open"

            # focus(), not click(): the overlay animates in and the box is
            # visible-but-not-clickable for long enough to blow a 30s timeout.
            # A contenteditable only needs focus to receive typed text.
            try:
                box.focus()
            except Exception:
                box.click(force=True)
            # press_sequentially, not insert_text. insert_text uses the CDP
            # Input.insertText path, which puts the characters in the box but
            # does not fire the input events LinkedIn's React form listens for
            # -- the text was visibly there and Send stayed disabled.
            try:
                box.press_sequentially(text, delay=8)
            except Exception:
                page.keyboard.type(text, delay=8)
            page.wait_for_timeout(2000)

            # Never press Send on a message we cannot see in the box. An empty
            # or half-typed referral request is worse than none: it cannot be
            # unsent, and it spends the connection.
            typed = ""
            try:
                typed = box.inner_text() or ""
            except Exception:
                pass
            probe = text.splitlines()[0][:18] if text.strip() else ''
            if probe and probe not in typed:
                return False, f"text did not land in the box (saw {typed[:40]!r})"

            send = page.locator("button.msg-form__send-button").first
            if not send.count():
                return False, "send button not found"
            if send.is_disabled():
                return False, "send button disabled (text did not register?)"
            send.click()
            page.wait_for_timeout(3500)
            return True, "sent"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {str(e)[:120]}"

    # -- application modal ---------------------------------------------------------

    def _modal(self, page: Page) -> Locator:
        # New DOM uses a native <dialog>; legacy uses role=dialog / semantic class
        return page.locator("dialog:visible, [role='dialog']:visible, "
                            ".jobs-easy-apply-modal:visible").first

    def _open_easy_apply(self, page: Page, item: QueuedItem) -> bool:
        page.goto(item.job.url, wait_until="domcontentloaded")
        btn = page.locator("button[aria-label*='Easy Apply'], "
                           "button.jobs-apply-button").first
        try:
            btn.wait_for(state="visible", timeout=20000)
        except PWTimeout:
            return False  # slow load, or the job has no Easy Apply (external)
        btn.click()
        try:
            self._modal(page).wait_for(state="visible", timeout=10000)
            return True
        except PWTimeout:
            return False

    def _modal_inputs(self, page: Page) -> list[Locator]:
        modal = self._modal(page)
        return modal.locator(
            "input:not([type='hidden']):not([type='file']), select, textarea").all()

    def _wait_form_ready(self, page: Page, timeout_ms: int = 12000) -> bool:
        """The dialog appears before its content hydrates — wait for the footer."""
        waited = 0
        while waited < timeout_ms:
            if self._footer_button(page)[0] is not None:
                return True
            page.wait_for_timeout(500)
            waited += 500
        return False

    def _progress(self, page: Page) -> str:
        """The 'N/M pages' marker, used to detect being stuck on one page."""
        try:
            m = re.search(r"\d+\s*/\s*\d+\s*page", self._modal(page).inner_text())
            return m.group(0) if m else ""
        except PWTimeout:
            return ""

    def _invalid_field_label(self, page: Page) -> str:
        """Label of the first field the browser/LinkedIn marked invalid."""
        for el in self._modal_inputs(page):
            try:
                if (el.get_attribute("aria-invalid") == "true"
                        or el.evaluate("e => e.matches(':invalid')")):
                    return self._label_for(el) or "(unlabeled field)"
            except PWTimeout:
                continue
        err = self._modal(page).locator(
            "[role='alert'], .artdeco-inline-feedback--error").first
        if err.count():
            try:
                return _clean(err.inner_text())
            except PWTimeout:
                pass
        return ""

    def _label_for(self, el: Locator) -> str:
        """Question text for an input.

        Tries <label> association, then fieldset legend, then — for the new
        DOM, where question text floats unassociated above the input — the
        first text line of the nearest ancestor that has any text.
        """
        try:
            return _clean(el.evaluate(
                """e => {
                  if (e.labels && e.labels[0] && e.labels[0].innerText.trim())
                    return e.labels[0].innerText;
                  const fs = e.closest('fieldset');
                  const lg = fs && fs.querySelector('legend');
                  if (lg && lg.textContent.trim()) return lg.textContent;
                  let n = e.parentElement;
                  for (let i = 0; i < 5 && n; i++) {
                    const t = (n.innerText || '').trim();
                    if (t) {
                      // first line that reads like a question — not a
                      // char counter (0/20) or other numeric chrome
                      const line = t.split('\\n').map(s => s.trim()).find(
                        s => /[A-Za-z]{3}/.test(s)
                          && !/^\\d+\\s*(\\/|of)\\s*\\d+/i.test(s));
                      if (line) return line;
                    }
                    n = n.parentElement;
                  }
                  return '';
                }"""))
        except PWTimeout:
            return ""

    def _footer_button(self, page: Page) -> tuple[Locator | None, str]:
        """The modal's advance button: ('submit'|'next'|'', locator)."""
        modal = self._modal(page)
        for btn in modal.get_by_role("button").all():
            try:
                t = _clean(btn.inner_text() or "").lower()
            except PWTimeout:
                continue
            if t in SUBMIT_TEXTS:
                return btn, "submit"
            if t in NEXT_TEXTS:
                return btn, "next"
        return None, ""

    def _next_or_submit(self, page: Page) -> str:
        btn, kind = self._footer_button(page)
        if btn is None:
            return ""
        if kind == "submit":
            # Untick "follow company" if present and checked
            follow = self._modal(page).locator("input[type='checkbox'][id*='follow']").first
            try:
                if follow.count() and follow.is_checked():
                    follow.evaluate("e => e.labels && e.labels[0] && e.labels[0].click()")
            except PWTimeout:
                pass
            btn.click()
            return "submitted"
        btn.click()
        return "next"

    def _discard(self, page: Page) -> None:
        """Close the modal WITHOUT submitting (dismiss → discard)."""
        try:
            page.locator("button[aria-label='Dismiss']").first.click(timeout=3000)
            page.wait_for_timeout(1000)
            discard = page.get_by_role("button", name=re.compile("discard", re.I)).first
            if discard.count():
                discard.click(timeout=3000)
        except PWTimeout:
            pass

    def _dismiss_post_submit(self, page: Page) -> None:
        """Close the confirmation dialog that follows a successful submit."""
        for name in ("Dismiss", "Done", "Not now"):
            btn = page.locator(f"button[aria-label='{name}']").first
            if not btn.count():
                btn = page.get_by_role("button", name=name).first
            try:
                if btn.count() and btn.is_visible():
                    btn.click(timeout=3000)
                    return
            except PWTimeout:
                continue

    def collect_questions(self, page: Page, item: QueuedItem) -> list[str]:
        """Walk the Easy Apply form collecting question labels, then discard."""
        if not self._open_easy_apply(page, item):
            return []
        questions: list[str] = []
        for _ in range(8):
            for el in self._modal_inputs(page):
                label = self._label_for(el)
                if label and label not in questions:
                    questions.append(label)
            _, kind = self._footer_button(page)
            if kind != "next":
                break
            self._next_or_submit(page)
            page.wait_for_timeout(1500)
        self._discard(page)
        return questions

    # -- submission ---------------------------------------------------------------

    def apply(self, page: Page, item: QueuedItem, limiter) -> ApplyResult:
        profile = load_profile()
        answers = dict(item.application.answers)
        if not self._open_easy_apply(page, item):
            return self.closed_posting(page) or ApplyResult(
                False, "Easy Apply button/modal not found")

        last_progress = None
        for _ in range(12):
            if not self._wait_form_ready(page):
                self._discard(page)
                return ApplyResult(False, "Form never became ready (no footer button)")
            limiter.action_delay()
            unanswerable = self._fill_page(page, answers, profile, item.job)
            if unanswerable:
                self._discard(page)
                return ApplyResult(
                    False, f"Unanswered required question: {unanswerable!r}")
            progress_before = self._progress(page)
            outcome = self._next_or_submit(page)
            if outcome == "submitted":
                page.wait_for_timeout(3000)
                self._dismiss_post_submit(page)
                return ApplyResult(True)
            if outcome == "":
                self._discard(page)
                return ApplyResult(False, "No Next/Submit button found in modal")
            page.wait_for_timeout(1800)
            # A rejected Next keeps the modal on the same page — find out why
            progress_now = self._progress(page)
            if progress_now and progress_now == progress_before == last_progress:
                why = self._invalid_field_label(page) or "unknown validation failure"
                self._discard(page)
                return ApplyResult(False, f"Stuck at {progress_now}: {why}")
            last_progress = progress_now

        self._discard(page)
        return ApplyResult(False, "Form did not reach Submit within 12 pages")

    def _fill_page(self, page: Page, answers: dict, profile: dict,
                   job: Job | None = None) -> str | None:
        """Fill every field on the current modal page.

        Returns the label of a required field we couldn't answer, or None.
        Prefilled values (LinkedIn fills contact info itself) are left alone.
        Answer order: prepared answers -> profile map -> local LLM (grounded;
        UNKNOWN falls through to human review).
        """
        # Radio groups first — handled in JS because the new DOM hides their
        # question/option text from label associations (see _radio_groups)
        for group in self._radio_groups(page):
            if any(o["checked"] for o in group["options"]):
                continue
            question = _clean(group["question"])
            if not question:
                return "(radio group with no question text)"
            option_texts = [o["text"] for o in group["options"] if o["text"]]
            answer = resolve_answer(question, profile, prepared=answers,
                                    options=option_texts, job=job)
            if answer is None:
                return question
            if not self._click_radio(page, group["name"], str(answer)):
                return question

        for el in self._modal_inputs(page):
            itype = (el.get_attribute("type") or "").lower()
            if itype in ("radio", "checkbox"):
                continue  # radios handled above; never tick agreement checkboxes
            tag = el.evaluate("e => e.tagName").lower()
            label = self._label_for(el)
            if not label:
                continue

            if tag == "select":
                current = (el.input_value() or "").strip().lower()
                if current not in ("", "select an option"):
                    continue
                option_texts = [t.strip() for t in
                                el.locator("option").all_inner_texts()
                                if t.strip().lower() != "select an option"]
                answer = resolve_answer(label, profile, prepared=answers,
                                        options=option_texts, job=job)
                if answer is None:
                    return label
                try:
                    el.select_option(label=str(answer))
                except Exception:
                    return label  # answer matches no option — human call

            else:  # text-ish input or textarea
                if el.input_value():
                    continue
                answer = resolve_answer(label, profile, prepared=answers, job=job)
                if answer is None:
                    return label
                el.fill(str(answer))
        return None

    _DIALOG_JS = ("document.querySelector('dialog')"
                  " || document.querySelector('[role=\"dialog\"]')")

    def _radio_groups(self, page: Page) -> list[dict]:
        """[{name, question, options: [{text, checked}]}] for the current page.

        New-DOM quirks this works around: the question text sits OUTSIDE the
        <fieldset> (no <legend>), and option labels have empty innerText —
        only textContent, or an ancestor div, carries the visible word.
        """
        return page.evaluate("""() => {
          const dlg = """ + self._DIALOG_JS + """;
          if (!dlg) return [];
          const groups = {};
          [...dlg.querySelectorAll('input[type=radio]')].forEach((r, idx) => {
            const name = r.name || ('__g' + idx);
            if (!groups[name]) {
              let q = '';
              const fs = r.closest('fieldset');
              if (fs) {
                const legend = fs.querySelector('legend');
                if (legend && legend.textContent.trim()) {
                  q = legend.textContent.trim();
                } else if (fs.parentElement) {
                  q = (fs.parentElement.textContent || '')
                        .replace(fs.textContent || '', '').trim();
                }
              }
              groups[name] = {name, question: q, options: []};
            }
            let text = (r.labels && r.labels[0])
                         ? r.labels[0].textContent.trim() : '';
            if (!text) {
              let n = r.parentElement;
              for (let i = 0; i < 3 && n && !text; i++) {
                text = (n.innerText || '').trim();
                n = n.parentElement;
              }
            }
            groups[name].options.push(
              {text: text.split('\\n')[0].trim(), checked: r.checked});
          });
          return Object.values(groups);
        }""")

    def _click_radio(self, page: Page, name: str, answer: str) -> bool:
        """Click the radio in group `name` whose text matches (exact, then substring)."""
        return page.evaluate("""(args) => {
          const dlg = """ + self._DIALOG_JS + """;
          const radios = [...dlg.querySelectorAll(
              'input[type=radio][name="' + CSS.escape(args.name) + '"]')];
          const textOf = r => {
            let t = (r.labels && r.labels[0]) ? r.labels[0].textContent.trim() : '';
            if (!t) { let n = r.parentElement;
              for (let i = 0; i < 3 && n && !t; i++) {
                t = (n.innerText || '').trim(); n = n.parentElement; } }
            return t.split('\\n')[0].trim().toLowerCase();
          };
          const want = args.answer.toLowerCase();
          let hit = radios.find(r => textOf(r) === want)
                 || radios.find(r => textOf(r) && textOf(r).includes(want));
          if (!hit) return false;
          (hit.labels && hit.labels[0] ? hit.labels[0] : hit).click();
          return true;
        }""", {"name": name, "answer": answer})
