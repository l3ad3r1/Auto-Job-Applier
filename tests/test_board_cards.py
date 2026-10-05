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


def test_shine_apply_is_inert_until_verified():
    """No click, no navigation: a guessed success marker would record
    applications that never happened. Passing None for page proves it."""
    from core.models import Job, QueuedItem
    adapter = ShineAdapter()
    job = Job(platform="shine", external_id="1", title="t", company="c",
              url="https://www.shine.com/jobs/x/y/1")
    item = QueuedItem.__new__(QueuedItem)
    item.job = job
    result = adapter.apply(None, item, None)
    assert result.ok is False and "not verified" in result.note


# --- Foundit apply, against a served copy of the page structure -----------------
# The live job page repeats id="applyBtn" on SEVEN buttons: a hidden sticky copy,
# the main one, and one per "Recommended Jobs" card. Clicking the wrong one marked
# an unrelated job as applied in the user's history (2026-10-05).

def _job_page(main_label="Quick Apply", extra_main=False, applied=False):
    on_main = ("window.__clicked = 'main'; document.body.insertAdjacentHTML("
               "'beforeend', '<p>Your application status</p>')")
    main = ('<button id="applyBtn" class="button button--primary applyJob__btn jobMain" '
            f'onclick="{on_main}">{main_label}</button>')
    sidebar = "".join(
        '<button id="applyBtn" class="button button--secondary applyJob applyJob__card" '
        'onclick="window.__clicked = \'sidebar\'">Apply Now</button>' for _ in range(3))
    sticky = ('<button id="applyBtn" class="button button--primary applyJob__btn jobScroll" '
              'style="display:none">Quick Apply</button>')
    status = "<p>Your application status</p>" if applied else ""
    return ("<html><body><h1>Sales Manager</h1>" + sticky + main
            + (main if extra_main else "") + sidebar + status + "</body></html>")


class _NoDelay:
    def action_delay(self):
        pass


def _run_foundit_apply(page, html):
    from core.models import Job, QueuedItem
    page.route("https://www.foundit.in/job/*",
               lambda route: route.fulfill(body=html, content_type="text/html"))
    job = Job(platform="foundit", external_id="1", title="t", company="c",
              url="https://www.foundit.in/job/1")
    item = QueuedItem.__new__(QueuedItem)
    item.job = job
    result = FounditAdapter().apply(page, item, _NoDelay())
    return result, page.evaluate("window.__clicked")


def test_foundit_apply_clicks_only_the_main_button(page):
    result, clicked = _run_foundit_apply(page, _job_page())
    assert clicked == "main"          # never one of the sidebar cards
    assert result.ok is True


def test_foundit_main_button_that_is_not_quick_apply_is_not_clicked(page):
    """"Apply Now" on the main button redirects to the company's own site."""
    result, clicked = _run_foundit_apply(page, _job_page(main_label="Apply Now"))
    assert clicked is None
    assert result.ok is False and "external" in result.note


def test_foundit_refuses_to_guess_between_two_main_buttons(page):
    result, clicked = _run_foundit_apply(page, _job_page(extra_main=True))
    assert clicked is None
    assert result.ok is False and "ambiguous" in result.note


def test_foundit_already_applied_is_success_without_clicking(page):
    result, clicked = _run_foundit_apply(page, _job_page(applied=True))
    assert clicked is None
    assert result.ok is True and "already" in result.note


# --- Foundit paging, against served fake result pages --------------------------

def _card(job_id, location="Hyderabad, India", title="Key Account Manager"):
    return (FOUNDIT_QUICK.replace("69334899", str(job_id))
            .replace("Hyderabad </div>", location + " </div>")
            .replace("Sales Manager", title))


def _serve_foundit(page, catalog):
    """catalog[(keyword, start)] -> list of card html. Records each request."""
    from urllib.parse import parse_qs, urlparse
    seen = []

    def handler(route):
        q = parse_qs(urlparse(route.request.url).query)
        key = (q["query"][0], int(q.get("start", ["0"])[0]))
        seen.append(key)
        route.fulfill(body="<html><body>" + "".join(catalog.get(key, [])) + "</body></html>",
                      content_type="text/html")

    page.route("https://www.foundit.in/srp/results*", handler)
    page.wait_for_timeout = lambda ms: None     # no real waiting in tests
    return seen


def _cfg(keywords, batch=40):
    return {"search": {"keywords": keywords, "locations": ["Hyderabad, Telangana, India"]},
            "limits": {"foundit_batch": batch}}


