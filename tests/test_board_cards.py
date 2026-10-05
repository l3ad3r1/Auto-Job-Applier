"""Card parsing for the Foundit and Shine adapters, against the markup shapes
observed on their live public results pages on 2026-10-05.

Runs without pytest, like tests/test_linkedin_send_message.py: the `__main__`
block hands each test a fresh Playwright page. The HTML below is trimmed from
the live structure with the content anonymised; selectors are what the adapters
use, so a board changing its markup shows up as a failure here AND as a drop in
`doctor`'s card count.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.foundit import FounditAdapter
from adapters.shine import ShineAdapter

FOUNDIT_QUICK = """
<div id="69334899" class="cardContainer">
  <div class="cardHead"><div class="infoSection">
    <div id="jobCardTitle" class="jobTitle"> Sales Manager </div>
    <div class="companyName"><p> Example Finance </p></div></div></div>
  <div class="cardBody">
    <div class="experienceSalary">
      <div class="bodyRow"><span class="details">2 - 5 Years</span></div>
      <div class="bodyRow"><span class="details">INR 8 - 12 LPA</span></div></div>
    <div class="bodyRow"><div class="details location"> Hyderabad </div></div>
    <div class="jobTags"><div class="cardApplyLabel">Early Applicant</div>
      <div class="cardApplyLabel">Quick Apply</div></div></div>
  <div class="cardFooter"><div class="jobAddedTime"><p class="timeText"> Posted a day ago </p></div></div>
</div>"""

FOUNDIT_PLAIN = FOUNDIT_QUICK.replace("Quick Apply", "").replace("69334899", "69333577")

SHINE_CARD = """
<article class="result-card_card__p42UB" itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
  <meta itemprop="url" content="https://www.shine.com/jobs/branch-manager/example-co/19668609">
  <a class="result-card_hit__tL8tN" href="/jobs/branch-manager/example-co/19668609?position_in_results=1"></a>
  <div class="result-card_co__QjJzU"><span class="result-card_company__V6mN_">Example Co</span>
    <span class="result-card_posted__i_x9C">3 days ago</span></div>
  <h3 class="result-card_role__tUHBe" itemprop="name">Branch Manager</h3>
  <div class="result-card_meta__tTsve">
    <span class="result-card_meta-item__3ge8b">6 to 11 Yrs</span>
    <span class="result-card_meta-item__3ge8b">12 - 14 Lakh/Yr</span>
    <span class="result-card_meta-item__3ge8b">Hyderabad</span></div>
  <button class="result-card_apply__D9pKa">Apply</button>
</article>"""


def test_foundit_card_fields(page):
    page.set_content(FOUNDIT_QUICK)
    job = FounditAdapter()._parse_card(page.locator("div.cardContainer").first)
    assert job.external_id == "69334899"
    assert job.title == "Sales Manager" and job.company == "Example Finance"
    assert job.location == "Hyderabad"
    assert job.salary == "INR 8 - 12 LPA"          # experience row is not salary
    assert job.posted_age_days == 1                # "Posted a day ago"
    assert job.url == "https://www.foundit.in/job/69334899"


def test_foundit_easy_apply_only_when_tagged_quick_apply(page):
    adapter = FounditAdapter()
    page.set_content(FOUNDIT_QUICK)
    assert adapter._parse_card(page.locator("div.cardContainer").first).easy_apply is True
    page.set_content(FOUNDIT_PLAIN)
    assert adapter._parse_card(page.locator("div.cardContainer").first).easy_apply is False


def test_foundit_card_without_numeric_id_is_rejected(page):
    page.set_content(FOUNDIT_QUICK.replace('id="69334899"', 'id="ad-banner"'))
    assert FounditAdapter()._parse_card(page.locator("div.cardContainer").first) is None


def test_shine_card_fields(page):
    page.set_content(SHINE_CARD)
    job = ShineAdapter()._parse_card(page.locator("article").first)
    assert job.external_id == "19668609"
    assert job.title == "Branch Manager" and job.company == "Example Co"
    assert job.location == "Hyderabad"             # last meta item, not salary
    assert job.salary == "12 - 14 Lakh/Yr"
    assert job.posted_age_days == 3
    assert job.url.endswith("/19668609")


def test_shine_card_survives_a_rotated_class_hash(page):
    """CSS-module hashes rotate on every Shine deploy. Selectors must hold the
    stable prefix only."""
    rotated = SHINE_CARD.replace("__QjJzU", "__ZZZZZ").replace("__V6mN_", "__AAAAA") \
        .replace("__i_x9C", "__BBBBB").replace("__3ge8b", "__CCCCC")
    page.set_content(rotated)
    job = ShineAdapter()._parse_card(page.locator("article").first)
    assert job and job.company == "Example Co" and job.posted_age_days == 3
    assert job.salary == "12 - 14 Lakh/Yr"


def test_shine_is_never_marked_easy_apply(page):
    """The card says only "Apply" for every job, so it proves nothing."""
    page.set_content(SHINE_CARD)
    assert ShineAdapter()._parse_card(page.locator("article").first).easy_apply is False


def test_new_boards_apply_is_inert_until_verified():
    """No click, no navigation: a guessed success marker would record
    applications that never happened. Passing None for page proves it."""
    from core.models import Job, QueuedItem
    for adapter, host in ((FounditAdapter(), "foundit.in"), (ShineAdapter(), "shine.com")):
        job = Job(platform=adapter.platform, external_id="1", title="t", company="c",
                  url=f"https://www.{host}/job/1")
        item = QueuedItem.__new__(QueuedItem)
        item.job = job
        result = adapter.apply(None, item, None)
        assert result.ok is False and "not verified" in result.note


if __name__ == "__main__":
    import inspect

    from playwright.sync_api import sync_playwright

    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        passed = failed = 0
        for t in tests:
            page = browser.new_page()
            try:
                t(page) if inspect.signature(t).parameters else t()
                print(f"PASS {t.__name__}")
                passed += 1
            except AssertionError as e:
                print(f"FAIL {t.__name__}: {e}")
                failed += 1
            finally:
                page.close()
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
