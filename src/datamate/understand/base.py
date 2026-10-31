"""Common interface for everything that reads the question: one Finding type, one Resolver type.

A resolver looks at the question and reports Findings, each tied to a character range of the question.
`understand()` runs the resolvers in priority order and drops a finding whose characters were already
claimed by a higher-priority resolver, so e.g. "5k" stays an amount (not the abbreviation "k") and
"năm 2025" inside "Tết năm 2025" is left to the Tết resolver.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from ..normalize import Normalized, Word


@dataclass
class Query:
    """The question plus everything resolvers share: normalized text with positions, and the as-of date."""
    question: str
    as_of: date
    norm: Normalized = field(init=False)
    found: list["Finding"] = field(default_factory=list)  # findings of the resolvers that already ran (higher priority)

    def __post_init__(self) -> None:
        self.norm = Normalized(self.question)

    @property
    def text(self) -> str:
        """NFC question. Every Finding offset points into this string."""
        return self.norm.raw

    def words(self) -> list[Word]:
        return self.norm.words()


@dataclass(frozen=True)
class Finding:
    resolver: str      # name of the resolver that produced it ("time", "lunar", "money", "slang", "entity")
    kind: str          # resolver-specific sub-type ("month", "abbr", "amount", ...)
    start: int         # character range in Query.text, end exclusive
    end: int
    text: str          # Query.text[start:end]
    line: str          # one line for the prompt section of that resolver
    value: Any = None  # structured result (Span, amount, Hint, expansion)

    def overlaps(self, other: "Finding") -> bool:
        return self.start < other.end and other.start < self.end


class Resolver(ABC):
    name: str
    section: str          # prompt section the findings' lines are listed under
    claims: bool = True   # False: advisory only, neither blocked by nor blocking other resolvers

    @abstractmethod
    def resolve(self, q: Query) -> list[Finding]: ...

    def finding(self, q: Query, kind: str, start: int, end: int, line: str, value: Any = None) -> Finding:
        return Finding(self.name, kind, start, end, q.text[start:end], line, value)


def understand(q: Query, resolvers: Sequence[Resolver]) -> list[Finding]:
    """Run `resolvers` in priority order (first = strongest); return the surviving findings in reading order."""
    kept: list[Finding] = []
    claimed: list[Finding] = []
    for r in resolvers:
        q.found = kept  # lets a later resolver build on earlier ones (e.g. coverage needs the resolved dates)
        mine = [f for f in r.resolve(q) if not (r.claims and any(f.overlaps(c) for c in claimed))]
        kept += mine
        if r.claims:
            claimed += mine
    return sorted(kept, key=lambda f: (f.start, f.end))