def test_foundit_pages_through_results_and_stops_at_an_empty_page(page):
    catalog = {("kam", 0): [_card(1), _card(2)], ("kam", 15): [_card(3)],
               ("kam", 30): [_card(4)]}
    seen = _serve_foundit(page, catalog)
    jobs = FounditAdapter().search(page, _cfg(["kam"]))
    assert [j.external_id for j in jobs] == ["1", "2", "3", "4"]
    assert seen == [("kam", 0), ("kam", 15), ("kam", 30)]      # MAX_PAGES, no 4th


def test_foundit_drops_cards_outside_the_search_area(page):
    catalog = {("kam", 0): [_card(1), _card(2, "India, Sindhnur"), _card(3, "Gurugram, India"),
                            _card(4, "Secunderabad, India")]}
    _serve_foundit(page, catalog)
    ids = [j.external_id for j in FounditAdapter().search(page, _cfg(["kam"]))]
    assert ids == ["1", "4"]


def test_foundit_stops_paging_a_keyword_with_nothing_in_area(page):
    """Deeper pages of an all-India result set are just more of the same."""
    catalog = {("bank", 0): [_card(1, "Mumbai, India"), _card(2, "India")],
               ("bank", 15): [_card(3)], ("kam", 0): [_card(9)]}
    seen = _serve_foundit(page, catalog)
    jobs = FounditAdapter().search(page, _cfg(["bank", "kam"]))
    assert ("bank", 15) not in seen                 # page 2 never requested
    assert [j.external_id for j in jobs] == ["9"]


def test_foundit_pages_breadth_first_so_no_keyword_is_starved(page):
    catalog = {("a", 0): [_card(1), _card(2)], ("a", 15): [_card(3), _card(4)],
               ("b", 0): [_card(11)], ("b", 15): [_card(12)]}
    seen = _serve_foundit(page, catalog)
    FounditAdapter().search(page, _cfg(["a", "b"]))
    assert seen[:4] == [("a", 0), ("b", 0), ("a", 15), ("b", 15)]


def test_foundit_respects_the_batch_limit(page):
    catalog = {("kam", 0): [_card(i) for i in range(1, 8)], ("kam", 15): [_card(20)]}
    seen = _serve_foundit(page, catalog)
    jobs = FounditAdapter().search(page, _cfg(["kam"], batch=5))
    assert len(jobs) == 5 and ("kam", 15) not in seen


def test_search_city_takes_the_city_from_the_configs_free_text_address():
    """config.yaml holds ONE string, "Hyderabad, Telangana, India". Both boards
    were handed that whole string in the URL."""
    from adapters.base import search_city
    assert search_city({"locations": ["Hyderabad, Telangana, India"]}) == "Hyderabad"
    assert search_city({"locations": [" Mumbai , India"]}) == "Mumbai"
    assert search_city({"locations": []}) == "Hyderabad"
    assert search_city({}) == "Hyderabad"


def test_foundit_requests_a_city_not_the_whole_address(page):
    urls = []
    page.route("https://www.foundit.in/srp/results*",
               lambda route: (urls.append(route.request.url),
                              route.fulfill(body="<html></html>", content_type="text/html")))
    page.wait_for_timeout = lambda ms: None
    FounditAdapter().search(page, _cfg(["kam"]))
    assert urls and "locations=hyderabad" in urls[0]
    assert "telangana" not in urls[0].lower() and "india" not in urls[0].lower()


def test_shine_requests_a_city_not_the_whole_address(page):
    urls = []
    page.route("https://www.shine.com/job-search/*",
               lambda route: (urls.append(route.request.url),
                              route.fulfill(body="<html></html>", content_type="text/html")))
    page.wait_for_timeout = lambda ms: None
    ShineAdapter().search(page, _cfg(["Key Account Manager"]))
    assert urls and urls[0].endswith("/key-account-manager-jobs-in-hyderabad")


def test_foundit_batch_defaults_to_three_times_discover_batch(page):
    catalog = {("kam", 0): [_card(i) for i in range(1, 15)],
               ("kam", 15): [_card(i) for i in range(20, 34)],
               ("kam", 30): [_card(i) for i in range(40, 54)]}
    _serve_foundit(page, catalog)
    cfg = {"search": {"keywords": ["kam"], "locations": ["Hyderabad, Telangana, India"]},
           "limits": {"discover_batch": 10}}           # no foundit_batch set
    jobs = FounditAdapter().search(page, cfg)
    assert len(jobs) == 30                              # 3 x 10, across 3 pages


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
