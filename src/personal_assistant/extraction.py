from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class SuggestedItem:
    kind: str
    text: str
    confidence: float


def extract_suggestions(text: str) -> list[SuggestedItem]:
    suggestions: list[SuggestedItem] = []
    lowered = text.lower()

    for sentence in re.split(r"[.!?]\s+", text):
        s = sentence.strip()
        if not s:
            continue
        sl = s.lower()
        if sl.startswith("decision") or " we decided " in sl:
            suggestions.append(SuggestedItem("decision", s, 0.85))
        elif any(k in sl for k in ["follow up", "i will", "i'll", "by "]):
            suggestions.append(SuggestedItem("commitment", s, 0.75))
        elif any(k in sl for k in ["blocked", "blocker", "risk", "dependency"]):
            suggestions.append(SuggestedItem("risk", s, 0.8))
        elif any(k in sl for k in ["todo", "task", "implement", "next step"]):
            suggestions.append(SuggestedItem("task", s, 0.7))

    if not suggestions and lowered.strip():
        suggestions.append(SuggestedItem("note", text.strip(), 0.5))
    return suggestions
