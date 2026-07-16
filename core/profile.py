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
