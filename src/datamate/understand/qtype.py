"""What kind of question is this, and which rulebook rows does that kind need?

The rulebook is in the system prompt for every question, but the rows that matter for one question are easy
to miss in 18 000 characters, and the same word means different things by type: an order counts for
revenue (not cancelled, not returned) but not for debt (returns still count), and the cancel rate counts
every status. So the matching rows are repeated at the very end of the prompt, right after the question.

The cues and row patterns live in topics.json. A pattern that matches no row of the shop's rulebook is
simply skipped, so a rulebook with other wording degrades to "fewer reminders", never to an error.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..normalize import norm
from ..rules import RuleBook, RuleItem
from .base import Finding, Query, Resolver
from .slang import expand_slang

TOPICS_FILE = Path(__file__).with_name("topics.json")


@lru_cache(maxsize=None)
def load_topics(path: Path = TOPICS_FILE) -> dict[str, dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    def expand(pattern: str) -> re.Pattern:
        for macro, value in data.get("macros", {}).items():
            pattern = pattern.replace(macro, value)
        return re.compile(pattern)

    return {name: {**t, "cue_re": [expand(c) for c in t["cues"]]} for name, t in data["topics"].items()}


class QuestionTypeResolver(Resolver):
    name = "qtype"
    section = "QUESTION TYPE"
    claims = False

    def __init__(self, topics: dict[str, dict] | None = None):
        self.topics = topics or load_topics()

    def resolve(self, q: Query) -> list[Finding]:
        # cues are written for full words: read "dc", "bnhiu", "ko" as "duoc", "bao nhieu", "khong"
        text, origin = expand_slang(q.norm.text)
        out = []
        for name, t in self.topics.items():
            hit = next((m for pat in t["cue_re"] if (m := pat.search(text))), None)
            if hit:
                start, end = q.norm.to_raw(origin[hit.start()], origin[hit.end() - 1] + 1)
                out.append(self.finding(q, name, start, end, f'{t["label"]} (cue: "{q.text[start:end]}")', name))
        return out


def rules_for(topics: list[str], rulebook: RuleBook | None, max_rows: int = 14, max_chars: int = 4500) -> str:
    """The rulebook rows for the detected topics, grouped by topic, each row once, in topic order."""
    if rulebook is None or not rulebook.items:
        return ""
    cfg = load_topics()
    seen: set[RuleItem] = set()
    blocks: list[str] = []
    rows = 0
    for name, t in cfg.items():  # topics.json order: measures first, so "which orders count" leads
        if name not in topics:
            continue
        # the topic's own row order, not the rulebook's: the first rows say which orders count
        picked = []
        for pattern in t["rows"]:
            picked += [it for it in rulebook.pick([pattern]) if it not in seen and it not in picked]
        picked = picked[: max(0, max_rows - rows)]
        if not picked:
            continue
        seen.update(picked)
        rows += len(picked)
        blocks.append(f'### {t["label"]}\n' + "\n".join(it.render() for it in picked))
    return "\n\n".join(blocks)[:max_chars]

