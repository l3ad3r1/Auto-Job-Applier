"""Find 1st-degree connections at companies we applied to, and ask for a referral.

Scope is deliberately narrow: **1st-degree connections only**. Those are people
who already accepted a connection, so a referral ask is ordinary contact. Cold
messaging strangers at a target company is not built here — it is what gets
LinkedIn accounts restricted, and it is not what a referral is.

Three findings from probing the live site shaped this, all of them ways the
naive version would message the wrong person:

* Keyword people-search is not a company filter. Searching "Bajaj Finance"
  returns people *named* Bajaj and alumni of "GL Bajaj". Only LinkedIn's
  `currentCompany` facet actually filters by employer, and it needs a numeric
  company id.
* Resolving that id is error-prone: "Bajaj Finance" resolved to Bajaj *Housing*
  Finance, a different employer. So the resolved name is scored against the one
  we asked for, and a poor match is skipped rather than guessed at.
* A company we cannot resolve is skipped entirely. There is no fuzzy fallback,
  because the failure mode of a fuzzy fallback is a referral request sent to a
  stranger who merely shares a surname with the company.

Nothing here sends anything on its own. `send_referrals` is only reached when
`networking.enabled` is true in config.yaml, and it stops at the daily cap.
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

CARD = "div[role='listitem']"
PROFILE_LINK = "a[href*='/in/']"
COMPANY_ID_RE = re.compile(r'currentCompany=%5B%22(\d+)%22%5D')

SCHEMA = """
CREATE TABLE IF NOT EXISTS referrals (
    id            INTEGER PRIMARY KEY,
    profile_url   TEXT NOT NULL UNIQUE,   -- one row per person, ever
    name          TEXT NOT NULL,
    headline      TEXT DEFAULT '',
    company       TEXT NOT NULL,          -- the company we applied to
    company_id    TEXT DEFAULT '',
    job_title     TEXT DEFAULT '',
    job_url       TEXT DEFAULT '',
    state         TEXT NOT NULL,          -- found | sent | failed | skipped
    message       TEXT DEFAULT '',
    found_at      TEXT NOT NULL,
    sent_at       TEXT DEFAULT '',
    notes         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ref_state ON referrals(state);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def _norm(text: str) -> set[str]:
    """Company-name tokens, minus the noise that makes everything look alike."""
    stop = {"ltd", "limited", "pvt", "private", "inc", "llp", "co", "corp",
            "company", "india", "the", "and", "services", "solutions", "group"}
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in stop and len(w) > 1}


