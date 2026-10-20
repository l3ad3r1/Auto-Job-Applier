"""Integrity gate for scraped posting text, applied before it is stored.

A posting is third-party text that ends up next to the candidate's data: it is
matched against `job_spec.reject` phrases, shown in the dashboard, and the
title goes into LLM prompts (title rescue). Two things can go wrong with it:

  * invisible characters (zero-width, bidi controls, soft hyphen, BOM) split
    words so that a reject phrase like "it staffing" silently stops matching,
    or reorder text on screen. They are removed and counted.
  * instruction-shaped lines aimed at an AI rather than a reader ("ignore
    previous instructions", "if you are an AI..."), and words mixing Latin
    with Cyrillic/Greek look-alike letters. These are flagged, never removed:
    a real posting can legitimately say "assistant" or "AI".

Adapted from MadsLorentzen/ai-job-search tools/posting_guard.py (MIT). Hidden
HTML is out of scope here: adapters store rendered inner text, not markup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Zero-width space/non-joiner/joiner, word joiner, BOM, soft hyphen,
# bidi embeddings/overrides (202A-202E) and isolates (2066-2069), LRM/RLM.
_INVISIBLE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad"
                        "\u202a-\u202e\u2066-\u2069\u200e\u200f]")
# Bidi overrides/isolates reorder what is displayed; a legitimate posting has
# no use for them, so their presence alone is tampering.
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
# An invisible character with a letter on both sides splits a word.
_IN_WORD = re.compile("\\w[\u200b\u200c\u200d\u2060\ufeff\u00ad]+\\w")

_INSTRUCTION = re.compile(
    r"\b(?:ignore|disregard|forget)\b.{0,30}\b(?:previous|prior|above|all|any)\b"
    r".{0,20}\b(?:instructions?|prompts?|rules)\b"
    r"|\bif you are an? (?:ai|llm|bot|language model|assistant|gpt)\b"
    r"|\b(?:system|hidden) prompt\b"
    r"|\bas an ai (?:model|assistant|language model)\b"
    r"|\byou are (?:now )?(?:an? )?(?:ai|chatgpt|claude|gpt|llm|language model)\b"
    r"|\b(?:ai|llm|bot) (?:reading|screening|processing) this\b",
    re.I)

_LATIN = re.compile(r"[A-Za-z]")
_LOOKALIKE = re.compile("[\u0370-\u03ff\u0400-\u04ff]")  # Greek, Cyrillic

# Prefix on the stored flag; auto-approve holds any row carrying it.
FLAG = "posting guard"


@dataclass
class GuardReport:
    invisible_removed: int = 0
    tampering: list[str] = field(default_factory=list)
    instruction_lines: list[str] = field(default_factory=list)
    mixed_script: list[str] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        """Signals of targeting, not just noise from a copy-paste."""
        return bool(self.tampering or self.instruction_lines or self.mixed_script)

    @property
    def summary(self) -> str:
        parts = list(self.tampering)
        if self.instruction_lines:
            parts.append("instruction-shaped text: "
                         + " | ".join(repr(s[:80]) for s in self.instruction_lines[:2]))
        if self.mixed_script:
            parts.append(f"look-alike letters in {self.mixed_script[:3]}")
        return f"{FLAG}: " + "; ".join(parts) if parts else ""


def scan(text: str) -> tuple[str, GuardReport]:
    """Return (cleaned text, report). Never raises; empty text is clean."""
    report = GuardReport()
    if not text:
        return text or "", report
    if _BIDI.search(text):
        report.tampering.append("bidi control characters")
    if _IN_WORD.search(text):
        report.tampering.append("invisible characters inside words")
    cleaned, report.invisible_removed = _INVISIBLE.subn("", text)

    for line in cleaned.splitlines():
        line = line.strip()
        if line and _INSTRUCTION.search(line):
            report.instruction_lines.append(line)
    for word in re.findall(r"\w+", cleaned):
        if _LATIN.search(word) and _LOOKALIKE.search(word):
            report.mixed_script.append(word)
    return cleaned, report
