"""Rulebook loading and retrieval.

The rulebook is shared by every business and encodes business definitions
("doanh thu" excludes cancelled orders, a week starts on Monday, ...). Short
rulebooks are passed to the model whole; long ones are chunked and the most
relevant chunks are retrieved with a small BM25.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .normalize import norm, tokens


def _chunk(text: str, target: int = 700) -> list[str]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    chunks: list[str] = []
    heading = ""
    buf = ""
    for b in blocks:
        if b.lstrip().startswith("#"):
            heading = b.strip().splitlines()[0]
        piece = b if (not heading or b.startswith(heading)) else f"{heading}\n{b}"
        if buf and len(buf) + len(piece) > target:
            chunks.append(buf)
            buf = piece
        else:
            buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        chunks.append(buf)
    return chunks


@dataclass(frozen=True)
class RuleItem:
    """One rule of the rulebook: a table row "| key | text |" or a bullet "- **key:** text"."""
    section: str   # "4" for "## 4. Công nợ"; "" before the first numbered section
    heading: str   # "Công nợ" or, under a ### sub-heading, "Nhà thuốc"
    key: str
    text: str

    def render(self) -> str:
        ref = f"§{self.section}" if self.section else ""
        return f"- {self.key} [{ref}]: {self.text}" if ref else f"- {self.key}: {self.text}"


_H2 = re.compile(r"^##\s+(?:(\d+)\.\s*)?(.+?)\s*$")
_H3 = re.compile(r"^###\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+\*\*(.+?)(?::\*\*|\*\*:)\s*(.+)$")


def parse_items(text: str) -> list[RuleItem]:
    items: list[RuleItem] = []
    section = heading = ""
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not in_table:
                in_table = True  # the first row of a table is its header ("| Luật | Mặc định |")
            elif len(cells) >= 2 and cells[0] and not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                items.append(RuleItem(section, heading, cells[0], " | ".join(cells[1:])))
            continue
        in_table = False
        if m := _H3.match(line):
            heading = m.group(1)
        elif m := _H2.match(line):
            section, heading = m.group(1) or "", m.group(2)
        elif m := _BULLET.match(line):
            items.append(RuleItem(section, heading, m.group(1).strip(), m.group(2).strip()))
    return items


class RuleBook:
    def __init__(self, text: str = ""):
        self.text = text.strip()
        self._items: list[RuleItem] | None = None
        self.chunks = _chunk(self.text) if self.text else []
        self._toks = [Counter(tokens(c)) for c in self.chunks]
        n = max(len(self.chunks), 1)
        df: Counter = Counter()
        for c in self._toks:
            df.update(c.keys())
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self._avg = sum(sum(c.values()) for c in self._toks) / n if self._toks else 1.0

    @property
    def items(self) -> list[RuleItem]:
        if self._items is None:
            self._items = parse_items(self.text)
        return self._items

    def pick(self, key_patterns: list[str]) -> list[RuleItem]:
        """Rules whose key (accent-free, lower-case) matches any of the regexes, in rulebook order."""
        pats = [re.compile(p) for p in key_patterns]
        return [it for it in self.items if any(p.search(norm(it.key)) for p in pats)]

    @classmethod
    def load(cls, path: str | Path | None) -> "RuleBook":
        if path is None:
            return cls("")
        p = Path(path)
        if p.is_dir():
            text = "\n\n".join(f.read_text(encoding="utf-8") for f in sorted(p.glob("*.md")) + sorted(p.glob("*.txt")))
        else:
            text = p.read_text(encoding="utf-8")
        return cls(text)

    def retrieve(self, question: str, k: int = 6, max_chars: int = 6000) -> str:
        if len(self.text) <= max_chars:
            return self.text
        q = set(tokens(question))
        scored = []
        for i, c in enumerate(self._toks):
            dl = sum(c.values()) or 1
            s = 0.0
            for t in q:
                f = c.get(t, 0)
                if f:
                    s += self._idf.get(t, 0.0) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl / self._avg))
            scored.append((s, i))
        top = sorted(scored, reverse=True)[:k]
        keep = sorted(i for s, i in top if s > 0) or [0]
        out = "\n\n".join(self.chunks[i] for i in keep)
        return out[:max_chars]
