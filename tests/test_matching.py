"""Ranking behaviour of core.matching.score_job.

These assert ORDERING, not exact numbers, so the weights can be retuned
without rewriting the tests. The one thing that must not regress is the
property the previous scorer lacked: that better matches outrank worse ones.
"""
from core.matching import is_match, parse_salary_lpa, score_job
from core.models import Job

SPEC = {
    "min_score": 60,
    "min_salary_lpa": 7.8,
    "target_titles": ["branch manager", "area sales manager", "sales manager",
                      "business development manager"],
    "industries": ["consumer electronics", "retail", "b2b"],
    "preferred_locations": ["hyderabad"],
    "reject": ["it staffing"],
}


def job(title, **kw):
    kw.setdefault("location", "Hyderabad, Telangana, India")
    kw.setdefault("easy_apply", True)
    kw.setdefault("posted_age_days", 2)
    return Job(platform="linkedin", external_id="1", title=title,
               company="Acme", **kw)


def s(title, **kw):
    return score_job(job(title, **kw), SPEC).score


def test_specific_title_outranks_generic():
    assert s("Area Sales Manager") > s("Sales Manager")


def test_title_as_qualifier_ranks_below_title_as_role():
    assert s("Sales Manager") > s("Intern supporting the Sales Manager")


def test_junior_titles_rank_below_target_seniority():
    assert s("Regional Sales Manager") > s("Sales Manager Trainee")


def test_fresh_beats_stale():
    assert s("Branch Manager", posted_age_days=1) > \
           s("Branch Manager", posted_age_days=45)


def test_unknown_age_scores_between_fresh_and_stale():
    """Platforms that don't expose age must not be systematically buried."""
    fresh = s("Branch Manager", posted_age_days=1)
    unknown = s("Branch Manager", posted_age_days=-1)
    stale = s("Branch Manager", posted_age_days=60)
    assert stale < unknown < fresh


def test_easy_apply_outranks_external_ats():
    assert s("Branch Manager", easy_apply=True) > \
           s("Branch Manager", easy_apply=False)


def test_off_profile_title_scores_zero_for_title():
    assert s("Account Executive - MEA") < s("Branch Manager")


def test_dealbreaker_hard_fails():
    assert s("Vendor Manager - IT Staffing") == -50


def test_salary_below_floor_hard_fails():
    assert s("Branch Manager", salary="3-5 Lacs P.A.") == -50


def test_disclosed_pay_above_floor_helps():
    assert s("Branch Manager", salary="12-18 Lacs P.A.") > \
           s("Branch Manager", salary="")


def test_crowded_listing_ranks_below_quiet_one():
    assert s("Branch Manager", applicants=8) > s("Branch Manager", applicants=400)


def test_score_stays_in_range():
    best = s("Area Sales Manager", salary="20-30 Lacs P.A.",
             posted_age_days=0, applicants=3,
             description="consumer electronics retail b2b")
    assert 0 <= best <= 100


def test_ranking_actually_spreads():
    """The bug this scorer replaced: everything landed on one of four values."""
    titles = ["Area Sales Manager", "Branch Manager", "Sales Manager",
              "Business Development Manager", "Account Executive"]
    scores = {s(t) for t in titles} | {
        s("Branch Manager", posted_age_days=d) for d in (0, 5, 20, 60)} | {
        s("Branch Manager", easy_apply=False)}
    assert len(scores) >= 6


def test_is_match_uses_min_score():
    assert is_match(job("Area Sales Manager"), SPEC)[0]
    assert not is_match(job("Account Executive - MEA"), SPEC)[0]


def test_salary_parser_handles_indian_formats():
    assert parse_salary_lpa("4-7.5 Lacs P.A.") == (4.0, 7.5)
    assert parse_salary_lpa("12 LPA") == (12.0, 12.0)
    assert parse_salary_lpa("undisclosed") is None


# --- per-platform auto-apply thresholds ---------------------------------------

def _eligible(platform, score, default, by_platform):
    """Mirror of cmd_auto_approve's gate: platform override, else default."""
    return score >= by_platform.get(platform.lower(), default)


def test_platform_override_lowers_only_that_platform():
    bp = {"naukri": 65, "linkedin": 70}
    assert _eligible("naukri", 67, 70, bp)
    assert not _eligible("linkedin", 67, 70, bp)


def test_platform_without_override_uses_default():
    bp = {"naukri": 65}
    assert not _eligible("indeed", 67, 70, bp)
    assert _eligible("indeed", 71, 70, bp)


def test_override_is_case_insensitive():
    assert _eligible("NAUKRI", 66, 70, {"naukri": 65})


def test_no_overrides_behaves_like_a_single_bar():
    for p in ("linkedin", "naukri", "indeed"):
        assert _eligible(p, 70, 70, {})
        assert not _eligible(p, 69, 70, {})


# --- posted-age parsing (real wordings taken from saved captures) -------------

def test_parse_posted_age_real_naukri_wordings():
    from adapters.base import parse_posted_age as f
    # Every distinct .job-post-day value seen across the saved Naukri captures.
    assert f("Today") == 0
    assert f("Few hours ago") == 0          # 6 occurrences; used to return -1
    assert f("1 day ago") == 1
    assert f("6 days ago") == 6
    assert f("1 week ago") == 7
    assert f("2 weeks ago") == 14
    assert f("3+ weeks ago") == 21          # the "+" form must not break it


