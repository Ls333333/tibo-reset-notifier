from __future__ import annotations

import re
from dataclasses import dataclass, field

RESET_VERBS = (
    r"(?:reset(?:ting)?|refresh(?:ing|ed)?|rolled\s*over|rolling\s*over|"
    r"rollover|topped\s*up|top\s*up|replenish(?:ed|ing)?|restor(?:ed|ing)?)"
)
LIMIT_NOUNS = (
    r"(?:rate\s*-?\s*limits?|limits?|quotas?|caps?|credits?|usage|"
    r"allowances?|budgets?)"
)

STRONG = [
    ("limits-reset",
     rf"{LIMIT_NOUNS}(.{{0,60}}?){RESET_VERBS}|{RESET_VERBS}(.{{0,60}}?){LIMIT_NOUNS}"),
    ("credits-for-users",
     r"credit\w*(.{0,40}?)(?:every|all)(.{0,20}?)(?:codex\s+)?users?"
     r"|(?:every|all)(.{0,20}?)(?:codex\s+)?users?(.{0,40}?)credit\w*"),
    ("credits-granted",
     r"\bwe\b(.{0,40}?)\b(?:added|credited|granted|gifted|topped\s*up)\b"
     r"|\bconsider\s+it\s+a\s+gift\b"),
]

MEDIUM = [
    ("rate-limits", r"\brate\s*-?\s*limits?\b"),
    ("quota", r"\bquotas?\b"),
    ("usage-limits", r"\busage\s+(?:limits?|caps?)\b|\blocal\s+rate\s+limits?\b"),
    ("periodic-cap",
     r"\b(?:weekly|daily|monthly)\s+(?:limits?|caps?|quotas?|budgets?|allowances?)\b"),
    ("credits", r"\bcredits?\b"),
    ("limits-statement", r"\byour\s+limits?\b|\blimits?\s+(?:are|is|were|will)\b"),
]

WEAK = [
    ("codex", r"\bcodex\b"),
    ("chatgpt", r"\bchatgpt\b"),
    ("openai", r"\bopenai\b"),
]


@dataclass
class Score:
    total: int = 0
    matched: list[str] = field(default_factory=list)
    strong: bool = False
    domain: bool = False

    @property
    def hits(self) -> str:
        return ", ".join(self.matched) or "none"


def _compile(rules: list[tuple[str, str]], weight: int):
    return [(name, re.compile(pattern, re.I), weight) for name, pattern in rules]


_RULES = _compile(STRONG, 3) + _compile(MEDIUM, 2) + _compile(WEAK, 1)
_STRONG_NAMES = {name for name, _, _ in _compile(STRONG, 3)}
_DOMAIN_NAMES = {"codex", "chatgpt", "openai"}


def score_text(text: str) -> Score:
    result = Score()
    if not text:
        return result
    for name, pattern, weight in _RULES:
        if pattern.search(text):
            result.total += weight
            result.matched.append(name)
    result.strong = any(name in _STRONG_NAMES for name in result.matched)
    result.domain = any(name in _DOMAIN_NAMES for name in result.matched)
    return result


def should_alert(score: Score, threshold: int) -> bool:
    return score.strong or (score.domain and score.total >= threshold)
