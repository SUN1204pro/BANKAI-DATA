"""Resolve Vietnamese time expressions (Rulebook §2) from canonicalized question text.

Every span that is understood is replaced with `<t>`, so the question parser can tell whether anything
in the question was left unexplained. All relative language anchors to `as_of_date`.
"""

from __future__ import annotations

import calendar as py_calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
import re

from src.grounder import CalendarIndex, DateWindow


@dataclass
class TimeSpec:
    windows: list[DateWindow] = field(default_factory=list)
    snapshot: str | None = None
    comparison: DateWindow | None = None
    weekday: int | None = None
    month_range: bool = False
    is_tet: bool = False
    same_day_count_basis: bool = False
    text: str = ""


def month_window(year: int, month: int) -> DateWindow:
    last = py_calendar.monthrange(year, month)[1]
    return DateWindow(date(year, month, 1).isoformat(), date(year, month, last).isoformat(), label=f"{year}-{month:02d}")


def quarter_window(year: int, quarter: int) -> DateWindow:
    first = (quarter - 1) * 3 + 1
    last_m = first + 2
    last = py_calendar.monthrange(year, last_m)[1]
    return DateWindow(date(year, first, 1).isoformat(), date(year, last_m, last).isoformat(), label=f"Q{quarter}/{year}")


def _year(y: str) -> int:
    v = int(y)
    return v + 2000 if v < 100 else v


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _resolve_dm(d: str, m: str, y: str | None, as_of: date) -> date | None:
    if y:
        return _safe_date(_year(y), int(m), int(d))
    candidate = _safe_date(as_of.year, int(m), int(d))
    if candidate and candidate > as_of:
        return _safe_date(as_of.year - 1, int(m), int(d))
    return candidate


def _yearless_month(m: int, as_of: date) -> int:
    return as_of.year if m <= as_of.month else as_of.year - 1


def previous_window(win: DateWindow, as_of_str: str, *, same_day_count: bool = False) -> DateWindow | None:
    """The period before `win`, per Rulebook §1-2. Month-to-date compares to equal days of the last month."""
    start = date.fromisoformat(win.start_date)
    end = date.fromisoformat(win.end_date)
    if start.day == 1 and same_day_count:
        prev_y, prev_m = (start.year, start.month - 1) if start.month > 1 else (start.year - 1, 12)
        last = py_calendar.monthrange(prev_y, prev_m)[1]
        return DateWindow(date(prev_y, prev_m, 1).isoformat(), date(prev_y, prev_m, min(end.day, last)).isoformat())
    if start.day == 1 and end.day == py_calendar.monthrange(end.year, end.month)[1]:
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        idx = start.year * 12 + start.month - 1 - months
        py, pm = idx // 12, idx % 12 + 1
        eidx = idx + months - 1
        ey, em = eidx // 12, eidx % 12 + 1
        return DateWindow(date(py, pm, 1).isoformat(), date(ey, em, py_calendar.monthrange(ey, em)[1]).isoformat())
    days = (end - start).days + 1
    return DateWindow((start - timedelta(days=days)).isoformat(), (start - timedelta(days=1)).isoformat())


def same_period_last_year(win: DateWindow) -> DateWindow:
    def shift(d: date) -> date:
        last = py_calendar.monthrange(d.year - 1, d.month)[1]
        return date(d.year - 1, d.month, min(d.day, last))

    start, end = date.fromisoformat(win.start_date), date.fromisoformat(win.end_date)
    if end.day == py_calendar.monthrange(end.year, end.month)[1]:
        end_shift = date(end.year - 1, end.month, py_calendar.monthrange(end.year - 1, end.month)[1])
    else:
        end_shift = shift(end)
    return DateWindow(shift(start).isoformat(), end_shift.isoformat())


_WEEKDAY_RE = re.compile(r"(?:ngay |cac |moi )?(?:thu (hai|ba|tu|nam|sau|bay|[2-7])\b|(chu nhat|cn)\b)")
_WEEKDAYS = {"hai": 1, "2": 1, "ba": 2, "3": 2, "tu": 3, "4": 3, "nam": 4, "5": 4, "sau": 5, "6": 5, "bay": 6, "7": 6}
DATE = r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?"