def test_parse_posted_age_absent_is_unknown():
    from adapters.base import parse_posted_age as f
    assert f("") == -1
    assert f("Promoted | Easy Apply") == -1  # a typical LinkedIn card


def test_parse_posted_age_takes_the_smallest():
    from adapters.base import parse_posted_age as f
    assert f("Reposted 2 days ago, originally 3 weeks ago") == 2


def test_indeed_ages_pair_to_job_keys():
    from adapters.indeed import _meta_by_jobkey
    html = ('{"formattedRelativeTime":"2 days ago","jobkey":"aaa1"}'
            '{"formattedRelativeTime":"1 week ago","jobkey":"bbb2"}')
    got = _meta_by_jobkey(html)
    assert got["aaa1"]["age"] == 2 and got["bbb2"]["age"] == 7


def test_indeed_ages_empty_when_no_json():
    from adapters.indeed import _meta_by_jobkey
    assert _meta_by_jobkey("<html>no jobcards json here</html>") == {}


def test_indeed_meta_reads_age_and_easy_apply():
    from adapters.indeed import _meta_by_jobkey
    # Field order mirrors the real payload: alphabetical, so jobkey comes last.
    html = ('{"formattedRelativeTime":"2 days ago","indeedApplyEnabled":true,'
            '"jobkey":"aaa1"}'
            '{"formattedRelativeTime":"1 week ago","indeedApplyEnabled":false,'
            '"jobkey":"bbb2"}')
    meta = _meta_by_jobkey(html)
    assert meta["aaa1"] == {"age": 2, "easy": True}
    assert meta["bbb2"] == {"age": 7, "easy": False}


def test_indeed_meta_missing_fields_are_absent_not_false():
    """A job with no applyEnabled must not be recorded as external."""
    from adapters.indeed import _meta_by_jobkey
    meta = _meta_by_jobkey('{"formattedRelativeTime":"1 day ago","jobkey":"aaa1"}')
    assert meta["aaa1"] == {"age": 1}
    assert "easy" not in meta["aaa1"]


def test_indeed_apply_selector_has_current_hook():
    """Regression guard: the live control is the testid anchor, not the
    legacy #indeedApplyButton, which matches nothing on Indeed's React page."""
    from adapters.indeed import APPLY_BTN
    assert "viewjob-indeed-apply" in APPLY_BTN
    assert "Apply now" in APPLY_BTN


def test_indeed_consent_rejects_not_accepts():
    from adapters.indeed import CONSENT_REJECT
    assert "reject" in CONSENT_REJECT.lower() or "refuse" in CONSENT_REJECT.lower()
    assert "accept" not in CONSENT_REJECT.lower()


def _spec_titles(*titles):
    return {"target_titles": list(titles)}


def test_reordered_title_matches_the_same_role():
    """"Manager - Business Development" is a business development manager."""
    from core.matching import _title_points
    bdm = _spec_titles("business development manager", "key account manager")
    for title in ("manager - business development",
                  "senior manager - business development",
                  "senior manager, key accounts sales",
                  "general manager business development"):
        assert _title_points(title, bdm)[0] > 0, title


def test_plural_word_in_title_matches_the_target():
    """"Key Accounts Manager" is a key account manager: plain substring
    matching missed it 14 times. Plural folding must not admit a different
    role or a match inside a longer word."""
    from core.matching import _title_points
    spec = _spec_titles("key account manager", "sales manager")
    for title in ("key accounts manager", "key accounts manager - fmcg",
                  "senior key accounts manager", "sales managers"):
        assert _title_points(title, spec)[0] > 0, title
    for title in ("key accounts executive", "accounts manager",
                  "key accountant manager"):
        assert _title_points(title, spec)[0] == 0, title
    # Same specificity bonuses as the singular spelling.
    assert (_title_points("key accounts manager", spec)[0]
            == _title_points("key account manager", spec)[0])


def test_reordering_does_not_admit_a_different_job():
    """The window must open on "manager" at the start or after a seniority
    word. Gaps and other leading nouns are different roles."""
    from core.matching import _title_points
    spec = _spec_titles("sales manager", "branch manager", "business manager")
    for title in ("acquisition manager-sales-sales",
                  "portfolio manager - branch",
                  "sr product manager - sales products",
                  "sales operations manager",
                  "premier acquisition manager-business banking"):
        assert _title_points(title, spec)[0] == 0, title


def test_naukri_chatbot_selector_targets_the_drawer_not_its_wrapper():
    """Regression guard: the wrapper `_chatBotContainer` has no layout size, so
    Playwright reports it not visible even with the drawer open. A selector
    that can resolve to it made every chatbot job look like "no confirmation"."""
    from adapters.naukri import CHATBOT_DRAWER
    assert "chatbot_drawer" in CHATBOT_DRAWER.lower()
    assert "container" not in CHATBOT_DRAWER.lower()


def test_posted_age_handles_article_wordings():
    """Foundit says "Posted a day ago" / "Posted today". An exact-match test and
    a digits-only regex both returned unknown for them, which scores the
    freshest listings on the board as age-unknown."""
    from adapters.base import parse_posted_age as age
    assert age("Posted a day ago") == 1
    assert age("Posted a week ago") == 7
    assert age("Posted a month ago") == 30
    assert age("an hour ago") == 0
    assert age("Posted today") == 0
    assert age("Posted yesterday") == 1
    assert age("Posted 2 days ago") == 2
    assert age("Be an early applicant") == -1      # "an" is not a count
    assert age("banana day ago") == -1             # nor is a word ending in "a"
