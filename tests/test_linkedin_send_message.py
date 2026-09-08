"""Regression tests for the send_message recipient-safety checks.

Runs without pytest (it isn't installed in this venv -- see CODEX.md
Testing section): call each `test_*` with a fresh Playwright page, as the
`if __name__` block below does. Each loads a static fixture
(tests/fixtures/linkedin_profile_*.html) via set_content() -- no network, no
LinkedIn account -- so the exact locator expressions used in
adapters.linkedin.send_message can be pinned down offline instead of only
being provable by running them live against the real account.

Background: `button:has-text('Message')`, unscoped, matched LinkedIn's global
"Compose message" chat-dock control on a profile that had no real Message
button -- `:has-text` is a case-insensitive substring match, and "Compose
message" contains "message". That silently opened a blank draft with no
recipient. These tests assert the fix (scope to the profile's own section,
verify the button names the person) and pin the bug it replaces.
"""
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load(page, name):
    page.set_content((FIXTURES / name).read_text(encoding="utf-8"))
    return page


def test_no_message_button_profile_has_none_in_profile_section(page):
    load(page, "linkedin_profile_no_message_button.html")
    card = page.locator("main section").first
    btn = card.locator("button:has-text('Message')").first
    assert btn.count() == 0


def test_unscoped_selector_would_match_the_global_widget(page):
    """Documents the actual bug: unscoped, the selector finds one match --
    the global "Compose message" control -- on a profile with no real
    Message button. This is why scoping to `main section` is required."""
    load(page, "linkedin_profile_no_message_button.html")
    unscoped = page.locator("button:has-text('Message')")
    assert unscoped.count() == 1
    assert unscoped.first.inner_text() == "Compose message"


def test_message_button_found_and_named_on_a_real_profile(page):
    load(page, "linkedin_profile_with_message_button.html")
    card = page.locator("main section").first
    btn = card.locator("button:has-text('Message')").first
    assert btn.count() == 1
    label = (btn.get_attribute("aria-label") or "").lower()
    assert "jane" in label


def test_sidebar_message_anchors_are_not_in_the_profile_section(page):
    """The "People also viewed" sidebar carries `a:has-text('Message ...')`
    links for other people. Scoping to `main section` (the intro, i.e. the
    first section) must not accidentally include those."""
    load(page, "linkedin_profile_with_message_button.html")
    card = page.locator("main section").first
    assert card.locator("a:has-text('Message')").count() == 0


def test_recipient_header_present_for_the_right_person(page):
    load(page, "linkedin_profile_with_message_button.html")
    header = page.locator(".msg-overlay-bubble-header__title").first
    assert header.inner_text().strip().lower() == "jane doe"


def test_recipient_header_absent_when_compose_opened_blank(page):
    """The no-Message-button fixture never shows a compose header naming
    anyone -- this is the belt-and-braces check for a blank draft."""
    load(page, "linkedin_profile_no_message_button.html")
    header = page.locator(".msg-overlay-bubble-header__title").first
    assert header.count() == 0


if __name__ == "__main__":
    from playwright.sync_api import sync_playwright

    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        passed = failed = 0
        for t in tests:
            page = browser.new_page()
            try:
                t(page)
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
