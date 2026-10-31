"""Time-of-day shifts: "ca sáng" = 06:00-13:59, "ca chiều" = 14:00-21:59 by order creation time (rulebook §2)."""
from __future__ import annotations

import re
from datetime import time

import pandas as pd

from .base import Finding, Query, Resolver

SHIFTS: dict[str, tuple[str, time, time]] = {
    "sang": ("ca sáng", time(6, 0), time(13, 59)),
    "chieu": ("ca chiều", time(14, 0), time(21, 59)),
}
_PAT = re.compile(r"\bca (sang|chieu)\b")


def time_columns(tables: dict[str, pd.DataFrame]) -> list[str]:
    """`table.column` of the time-of-day columns that ingest splits out of timestamps (`<col>_time`)."""
    return [f"{t}.{c}" for t, df in tables.items() for c in df.columns if c.endswith("_time")]


class ShiftResolver(Resolver):
    name = "shift"
    section = "SHIFTS"

    def __init__(self, columns: list[str] | None = None):
        self.columns = columns or []

    def resolve(self, q: Query) -> list[Finding]:
        ms = list(_PAT.finditer(q.norm.text))
        both = {m.group(1) for m in ms} == set(SHIFTS)
        out = []
        for m in ms:
            label, lo, hi = SHIFTS[m.group(1)]
            line = f'"{label}" = orders created {lo:%H:%M}-{hi:%H:%M} inclusive (by order-creation time of day)'
            if self.columns:
                line += f" [time columns: {', '.join(self.columns[:4])}]"
            if both and m.group(1) == "sang":
                line += "; when the two shifts are compared, ca sáng is the baseline"
            start, end = q.norm.to_raw(m.start(), m.end())
            out.append(self.finding(q, "shift", start, end, line, (lo, hi)))
        return out
