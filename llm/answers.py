"""Screening-question answering: profile map first, local LLM as fallback.

The LLM is grounded in profile.yaml only and instructed to reply UNKNOWN
when the ground truth doesn't cover the question — an UNKNOWN (or any
validation failure) flags the application for human review exactly as if
no LLM existed. It must never invent facts, and salary questions are
answered only from the profile's own CTC entries.
"""
from __future__ import annotations

import re

import yaml

from core.models import Job
from core.profile import load_config, load_profile, match_answer
from .client import LLMError, chat

_SYSTEM = """\
You fill job application forms for a candidate. You are given GROUND TRUTH
about the candidate. Answer the screening question truthfully using ONLY the
ground truth.

Rules:
- Reply with the answer text only — no explanations, no punctuation around it.
- Numeric questions (years, counts): reply with a number only.
- Yes/no questions: reply exactly Yes or No.
- If OPTIONS are listed, reply with EXACTLY one option, verbatim.
- Never invent facts, numbers, salary figures, or IDs not in the ground truth.
- If the ground truth does not determine the answer, reply exactly: UNKNOWN
"""

_cache: dict[str, str | None] = {}


def _llm_cfg() -> dict:
    cfg = load_config().get("llm", {})
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "base_url": cfg.get("base_url", "http://localhost:11434/v1"),
        "model": cfg.get("model", "gemma4:latest"),
    }


def _ground_truth(profile: dict) -> str:
    keep = {k: profile.get(k) for k in ("identity", "answers", "summary")}
    return yaml.safe_dump(keep, allow_unicode=True, sort_keys=False)


def llm_answer(question: str, options: list[str] | None,
               profile: dict, job: Job | None) -> str | None:
    """Ask the local LLM; None when it can't answer or validation fails."""
    cfg = _llm_cfg()
    if not cfg["enabled"]:
        return None
    cache_key = f"{question}|{options}"
    if cache_key in _cache:
        return _cache[cache_key]

    user = f"GROUND TRUTH:\n{_ground_truth(profile)}\n"
    if job is not None:
        user += f"\nJOB: {job.title} at {job.company} ({job.location})\n"
    user += f"\nQUESTION: {question}\n"
    user += f"OPTIONS: {options}\n" if options else "OPTIONS: free text\n"
    user += "Answer:"

    try:
        raw = chat(cfg["base_url"], cfg["model"], _SYSTEM, user)
    except LLMError as e:
        print(f"    [llm] {e}")
        return None
    answer = raw.strip().strip('"').strip()
    result: str | None = answer

    if not answer or answer.upper() == "UNKNOWN" or len(answer) > 120:
        result = None
    elif options:
        # Must resolve to exactly one offered option
        exact = [o for o in options if o.strip().lower() == answer.lower()]
        loose = [o for o in options if answer.lower() in o.lower()]
        result = exact[0] if exact else (loose[0] if len(loose) == 1 else None)
    elif re.search(r"how many|years of|number of", question.lower()):
        m = re.search(r"\d+(\.\d+)?", answer)
        result = m.group(0) if m else None

    _cache[cache_key] = result
    if result is not None:
        print(f"    [llm] {question!r} -> {result!r}")
    return result


def resolve_answer(question: str, profile: dict, *,
                   options: list[str] | None = None,
                   prepared: dict | None = None,
                   job: Job | None = None) -> str | None:
    """Answer lookup order: prepared answers -> profile map -> local LLM."""
    if prepared:
        for k, v in prepared.items():
            if k.lower() == question.lower():
                return str(v)
    mapped = match_answer(profile, question)
    if mapped is not None:
        return mapped
    return llm_answer(question, options, profile, job)
