"""Load user config and profile YAML files."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(name: str) -> dict:
    path = ROOT / name
    if not path.exists():
        example = ROOT / name.replace(".yaml", ".example.yaml")
        raise FileNotFoundError(
            f"{path} not found. Copy {example.name} to {name} and edit it.")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> dict:
    return _load_yaml("config.yaml")


def load_profile() -> dict:
    return _load_yaml("profile.yaml")


def unmapped_search_keywords(config: dict, profile: dict) -> list[str]:
    """Search keywords that no target_title can ever match.

    `config.yaml` decides what we search the job boards FOR; `profile.yaml`
    `job_spec.target_titles` decides what the match gate ACCEPTS. Nothing kept
    the two in sync, so a keyword could be searched at full browser cost and
    then have every result auto-skipped at the gate.

    That is not hypothetical: 'Business Development Manager' was searched for
    while target_titles had no entry that could match it, and 132 of 242
    skipped jobs were 'title not in target_titles' with the location matching.

    Mirrors the scoring rule in matching.score_job (target phrase must be a
    substring of the job title): a keyword is reachable when some target title
    is a substring of it, since a listing for that keyword carries it verbatim.
    """
    titles = [t.lower() for t in (profile.get("job_spec", {}) or {}).get("target_titles", [])]
    keywords = (config.get("search", {}) or {}).get("keywords", []) or []
    return [kw for kw in keywords
            if not any(t in kw.lower() for t in titles)]


def match_answer(profile: dict, question: str) -> str | None:
    """Match a screening question against the profile's answer map.

    Lowercased substring match: the profile key must appear in the question.
    Returns None when nothing matches — caller flags it for human review.
    """
    q = question.lower()
    for key, answer in (profile.get("answers") or {}).items():
        if key.lower() in q:
            return str(answer)
    return None