def parse_time(text: str, as_of_date: str, calendar: CalendarIndex | None) -> TimeSpec:
    """Extract time windows from canonical text. Returns a spec whose `.text` has spans replaced by <t>."""
    as_of = date.fromisoformat(as_of_date)
    spec = TimeSpec()
    t = " " + text + " "
    found: list[DateWindow] = []

    def take(pattern: str, builder) -> None:
        """Replace each understood span with a numbered marker, so mention order survives multiple passes."""
        nonlocal t
        out, pos = [], 0
        for m in re.finditer(pattern, t):
            res = builder(m)
            if res is False:
                continue
            wins = res if isinstance(res, list) else ([res] if res is not None else [])
            tokens = []
            for w in wins:
                found.append(w)
                tokens.append(f"<t{len(found) - 1}>")
            out.append(t[pos:m.start()] + " " + (" ".join(tokens) or "<t>") + " ")
            pos = m.end()
        if out:
            t = "".join(out) + t[pos:]

    # Snapshot phrasing: "den het ngay D", "tinh den hom nay", "cuoi ngay hom nay", "het hom nay".
    def snap_date(m):
        d = _resolve_dm(m.group(1), m.group(2), m.group(3), as_of)
        if d is None:
            return False
        spec.snapshot = d.isoformat()
        return None

    take(r"\b(?:tinh |tính )?(?:den|toi|toi het|den het|het|cuoi|tai)\s*(?:het\s*)?ngay\s+" + DATE, snap_date)

    def snap_today(m):
        spec.snapshot = as_of_date
        return None

    # Only snapshot-style wording: "tu dau nam den het hom nay" is a flow window handled below.
    if not re.search(r"\btu dau\b|\btu ngay\b|\bthang nay\b", t):
        take(r"\b(?:tinh )?(?:den|toi) (?:het |cuoi )?(?:ngay )?hom nay\b|\bhet hom nay\b|\bcuoi ngay hom nay\b|\bcuoi hom nay\b", snap_today)

    # Tết windows
    def tet(m):
        if calendar is None:
            return False
        y = int(m.group(1)) if m.group(1) else as_of.year
        resolved = calendar.resolve_tet_window(y)
        if resolved is None:
            return False
        spec.is_tet = True
        spec.comparison = resolved[1]
        return resolved[0]

    # "dip Tet nam nay ... dip Tet nam ngoai": two explicit Tet periods, compared with each other.
    def tet_relative(m):
        if calendar is None:
            return False
        y = as_of.year if m.group(1) == "nay" else as_of.year - 1
        resolved = calendar.resolve_tet_window(y)
        return resolved[0] if resolved else False

    take(r"\b(?:dip |mua )?tet nam (nay|ngoai|truoc)\b", tet_relative)
    take(r"\b(?:dip |mua )?tet(?: am lich| nguyen dan)?(?: nam)?\s*(20\d\d)?\b(?! *truoc)", tet)
    if spec.is_tet:
        t = re.sub(r"\b(?:cung so ngay |\d+ ngay )?(?:ngay )?truoc <t\d*>|\b(?:\d+ ngay )?(?:ngay )?truoc tet\b", " <pre> ", t)

    # Explicit day ranges
    def day_range(m):
        d2 = _resolve_dm(m.group(4), m.group(5), m.group(6), as_of)
        d1 = _resolve_dm(m.group(1), m.group(2), m.group(3) or m.group(6), d2 or as_of)
        if not d1 or not d2:
            return False
        return DateWindow(d1.isoformat(), d2.isoformat())

    take(r"\b(?:tu |giai doan tu )(?:ngay )?" + DATE + r"\s+(?:den|toi)\s+(?:het\s+)?(?:ngay\s+)?" + DATE, day_range)

    # Month ranges: "tu thang 3/2025 den thang 5/2025", "thang 3/2025 den thang 5/2025"
    def month_range(m):
        y2 = _year(m.group(4)) if m.group(4) else None
        m1, m2 = int(m.group(1)), int(m.group(3))
        y1 = _year(m.group(2)) if m.group(2) else (y2 if y2 else _yearless_month(m1, as_of))
        y2 = y2 or y1
        idx, end_idx = y1 * 12 + m1 - 1, y2 * 12 + m2 - 1
        if idx > end_idx or not (1 <= m1 <= 12 and 1 <= m2 <= 12):
            return False
        spec.month_range = True
        return [month_window(i // 12, i % 12 + 1) for i in range(idx, end_idx + 1)]

    take(r"\b(?:tu )?thang (\d{1,2})(?:[/\-](\d{2,4}))? (?:den|toi) (?:het )?thang (\d{1,2})(?:[/\-](\d{2,4}))?", month_range)

    # "3 thang lien nhau ket thuc o thang 12/2025": months ending at the named month.
    def months_ending(m):
        n, mo = int(m.group(1)), int(m.group(2))
        y = _year(m.group(3)) if m.group(3) else _yearless_month(mo, as_of)
        end = y * 12 + mo - 1
        spec.month_range = True
        return [month_window(i // 12, i % 12 + 1) for i in range(end - n + 1, end + 1)]

    take(r"\b(\d+) thang (?:lien nhau|lien tiep) ket thuc o thang (\d{1,2})(?:[/\-](\d{2,4}))?", months_ending)

    # Month lists sharing one year: "thang 5, 6, 7/2025", "thang 5, 6 va 7 nam 2025"
    def month_list(m):
        y = _year(m.group(3))
        months = [int(x) for x in re.findall(r"\d{1,2}", m.group(1))] + [int(m.group(2))]
        if not all(1 <= v <= 12 for v in months):
            return False
        return [month_window(y, v) for v in months]

    take(r"\bthang ((?:\d{1,2}\s*(?:,|va)\s*)+)(\d{1,2})\s*(?:/|-|nam )\s*(\d{2,4})", month_list)

    # "tu dau thang nay ...", "thang nay"
    def mtd(m):
        spec.same_day_count_basis = True
        return DateWindow(date(as_of.year, as_of.month, 1).isoformat(), as_of_date, label="thang nay")

    take(r"\b(?:tu )?dau thang(?: nay)?(?: (?:den|toi) (?:het )?(?:ngay )?hom nay)?\b|\bthang nay(?: (?:den|toi) (?:het )?(?:ngay )?hom nay)?\b", mtd)

    def ytd(m):
        return DateWindow(date(as_of.year, 1, 1).isoformat(), as_of_date, label="tu dau nam")

    take(r"\b(?:tu )?dau nam(?: nay)?(?: (?:den|toi) (?:het )?(?:ngay )?hom nay)?\b|\bnam nay(?: (?:tinh )?(?:den|toi) (?:het )?(?:ngay )?hom nay)?\b|\b(?:tinh )?tu (?:ngay )?1/1(?: (?:den|toi) (?:het )?(?:ngay )?hom nay)?\b", ytd)

    def last_month(m):
        y, mo = (as_of.year, as_of.month - 1) if as_of.month > 1 else (as_of.year - 1, 12)
        return month_window(y, mo)

    def explicit_quarter(m):
        q = {"1": 1, "2": 2, "3": 3, "4": 4, "i": 1, "ii": 2, "iii": 3, "iv": 4}[m.group(1)]
        if m.group(2):
            return quarter_window(_year(m.group(2)), q)
        cur = (as_of.month - 1) // 3 + 1
        return quarter_window(as_of.year if q <= cur else as_of.year - 1, q)

    take(r"\bquy (1|2|3|4|iii|ii|iv|i)\b(?:\s*(?:/|-|nam )\s*(\d{2,4}))?", explicit_quarter)

    def explicit_month(m):
        mo = int(m.group(1))
        if not 1 <= mo <= 12:
            return False
        y = _year(m.group(2)) if m.group(2) else _yearless_month(mo, as_of)
        return month_window(y, mo)

    take(r"\bthang (\d{1,2})(?:\s*(?:/|-|nam )\s*(\d{2,4}))?\b(?![/\-]\d)", explicit_month)

    # Bare day-month-year: only with an explicit "ngay" or a four-digit year.
    def explicit_day(m):
        d = _resolve_dm(m.group(1), m.group(2), m.group(3), as_of)
        if d is None:
            return False
        return DateWindow(d.isoformat(), d.isoformat())

    take(r"\bngay\s+" + DATE + r"\b", explicit_day)
    take(r"(?<![\d/.\-])(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})(?![\d])", explicit_day)

    # Bare months: "7/2025", "01/2026", "8-2025", "3/26" (two-digit year only for plausible data years)
    def bare_month(m):
        mo = int(m.group(1))
        if not 1 <= mo <= 12:
            return False
        return month_window(_year(m.group(2)), mo)

    take(r"(?<![\d/.\-])(\d{1,2})[/\-](20\d\d|2[4-7])(?![\d/\-.])", bare_month)

    def this_week(m):
        mon = as_of - timedelta(days=as_of.isoweekday() - 1)
        return DateWindow(mon.isoformat(), as_of_date, label="tuan nay")

    def last_week(m):
        mon = as_of - timedelta(days=as_of.isoweekday() - 1)
        return DateWindow((mon - timedelta(days=7)).isoformat(), (mon - timedelta(days=1)).isoformat(), label="tuan truoc")

    take(r"\btuan nay(?: (?:tinh )?(?:den|toi) (?:het )?hom nay)?\b", this_week)
    take(r"\btuan (?:truoc|roi|vua qua)\b", last_week)

    # "thang truoc" names a period only when no other period is given; otherwise it is a comparison.
    take(r"(?<!voi )(?<!so )\bthang (?:truoc|roi|vua qua)\b(?! do)", lambda m: last_month(m) if not found else False)

    def last_n(m):
        n = int(m.group(1))
        return DateWindow((as_of - timedelta(days=max(0, n - 1))).isoformat(), as_of_date, label=f"{n} ngay")

    take(r"\b(\d+) ngay (?:gan nhat|gan day|qua|vua qua)(?: ke ca hom nay)?\b", last_n)

    def full_year(m):
        return DateWindow(f"{m.group(1)}-01-01", f"{m.group(1)}-12-31", label=m.group(1))

    take(r"\bnam (20\d\d)\b", full_year)
    take(r"\bhom nay\b", lambda m: DateWindow(as_of_date, as_of_date, label="hom nay"))
    yest = (as_of - timedelta(days=1)).isoformat()
    take(r"\bhom qua\b", lambda m: DateWindow(yest, yest, label="hom qua"))
    before = (as_of - timedelta(days=2)).isoformat()
    take(r"\bhom kia\b", lambda m: DateWindow(before, before, label="hom kia"))

    # Weekday filter. Only with weekday wording, not "thu" as in "thu ve".
    m_wd = re.search(r"\b(?:ngay|moi|cac) (?:thu (hai|ba|tu|nam|sau|bay|[2-7])\b|(chu nhat))", t) or re.search(r"\b(chu nhat)\b", t)
    if m_wd:
        groups = [g for g in m_wd.groups() if g]
        spec.weekday = 7 if groups and groups[-1] == "chu nhat" else _WEEKDAYS.get(groups[0]) if groups else None
        t = t[: m_wd.start()] + " <wd> " + t[m_wd.end():]

    ordered = [found[int(i)] for i in re.findall(r"<t(\d+)>", t)]
    seen: set[tuple[str, str]] = set()
    for w in ordered:
        if (w.start_date, w.end_date) not in seen:
            seen.add((w.start_date, w.end_date))
            spec.windows.append(w)
    t = re.sub(r"<t\d+>", "<t>", t)
    spec.text = re.sub(r"\s+", " ", t).strip()
    return spec
