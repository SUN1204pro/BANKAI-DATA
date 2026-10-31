"""Compare the asked periods with the days the shop has data for (rulebook §7: no data is not zero)."""
from __future__ import annotations

from datetime import date

from ..timeparse import Span
from .base import Finding, Query, Resolver


class CoverageResolver(Resolver):
    name = "coverage"
    section = "DATA COVERAGE"
    claims = False

    def __init__(self, date_range: tuple[str, str] | None):
        self.range = (date.fromisoformat(date_range[0]), date.fromisoformat(date_range[1])) if date_range else None

    def resolve(self, q: Query) -> list[Finding]:
        if self.range is None:
            return []
        lo, hi = self.range
        stock = any(f.resolver == "basis" and f.kind == "stock" for f in q.found)
        out = []
        for f in q.found:
            if f.resolver not in ("time", "lunar"):
                continue
            s: Span = f.value
            end = min(s.end, q.as_of)  # days after today cannot have data yet, describe() already says so
            if s.start > q.as_of:
                continue
            if end < lo:
                msg = f"lies entirely BEFORE the first day with data ({lo.isoformat()})"
                kind = "outside"
            elif s.start > hi and not stock:
                msg = f"lies entirely AFTER the last day with data ({hi.isoformat()})"
                kind = "outside"
            elif not stock and (s.start < lo or end > hi):
                msg = f"is only partly covered: data exists from {lo.isoformat()} to {hi.isoformat()}; compute on that part"
                kind = "partial"
            else:
                continue
            if kind == "outside":
                msg += " -> no data is not zero: answer unanswerable (type \"unanswerable\"), unless the question needs nothing from that period"
            out.append(self.finding(q, kind, f.start, f.end, f'"{f.text}" ({s.start.isoformat()}..{s.end.isoformat()}) {msg}', s))
        return out
