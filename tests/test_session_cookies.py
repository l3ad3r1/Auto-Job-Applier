"""Session-only cookies survive a browser restart when persist_session_cookies
is on, and a run that never verified a login cannot overwrite a good saved jar.

Runs without pytest (call each test_*). Uses a throwaway profile under
profiles/ and removes it afterwards; headless, no network.
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.base import BaseAdapter, ROOT  # noqa: E402


class _Probe(BaseAdapter):
    platform = "zz-cookietest"
    persist_session_cookies = True

    def is_logged_in(self, page):
        return True

    def search(self, page, config):
        return []

    def collect_questions(self, page, item):
        return []

    def apply(self, page, item, limiter):
        return None


SESSION_COOKIE = {"name": "sessionid", "value": "abc123", "domain": "example.com",
                  "path": "/", "httpOnly": True, "secure": False}   # no expiry


def _fresh():
    shutil.rmtree(ROOT / "profiles" / _Probe.platform, ignore_errors=True)


def _cookie_names(adapter):
    return {c["name"] for c in adapter._ctx.cookies()}


def test_session_cookie_survives_restart_after_a_verified_login():
    _fresh()
    try:
        a = _Probe()
        ctx, _ = a.open_browser(headless=True)
        ctx.add_cookies([SESSION_COOKIE])
        a._session_ok = True
        a.close_browser()

        b = _Probe()
        b.open_browser(headless=True)
        try:
            assert "sessionid" in _cookie_names(b)
        finally:
            b.close_browser()
    finally:
        _fresh()


def test_session_cookie_is_lost_without_the_feature():
    """Control: this is the bug. Same steps, persistence off, cookie gone."""
    _fresh()
    try:
        class Off(_Probe):
            persist_session_cookies = False
        a = Off()
        ctx, _ = a.open_browser(headless=True)
        ctx.add_cookies([SESSION_COOKIE])
        a.close_browser()
        b = Off()
        b.open_browser(headless=True)
        try:
            assert "sessionid" not in _cookie_names(b)
        finally:
            b.close_browser()
    finally:
        _fresh()


def test_unverified_run_does_not_overwrite_the_saved_jar():
    _fresh()
    try:
        a = _Probe()
        ctx, _ = a.open_browser(headless=True)
        ctx.add_cookies([SESSION_COOKIE])
        a._session_ok = True
        a.close_browser()
        saved = (ROOT / "profiles" / _Probe.platform / "session-cookies.json").read_text(encoding="utf-8")

        b = _Probe()                       # logged out: never sets _session_ok
        ctx, _ = b.open_browser(headless=True)
        ctx.clear_cookies()
        b.close_browser()

        after = (ROOT / "profiles" / _Probe.platform / "session-cookies.json").read_text(encoding="utf-8")
        assert after == saved and "sessionid" in json.loads(after)[0]["name"]
    finally:
        _fresh()


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
