"""Resolve Vietnamese relative time expressions against an as-of date.

Pure code, no LLM: every date range the model sees is computed here, so
"tuần rồi" / "tháng này" / "cùng kỳ năm ngoái" never depend on model arithmetic.

Conventions follow the DataMate rulebook (section 2):
  * "hôm nay" is the question's `as_of_date`.
  * Weeks start on Monday. "tuần này" = Monday .. today. "tuần trước/rồi" = the whole previous week.
  * "tháng này" = day 1 .. today. When it is compared with "tháng trước", the previous month is cut
    to the same number of days (or to its last day if shorter).
  * "từ đầu năm" = 01/01 .. today. "N ngày gần nhất" = N days ending today.
  * "tháng M" / "quý Q" with no year = this year if it does not start after today, else last year.
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from .normalize import Normalized


@dataclass
class Conventions:
    week_start: int = 0                    # 0 = Monday ... 6 = Sunday (rulebook: Monday)
    rolling_includes_today: bool = True    # "N ngày gần nhất": last day is today (rulebook)


@dataclass
class Span:
    phrase: str
    start: date
    end: date      # inclusive
    kind: str      # day | week | month | quarter | year | range | lunar_festival
    note: str = ""
    pos: tuple[int, int] | None = None  # [start, end) in Normalized(question).raw; None if not tied to the text

    def as_dict(self) -> dict:
        return {
            "phrase": self.phrase,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "kind": self.kind,
            "note": self.note,
        }


# ---------------------------------------------------------------- helpers
def month_bounds(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def add_months(y: int, m: int, delta: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def shift_months(d: date, delta: int) -> date:
    y, m = add_months(d.year, d.month, delta)
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def week_bounds(d: date, week_start: int = 0) -> tuple[date, date]:
    start = d - timedelta(days=(d.weekday() - week_start) % 7)
    return start, start + timedelta(days=6)


def quarter_bounds(y: int, q: int) -> tuple[date, date]:
    m0 = 3 * (q - 1) + 1
    return date(y, m0, 1), month_bounds(y, m0 + 2)[1]


def _shift_year(d: date, delta: int) -> date:
    try:
        return d.replace(year=d.year + delta)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + delta, day=28)


def shift_span_year(span: Span, delta: int, phrase: str) -> Span:
    s, e = span.start, span.end
    month_aligned = s.day == 1 and e.day == calendar.monthrange(e.year, e.month)[1]
    ns = _shift_year(s, delta)
    ne = month_bounds(e.year + delta, e.month)[1] if month_aligned else _shift_year(e, delta)
    return Span(phrase, ns, ne, span.kind, f"same period {abs(delta)} year(s) earlier than {span.phrase!r}")


def date_without_year(d: int, m: int, as_of: date) -> date:
    """Rulebook rule for a month/day with no year: this year unless that is after today."""
    cand = date(as_of.year, m, d)
    return cand if cand <= as_of else date(as_of.year - 1, m, d)


_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
_YEAR_WORD = r"(nay|ngoai|truoc|roi|\d{4})"
_DATE = r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{4}))?"
_TO_NOW = r"(?: (?:toi|den|cho den) (?:nay|hom nay|bay gio|hien tai))?"

# Priority order matters: longer / more specific patterns first. Matches never overlap.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("range_dm", re.compile(rf"\btu (?:ngay )?{_DATE} (?:den|toi|-) (?:het )?(?:ngay )?{_DATE}(?!\d)")),
    ("date_dmy", re.compile(r"\b(?:ngay )?(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")),
    ("date_d_thang_m", re.compile(r"\bngay (\d{1,2}) thang (\d{1,2})(?: nam (\d{4}))?\b")),
    ("date_dm", re.compile(r"\bngay (\d{1,2})[/\-](\d{1,2})(?![/\-.]\d)")),
    ("rolling", re.compile(r"\b(\d{1,3}) (ngay|tuan|thang) (?:qua|gan day|gan nhat|vua qua|vua roi|truoc do)\b")),
    ("cung_ky", re.compile(r"\bcung ky (?:nam ngoai|nam truoc|nam roi)\b")),
    ("ytd", re.compile(rf"\btu dau nam{_TO_NOW}")),
    ("mtd", re.compile(rf"\btu dau thang{_TO_NOW}")),
    ("month_n", re.compile(rf"\bthang (\d{{1,2}})(?:(?: nam |/){_YEAR_WORD})?\b")),
    ("quarter_n", re.compile(rf"\bquy (iv|iii|ii|i|[1-4])(?:(?: nam |/){_YEAR_WORD})?\b")),
    ("year_n", re.compile(r"\bnam (\d{4})\b")),
    ("today", re.compile(r"\bhom nay\b")),
    ("yesterday", re.compile(r"\bhom qua\b")),
    ("day_before", re.compile(r"\bhom kia\b")),
    ("week_this", re.compile(r"\btuan nay\b")),
    ("week_last", re.compile(r"\btuan (?:roi|truoc|qua|vua roi|vua qua)\b")),
    ("month_this", re.compile(r"\bthang nay\b")),
    ("month_last", re.compile(r"\bthang (?:roi|truoc|qua|vua roi|vua qua)\b")),
    ("quarter_this", re.compile(r"\bquy nay\b")),
    ("quarter_last", re.compile(r"\bquy (?:roi|truoc|qua)\b")),
    ("year_this", re.compile(r"\bnam nay\b")),
    ("year_last", re.compile(r"\bnam (?:ngoai|truoc|roi|qua)\b")),
]


def _year_from_token(tok: str | None, as_of: date) -> int:
    if tok is None or tok == "nay":
        return as_of.year
    if tok.isdigit():
        return int(tok)
    return as_of.year - 1  # ngoai / truoc / roi


def _rolling(n: int, unit: str, as_of: date, conv: Conventions, phrase: str) -> Span:
    end = as_of if conv.rolling_includes_today else as_of - timedelta(days=1)
    if unit == "ngay":
        start = end - timedelta(days=n - 1)
    elif unit == "tuan":
        start = end - timedelta(days=7 * n - 1)
    else:
        start = shift_months(end, -n) + timedelta(days=1)
    return Span(phrase, start, end, "range", f"rolling {n} {unit}, last day = {end.isoformat()}")


def _build(name: str, m: re.Match, as_of: date, conv: Conventions) -> Span | None:
    phrase = m.group(0)
    g = m.groups()
    try:
        if name == "range_dm":
            d1, m1, y1, d2, m2, y2 = g
            s = date(int(y1), int(m1), int(d1)) if y1 else date_without_year(int(d1), int(m1), as_of)
            if y2:
                e = date(int(y2), int(m2), int(d2))
            elif y1:
                e = date(int(y1), int(m2), int(d2))
                if e < s:
                    e = date(int(y1) + 1, int(m2), int(d2))
            else:
                e = date_without_year(int(d2), int(m2), as_of)
            if e < s:
                return None
            return Span(phrase, s, e, "range", "both ends included")
        if name == "date_dmy":
            d = date(int(g[2]), int(g[1]), int(g[0]))
            return Span(phrase, d, d, "day")
        if name == "date_d_thang_m":
            d = date(int(g[2]), int(g[1]), int(g[0])) if g[2] else date_without_year(int(g[0]), int(g[1]), as_of)
            return Span(phrase, d, d, "day", "" if g[2] else f"year inferred = {d.year}")
        if name == "date_dm":
            d = date_without_year(int(g[0]), int(g[1]), as_of)
            return Span(phrase, d, d, "day", f"year inferred = {d.year}")
        if name == "rolling":
            return _rolling(int(g[0]), g[1], as_of, conv, phrase)
        if name == "ytd":
            return Span(phrase, date(as_of.year, 1, 1), as_of, "range", "year to date, today included")
        if name == "mtd":
            return Span(phrase, as_of.replace(day=1), as_of, "range", "month to date, today included")
        if name == "month_n":
            mo = int(g[0])
            if not 1 <= mo <= 12:
                return None
            note = ""
            if g[1] is None:
                y = as_of.year if date(as_of.year, mo, 1) <= as_of else as_of.year - 1
                note = f"year inferred = {y}"
            else:
                y = _year_from_token(g[1], as_of)
            s, e = month_bounds(y, mo)
            return Span(phrase, s, e, "month", note)
        if name == "quarter_n":
            q = _ROMAN.get(g[0]) or int(g[0])
            note = ""
            if g[1] is None:
                y = as_of.year if quarter_bounds(as_of.year, q)[0] <= as_of else as_of.year - 1
                note = f"year inferred = {y}"
            else:
                y = _year_from_token(g[1], as_of)
            s, e = quarter_bounds(y, q)
            return Span(phrase, s, e, "quarter", note)
        if name == "year_n":
            y = int(g[0])
            return Span(phrase, date(y, 1, 1), date(y, 12, 31), "year")
        if name == "today":
            return Span(phrase, as_of, as_of, "day")
        if name == "yesterday":
            d = as_of - timedelta(days=1)
            return Span(phrase, d, d, "day")
        if name == "day_before":
            d = as_of - timedelta(days=2)
            return Span(phrase, d, d, "day")
        if name == "week_this":
            s, _ = week_bounds(as_of, conv.week_start)
            return Span(phrase, s, as_of, "week", "Monday of this week .. today")
        if name == "week_last":
            s, e = week_bounds(as_of - timedelta(days=7), conv.week_start)
            return Span(phrase, s, e, "week", "the whole previous week")
        if name == "month_this":
            return Span(phrase, as_of.replace(day=1), as_of, "month", "day 1 .. today")
        if name == "month_last":
            y, mo = add_months(as_of.year, as_of.month, -1)
            s, e = month_bounds(y, mo)
            return Span(phrase, s, e, "month", "the whole previous month")
        if name == "quarter_this":
            s, _ = quarter_bounds(as_of.year, (as_of.month - 1) // 3 + 1)
            return Span(phrase, s, as_of, "quarter", "first day of quarter .. today")
        if name == "quarter_last":
            q = (as_of.month - 1) // 3
            y = as_of.year
            if q == 0:
                q, y = 4, y - 1
            s, e = quarter_bounds(y, q)
            return Span(phrase, s, e, "quarter", "the whole previous quarter")
        if name == "year_this":
            return Span(phrase, date(as_of.year, 1, 1), as_of, "year", "01/01 .. today")
        if name == "year_last":
            y = as_of.year - 1
            return Span(phrase, date(y, 1, 1), date(y, 12, 31), "year", "the whole previous year")
    except ValueError:
        return None
    return None


def resolve(question: str, as_of: date, conv: Conventions | None = None) -> list[Span]:
    """Return every time expression found in `question`, in reading order."""
    conv = conv or Conventions()
    nz = Normalized(question)
    text = nz.text  # == norm(question)
    # norm() is 1:1 per character unless it collapsed whitespace, so when the
    # strings align we can show the user's original wording (with accents).
    original = unicodedata.normalize("NFC", question).strip()
    aligned = len(original) == len(text)

    def shown(m: re.Match) -> str:
        return original[m.start():m.end()] if aligned else m.group(0)

    taken: list[tuple[int, int]] = []
    found: list[tuple[int, str, re.Match]] = []
    for name, pat in _PATTERNS:
        for m in pat.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            found.append((m.start(), name, m))
    found.sort(key=lambda x: x[0])

    plain: list[tuple[int, str, Span]] = []
    for pos, name, m in found:
        if name == "cung_ky":
            continue
        span = _build(name, m, as_of, conv)
        if span is not None:
            span.phrase = shown(m)
            span.pos = nz.to_raw(m.start(), m.end())
            plain.append((pos, name, span))

    # Rulebook: "tháng này" compared with "tháng trước" -> previous month is cut to the same number of days.
    this_m = [s for _, n, s in plain if n == "month_this"]
    last_m = [s for _, n, s in plain if n == "month_last"]
    if this_m and last_m:
        for s in last_m:
            same_day_end = min(as_of.day, s.end.day)
            s.end = s.end.replace(day=same_day_end)
            s.note = f"cut to the same {as_of.day} day(s) as 'this month' for the comparison (or the whole month if shorter)"

    out = [(pos, span) for pos, _, span in plain]
    for pos, name, m in found:
        if name != "cung_ky":
            continue
        before = [p for p in out if p[0] < pos]
        after = [p for p in out if p[0] > pos]
        base = before[-1][1] if before else (after[0][1] if after else None)
        if base is not None:
            shifted = shift_span_year(base, -1, shown(m))
            shifted.pos = nz.to_raw(m.start(), m.end())
            out.append((pos, shifted))
    out.sort(key=lambda x: x[0])
    return [s for _, s in out]


def describe(spans: list[Span], as_of: date) -> list[str]:
    """Human/LLM-readable lines for the prompt."""
    lines = []
    for s in spans:
        extra = []
        if s.note:
            extra.append(s.note)
        if s.end > as_of:
            extra.append(f"after today ({as_of.isoformat()}): records dated later do not exist yet")
        tail = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f'"{s.phrase}" = {s.start.isoformat()} to {s.end.isoformat()} inclusive [{s.kind}]{tail}')
    return lines
