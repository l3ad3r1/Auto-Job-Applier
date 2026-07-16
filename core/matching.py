"""Score jobs against the user's job_spec so only real matches reach review.

Scoring (title + description text, lowercased):
  +4  title contains a target_titles phrase
  +2  per industry keyword found (capped at +6)
  +2  location contains a preferred_locations phrase
  -50 any reject phrase found (hard dealbreaker)

A job needs >= min_score to be queued for review; everything else is
auto-skipped with the reason recorded so it's auditable in the dashboard.
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


def score_job(job: Job, spec: dict) -> MatchResult:
    text = f"{job.title} {job.description}".lower()
    title = job.title.lower()
    location = job.location.lower()
    score, reasons = 0, []

    for phrase in spec.get("reject", []):
        if phrase.lower() in text:
            return MatchResult(-50, [f"dealbreaker: {phrase!r}"])

    # Salary floor: reject when the posted range tops out below the floor.
    # Undisclosed salary passes through — the dashboard marks it for review.
    floor = spec.get("min_salary_lpa")
    if floor:
        rng = parse_salary_lpa(job.salary)
        if rng and rng[1] < float(floor):
            return MatchResult(
                -50, [f"salary {job.salary!r} tops out below {floor} LPA"])
        if rng:
            score += 1
            reasons.append(f"salary ok ({job.salary})")

    for phrase in spec.get("target_titles", []):
        if phrase.lower() in title:
            score += 4
            reasons.append(f"title~{phrase!r}")
            break
    else:
        reasons.append("title not in target_titles")

    hits = [kw for kw in spec.get("industries", []) if kw.lower() in text]
    if hits:
        bonus = min(6, 2 * len(hits))
        score += bonus
        reasons.append(f"industry {hits[:3]}")

    for phrase in spec.get("preferred_locations", []):
        if phrase.lower() in location:
            score += 2
            reasons.append(f"location~{phrase!r}")
            break

    return MatchResult(score, reasons)


def is_match(job: Job, spec: dict) -> tuple[bool, MatchResult]:
    result = score_job(job, spec)
    return result.score >= int(spec.get("min_score", 4)), result