def name_match(asked: str, resolved: str) -> float:
    """0-1 similarity between the company we wanted and the one LinkedIn gave.

    Jaccard, not coverage. Coverage (|a&b| / |a|) scores "Bajaj Finance" against
    "Bajaj Housing Finance" as a perfect 1.0, because every token we asked for
    appears — which is the exact mismatch this is supposed to catch. Penalising
    the extra token drops it to 0.67 while an "... Limited" suffix, being a
    stopword, still scores 1.0.
    """
    a, b = _norm(asked), _norm(resolved)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def target_companies(db_path: Path, limit: int = 25) -> list[dict]:
    """Companies we applied to, most-applied first, with a representative job."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT j.company, COUNT(*) AS applications,
                      MAX(j.title) AS job_title, MAX(j.url) AS job_url
                 FROM applications a JOIN jobs j ON j.id = a.job_id
                WHERE a.state = 'applied' AND TRIM(COALESCE(j.company,'')) <> ''
             GROUP BY LOWER(j.company)
             ORDER BY applications DESC, j.company
                LIMIT ?""", (limit,)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def resolve_company(page, company: str, min_match: float = 0.8) -> tuple[str, str] | None:
    """(company_id, resolved_name) from LinkedIn's own "see employees" link.

    Returns None when nothing resolves well enough — the caller must then skip
    the company rather than fall back to a keyword search.
    """
    page.goto("https://www.linkedin.com/search/results/companies/?keywords="
              + company.replace(" ", "%20"), wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    best = None
    for card in page.locator(CARD).all():
        try:
            html = card.inner_html()
            text = (card.inner_text() or "").strip()
        except Exception:
            continue
        ids = COMPANY_ID_RE.findall(html)
        if not ids:
            continue
        resolved = text.split("\n")[0].strip()
        score = name_match(company, resolved)
        if best is None or score > best[0]:
            best = (score, ids[0], resolved)
    if best is None or best[0] < min_match:
        return None
    return best[1], best[2]


def find_connections(page, company_id: str, limit: int = 10) -> list[dict]:
    """1st-degree connections currently at that company."""
    page.goto("https://www.linkedin.com/search/results/people/"
              f"?currentCompany=%5B%22{company_id}%22%5D&network=%5B%22F%22%5D",
              wait_until="domcontentloaded")
    page.wait_for_timeout(6500)
    people = []
    for card in page.locator(CARD).all():
        link = card.locator(PROFILE_LINK).first
        if not link.count():
            continue
        try:
            lines = [x.strip() for x in (card.inner_text() or "").split("\n") if x.strip()]
            url = (link.get_attribute("href") or "").split("?")[0]
        except Exception:
            continue
        if not url or not lines:
            continue
        blob = " ".join(lines)
        if "1st" not in blob:          # the facet should guarantee this; verify anyway
            continue
        name = re.sub(r"\s*[•·]?\s*1st.*$", "", lines[0]).strip()
        headline = next((l for l in lines[1:]
                         if "1st" not in l and l.lower() != "message"), "")
        people.append({"name": name, "headline": headline, "profile_url": url})
        if len(people) >= limit:
            break
    return people


def draft_message(contact: dict, company: str, job_title: str, profile: dict) -> str:
    """A short, specific ask. Generic asks are what burn a weak tie."""
    identity = profile.get("identity", {}) or {}
    me = (identity.get("full_name") or identity.get("name") or "").strip()
    first = (contact["name"].split() or [""])[0]
    summary = (profile.get("summary") or "").strip()
    role_line = summary.split(".")[0][:110] if summary else "a sales leadership background"

    lines = [
        f"Hi {first},",
        "",
        f"I've applied for the {job_title} role at {company} and saw you're "
        f"there — I thought I'd ask you directly rather than let the "
        f"application sit in a queue.",
        "",
        f"Quick context: {role_line}.",
        "",
        "If you think it's a reasonable fit, would you be open to referring me "
        "internally? Happy to send my CV and a two-line summary you can paste. "
        "And no problem at all if you'd rather not.",
        "",
        "Thanks!" + (f"\n{me}" if me else ""),
    ]
    return "\n".join(lines)


def record_found(con: sqlite3.Connection, contact: dict, company: str,
                 company_id: str, job: dict, message: str) -> bool:
    """Insert a contact once. Returns False if we have seen this person before.

    UNIQUE(profile_url) is what guarantees nobody is ever messaged twice, even
    if they turn up again under a different company.
    """
    cur = con.execute(
        """INSERT OR IGNORE INTO referrals
             (profile_url, name, headline, company, company_id, job_title,
              job_url, state, message, found_at)
           VALUES (?,?,?,?,?,?,?,'found',?,?)""",
        (contact["profile_url"], contact["name"], contact.get("headline", ""),
         company, company_id, job.get("job_title", ""), job.get("job_url", ""),
         message, _now()))
    con.commit()
    return cur.rowcount > 0


def sent_today(con: sqlite3.Connection) -> int:
    row = con.execute(
        "SELECT COUNT(*) c FROM referrals WHERE state='sent'"
        " AND sent_at >= date('now')").fetchone()
    return row["c"]


def pending(con: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM referrals WHERE state='found' ORDER BY found_at LIMIT ?",
        (limit,)).fetchall()


def mark(con: sqlite3.Connection, row_id: int, state: str, notes: str = "") -> None:
    con.execute(
        "UPDATE referrals SET state=?, notes=?, sent_at=? WHERE id=?",
        (state, notes, _now() if state == "sent" else "", row_id))
    con.commit()


def discover(page, db_path: Path, profile: dict, companies: int = 10,
             pause: float = 4.0) -> dict:
    """Find and draft. Writes nothing to LinkedIn."""
    con = connect(db_path)
    stats = {"companies": 0, "skipped_unresolved": 0, "found": 0, "already_known": 0}
    try:
        for job in target_companies(db_path, limit=companies):
            company = job["company"]
            stats["companies"] += 1
            resolved = resolve_company(page, company)
            time.sleep(pause)
            if resolved is None:
                stats["skipped_unresolved"] += 1
                continue
            company_id, _resolved_name = resolved
            for contact in find_connections(page, company_id):
                message = draft_message(contact, company, job["job_title"], profile)
                if record_found(con, contact, company, company_id, job, message):
                    stats["found"] += 1
                else:
                    stats["already_known"] += 1
            time.sleep(pause)
    finally:
        con.close()
    return stats
