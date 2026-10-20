"""Is a job page telling us the posting is closed?

Expired listings are the biggest cause of failed applies (see core.matching).
They used to be recorded as "Apply button not found" or even "external
application", got debug captures, and some were retried. The phrases below are
the banners found in 129 of the saved failure pages in data/debug (2026-10):

    linkedin  "No longer accepting applications"                 86 pages
    foundit   "This job is no longer accepting applications"     19
    naukri    "Job you are looking for is expired"               14
    indeed    "This job has expired on Indeed"                   10

Approach adapted from career-ops liveness-core.mjs (MIT): match a closure
statement in the page text, after normalizing typographic punctuation and
whitespace (Naukri splits its banner across elements).
"""
from __future__ import annotations

import re
import unicodedata

_CLOSED = [
    re.compile(r"no longer accepting applications", re.I),
    re.compile(r"job you are looking for is (?:expired|no longer available)", re.I),
    re.compile(r"this job has expired", re.I),
    re.compile(r"job posting has expired", re.I),
    re.compile(r"this (?:job|position|role) is no longer (?:available|open)", re.I),
]


def _normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


def closed_reason(page_text: str) -> str:
    """The closure statement found in the page text, or "" if none."""
    text = _normalize(page_text or "")
    for pattern in _CLOSED:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return ""
