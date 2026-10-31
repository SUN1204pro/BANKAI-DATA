"""Time expressions as resolvers. The logic stays in timeparse.py / lunar.py; these only adapt it to Finding."""
from __future__ import annotations

from ..lunar import LunarCalendar, resolve_lunar
from ..timeparse import Conventions, Span, describe, resolve
from .base import Finding, Query, Resolver


class _SpanResolver(Resolver):
    section = "RESOLVED TIME RANGES"

    def _findings(self, q: Query, spans: list[Span]) -> list[Finding]:
        out = []
        for s in spans:
            start, end = s.pos if s.pos else (0, 0)
            out.append(self.finding(q, s.kind, start, end, describe([s], q.as_of)[0], s))
        return out


class TimeResolver(_SpanResolver):
    name = "time"

    def __init__(self, conv: Conventions | None = None):
        self.conv = conv or Conventions()

    def resolve(self, q: Query) -> list[Finding]:
        return self._findings(q, resolve(q.question, q.as_of, self.conv))


class LunarResolver(_SpanResolver):
    """Tết, rằm, holidays. Put it before TimeResolver: "Tết năm 2025" then wins over the plain "năm 2025"."""
    name = "lunar"

    def __init__(self, cal: LunarCalendar):
        self.cal = cal

    def resolve(self, q: Query) -> list[Finding]:
        return self._findings(q, resolve_lunar(q.question, self.cal, q.as_of))
