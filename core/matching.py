"""Rank jobs against the user's job_spec, 0-100. Higher is a better match.

    title fit      0-40  graded by specificity, position and seniority
    freshness      0-25  from the card's "2 weeks ago"; expiry is the top
                         cause of failed applies, unknown scores mid
    appliability   0-15  easy-apply; an external-ATS listing cannot be
                         submitted by this tool at all
    location       0-10  preferred_locations
    pay            0- 6  disclosed and clears min_salary_lpa
    domain         0- 8  industry keywords (only ~24% of rows carry any
                         description text, so this can add but never gate)
    crowding      -4/+4  applicant count, when the card exposes it
    dealbreaker     -50  reject phrase, or pay below the floor

A job needs >= job_spec.min_score to reach review; the rest are auto-skipped
with the reason recorded, so every decision is auditable in the dashboard.

Replaced a scorer that awarded a flat +4 for any target_titles substring hit
and +2 for location. With descriptions absent on 76% of rows the industry
bonus almost never fired, so 88% of the queue scored exactly 6 or 7 and the
"score" could not rank anything: a score-9 job failed to apply on the same run
that two score-6 jobs succeeded. Thresholds are therefore on a different scale
than before -- recalibrated 2026-09-03 to min_score 60 for review (~38% of
discovered jobs, matching the old bar's ~36%) and 70 for auto-apply (~15%).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Job


def parse_salary_lpa(text: str) -> tuple[float, float] | None:
    """Parse Indian salary strings to an annual (lo, hi) in LPA.

    Handles '4-7.5 Lacs P.A.', '₹ 2,75,000 - 6,00,000 a year',
    '₹40,000 - ₹60,000 a month', '12 LPA'. Returns None for undisclosed
    or unparseable text.
    """
    t = text.lower().replace(",", "")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]
    if not nums:
        return None
    if "lac" in t or "lakh" in t or "lpa" in t:
        vals = nums
    elif "month" in t:
        vals = [n * 12 / 100000 for n in nums]
    elif "year" in t or "annum" in t or "p.a" in t:
        vals = [n / 100000 if n > 1000 else n for n in nums]
    else:
        return None
    vals = [v for v in vals if 0.5 <= v <= 200]  # discard parse garbage
    if not vals:
        return None
    return min(vals), max(vals)


@dataclass
class MatchResult:
    score: int
    reasons: list[str]

    @property
    def summary(self) -> str:
        return f"score {self.score}: " + "; ".join(self.reasons)


SENIOR_MARKERS = ("head", "regional", "zonal", "state", "senior", "sr.", "cluster",
                  "area", "branch", "national", "general manager", "gm ")
JUNIOR_MARKERS = ("executive", "officer", "associate", "assistant", "representative",
                  "trainee", "intern", "junior", "jr.", "counsellor", "counselor",
                  "telecaller", "tele caller", "field sales")


def _title_tokens(text: str) -> list[str]:
    """Lowercase words, with a trailing plural 's' folded off ("accounts")."""
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w
            for w in re.findall(r"[a-z0-9]+", text.lower())]


# Words that may sit in front of a leading "Manager" without changing the role:
# "Senior Manager - Business Development". Anything else ("Acquisition Manager",
# "Portfolio Manager") makes "Manager" the tail of a different job title.
_MANAGER_PREFIX = {"senior", "sr", "general", "regional", "zonal", "state",
                   "national", "area", "cluster", "deputy", "lead"}


def _reordered_hit(target: str, title_tokens: list[str]) -> bool:
    """"Manager - Business Development" for the target "business development
    manager": same words, adjacent, but with the role word moved to the front.

    Plain substring matching rejected these ("Senior Manager, Key Accounts
    Sales" for "key account manager"). The shape is narrow on purpose. The
    window must open on the target's last word, and sit at the start of the
    title or just after a seniority word. Without that, "Acquisition
    Manager-Sales" matched "sales manager" and "Portfolio Manager - Branch"
    matched "branch manager". Gaps are not allowed either: that would admit
    "Sales Operations Manager" for "sales manager", which is a different job.
    """
    want = _title_tokens(target)
    n = len(want)
    if n < 2:
        return False
    for i in range(len(title_tokens) - n + 1):
        if title_tokens[i] != want[-1]:
            continue
        if i and title_tokens[i - 1] not in _MANAGER_PREFIX:
            continue
        if sorted(title_tokens[i:i + n]) == sorted(want):
            return True
    return False


def _title_points(title: str, spec: dict) -> tuple[int, list[str]]:
    """0-40. Graded by how specifically the title matches, not merely whether.

    The old scorer gave a flat +4 to any target_titles substring hit, so
    "Sales Manager" and "Sales Manager (Police Canteens)" were indistinguishable
    and 88% of the queue landed on the same score. Here a longer, more specific
    matched phrase counts for more, matching at the start of the title counts
    for more, and seniority is scored against a 7-year Branch Manager profile.
    """
    targets = [t.lower() for t in spec.get("target_titles", [])]
    tokens = _title_tokens(title)
    hits = [t for t in targets if t in title or _reordered_hit(t, tokens)]
    if not hits:
        return 0, ["title not in target_titles"]

    best = max(hits, key=len)
    # Specificity: "area sales manager" (3 words) beats bare "sales manager".
    words = len(best.split())
    points = {1: 14, 2: 20}.get(words, 26)
    reasons = [f"title~{best!r}"]

    # A match at the front is the role; deep in the string it is often a
    # qualifier ("Intern supporting the Sales Manager").
    if title.startswith(best):
        points += 6
        reasons.append("title starts with the match")

    # The title is nearly all match, rather than one clause of many.
    if len(best) / max(len(title), 1) > 0.6:
        points += 4
        reasons.append("title is a tight match")

    if any(m in title for m in SENIOR_MARKERS):
        points += 4
        reasons.append("seniority fits")
    if any(m in title for m in JUNIOR_MARKERS):
        points -= 10
        reasons.append("below target seniority")

    return max(0, min(40, points)), reasons


def _freshness_points(job: Job) -> tuple[int, list[str]]:
    """0-25. Expired listings are the single largest cause of failed applies.

    A month-old posting is usually already closed: of the applications that
    failed, the most common reason by far was the apply button simply not
    being there any more. Unknown age scores mid-range, never worst, so
    platforms that do not expose it are not systematically buried.
    """
    age = job.posted_age_days
    if age < 0:
        return 12, ["age unknown"]
    if age <= 1:
        return 25, ["posted today/yesterday"]
    if age <= 3:
        return 22, [f"posted {age}d ago"]
    if age <= 7:
        return 17, [f"posted {age}d ago"]
    if age <= 14:
        return 10, [f"posted {age}d ago"]
    if age <= 30:
        return 4, [f"posted {age}d ago - may have closed"]
    return 0, [f"posted {age}d ago - likely expired"]


def score_job(job: Job, spec: dict) -> MatchResult:
    """Rank a job 0-100. Higher is a better, more applicable match.

    Components: title fit (0-40), freshness (0-25), appliability (0-15),
    location (0-10), pay (0-6), domain (0-8). Dealbreakers still hard-fail.
    """
    text = f"{job.title} {job.description}".lower()
    title = job.title.lower()
    location = job.location.lower()
    score, reasons = 0, []

    for phrase in spec.get("reject", []):
        if phrase.lower() in text:
            return MatchResult(-50, [f"dealbreaker: {phrase!r}"])

    floor = spec.get("min_salary_lpa")
    rng = parse_salary_lpa(job.salary) if floor else None
    if floor and rng and rng[1] < float(floor):
        return MatchResult(-50, [f"salary {job.salary!r} tops out below {floor} LPA"])

    pts, why = _title_points(title, spec)
    score += pts
    reasons += why

    pts, why = _freshness_points(job)
    score += pts
    reasons += why

    # Appliability: an external-ATS listing cannot be submitted by this tool at
    # all, so it should never outrank something applyable.
    if job.easy_apply:
        score += 15
        reasons.append("easy apply")
    else:
        reasons.append("not easy apply")

    for phrase in spec.get("preferred_locations", []):
        if phrase.lower() in location:
            score += 10
            reasons.append(f"location~{phrase!r}")
            break
    else:
        reasons.append("outside preferred locations")

    if rng:
        score += 6
        reasons.append(f"pay disclosed, clears floor ({job.salary})")

    # Domain keywords. Only ~24% of rows carry any description text, so this
    # can add but must never be required to clear a threshold.
    hits = [kw for kw in spec.get("industries", []) if kw.lower() in text]
    if hits:
        score += min(8, 3 * len(hits))
        reasons.append(f"industry {hits[:3]}")

    # Crowding, when the card exposed it.
    if job.applicants >= 0:
        if job.applicants <= 25:
            score += 4
            reasons.append(f"{job.applicants} applicants")
        elif job.applicants >= 200:
            score -= 4
            reasons.append(f"{job.applicants} applicants - crowded")

    return MatchResult(max(0, min(100, score)), reasons)


def is_match(job: Job, spec: dict) -> tuple[bool, MatchResult]:
    result = score_job(job, spec)
    return result.score >= int(spec.get("min_score", 4)), result
