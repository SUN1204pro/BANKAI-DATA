"""Is the question about activity inside a period ("phát sinh") or about a balance at the end of a day?

Rulebook §2: "ngày X, hôm nay, hôm qua" with revenue / orders / price is FLOW, only records inside the period.
"đến hết ngày X, tính đến hôm nay" with còn nợ / còn tồn / còn hàng is STOCK: the state at the end of that
day, summing everything from the first record of the data to that day, not just the period "tháng này".
"""
from __future__ import annotations

import re
from datetime import date

from ..timeparse import Span
from .base import Finding, Query, Resolver

# Words that ask for what is left. "đến hết ngày" alone is not enough: "từ đầu tháng đến hết hôm nay" is a flow range.
_STOCK = re.compile(
    r"\bcon (no|thieu|ton|hang|lai|bao nhieu|may)\b|\bno con\b|\bdang (no|thieu)\b|\bton kho\b|\bno nhieu nhat\b"
    r"|\bkhach no\b|\bcong no\b.*\b(den het|tinh den|cuoi ngay)\b|\b(den het|tinh den|cuoi ngay)\b.*\bcong no\b"
)
# Explicit movements during a period win over the cues above: "ghi nợ thêm", "trả nợ được", "thu hồi".
_FLOW = re.compile(r"\bghi no them\b|\btra no\b|\bthu hoi\b|\bno them\b|\bnhap (them|vao)\b")


def stock_cue(text: str) -> re.Match | None:
    """`text` must be accent-free (Normalized.text). Returns the match when the question asks for a balance."""
    if _FLOW.search(text):
        return None
    return _STOCK.search(text)


class BasisResolver(Resolver):
    name = "basis"
    section = "TIME BASIS"
    claims = False

    def __init__(self, first_day: str | None = None):
        self.first_day = first_day

    def resolve(self, q: Query) -> list[Finding]:
        spans: list[Span] = [f.value for f in q.found if f.resolver in ("time", "lunar")]
        m = stock_cue(q.norm.text)
        if m:
            day: date = max((s.end for s in spans), default=q.as_of)
            day = min(day, q.as_of)
            since = f" (the data starts {self.first_day})" if self.first_day else ""
            line = (f"STOCK - state at the END of {day.isoformat()}: add up ALL records from the first day of the data{since} "
                    f"through {day.isoformat()} inclusive; do not limit it to the period named in the question")
            start, end = q.norm.to_raw(m.start(), m.end())
            return [self.finding(q, "stock", start, end, line, day)]
        if spans:
            line = "FLOW (phát sinh) - count only records dated inside the ranges above"
            start, end = min(f.start for f in q.found if f.resolver in ("time", "lunar")), max(f.end for f in q.found if f.resolver in ("time", "lunar"))
            return [self.finding(q, "flow", start, end, line)]
        return []
