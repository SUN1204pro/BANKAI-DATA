"""Solar-lunar calendar lookups (Tết, rằm, mùng 1, public holidays).

Calendar file (train/calendar.csv) columns:
  date, weekday, lunar_day, lunar_month, lunar_year, lunar_leap, holiday
The loader also accepts a few other layouts (see `_KEY_ALIASES`) and raises
CalendarFormatError, naming the columns it saw, if none fits.

Tết rule from the rulebook (section 2): "Tết năm Y", "dịp Tết", "mùa Tết" run from the solar date
of 23 tháng Chạp (lunar year Y-1) to the END of the solar date of mùng 10 tháng Giêng (lunar year Y).
"So với trước Tết" = the same number of days immediately before 23 tháng Chạp.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from .normalize import Normalized, norm
from .parsing import parse_date
from .timeparse import Span


class CalendarFormatError(Exception):
    pass


@dataclass(frozen=True)
class LunarDay:
    solar: date
    day: int
    month: int
    leap: bool
    year: int
    holiday: str = ""


_KEY_ALIASES = {
    "solar": ["date", "solar_date", "duong_lich", "gregorian", "ngay_duong", "ngay_duong_lich", "solar", "duong"],
    "day": ["lunar_day", "ngay_am", "am_day", "lday", "ngay_am_lich", "lunar_d"],
    "month": ["lunar_month", "thang_am", "am_month", "lmonth", "thang_am_lich", "lunar_m"],
    "year": ["lunar_year", "nam_am", "am_year", "lyear", "nam_am_lich", "lunar_y"],
    "leap": ["lunar_leap", "is_leap", "leap", "leap_month", "nhuan", "thang_nhuan", "is_leap_month"],
    "holiday": ["holiday", "ngay_le", "le", "su_kien", "event"],
    "full": ["lunar_date", "am_lich", "ngay_am_lich_day_du", "lunar"],
}


def _key(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm(k)).strip("_")


def _pick(keys: dict[str, str], names: list[str]) -> str | None:
    for n in names:
        if n in keys:
            return keys[n]
    return None


def _truthy(v) -> bool:
    return norm(v) in {"1", "true", "yes", "co", "nhuan", "y", "t"}


class LunarCalendar:
    def __init__(self, days: list[LunarDay]):
        self.days = sorted(days, key=lambda d: d.solar)
        self.by_solar = {d.solar: d for d in self.days}
        self.by_lunar = {(d.year, d.month, d.day, d.leap): d.solar for d in self.days}
        self._holidays: dict[str, list[tuple[date, date]]] | None = None

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | Path) -> "LunarCalendar":
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("days") or data.get("data") or list(data.values())[0]
            rows = list(data)
        else:
            import pandas as pd

            if suffix in (".xlsx", ".xlsm", ".xls"):
                df = pd.read_excel(path, dtype=object)
            else:
                df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig", sep=None, engine="python")
            rows = df.to_dict("records")
        return cls.from_rows(rows)

    @classmethod
    def from_rows(cls, rows: Iterable[dict]) -> "LunarCalendar":
        days: list[LunarDay] = []
        cols: dict[str, str] | None = None
        for row in rows:
            if cols is None:
                cols = {_key(k): k for k in row}
            sk = _pick(cols, _KEY_ALIASES["solar"])
            if sk is None:
                raise CalendarFormatError(f"no solar-date column; columns seen: {list(row)}")
            solar = parse_date(row[sk])
            if solar is None:
                continue
            dk, mk, yk = (_pick(cols, _KEY_ALIASES[k]) for k in ("day", "month", "year"))
            lk = _pick(cols, _KEY_ALIASES["leap"])
            hk = _pick(cols, _KEY_ALIASES["holiday"])
            holiday = str(row[hk]).strip() if hk and row[hk] is not None else ""
            if holiday.lower() in {"nan", "none"}:
                holiday = ""
            if dk and mk:
                lday, lmonth = int(float(row[dk])), int(float(row[mk]))
                lyear = int(float(row[yk])) if yk and str(row[yk]).strip() else solar.year
                leap = _truthy(row[lk]) if lk else False
            else:
                fk = _pick(cols, _KEY_ALIASES["full"])
                if fk is None:
                    raise CalendarFormatError(f"no lunar columns; columns seen: {list(row)}")
                text = str(row[fk])
                m = re.search(r"(\d{1,2})\D+(\d{1,2})\D+(\d{4})", text)
                if not m:
                    continue
                lday, lmonth, lyear = int(m.group(1)), int(m.group(2)), int(m.group(3))
                leap = "nhuan" in norm(text) or "leap" in norm(text)
            days.append(LunarDay(solar, lday, lmonth, leap, lyear, holiday))
        if not days:
            raise CalendarFormatError("calendar file contained no usable rows")
        return cls(days)

    # ------------------------------------------------------------ queries
    def lunar_to_solar(self, lmonth: int, lday: int, lyear: int, leap: bool = False) -> date | None:
        return self.by_lunar.get((lyear, lmonth, lday, leap))

    def solar_to_lunar(self, d: date) -> LunarDay | None:
        return self.by_solar.get(d)

    def occurrences(self, lday: int, start: date, end: date) -> list[date]:
        """Solar dates in [start, end] that fall on lunar day `lday` (non-leap and leap months)."""
        return [d.solar for d in self.days if d.day == lday and start <= d.solar <= end]

    def holiday_groups(self) -> dict[str, list[tuple[date, date]]]:
        """normalised holiday name -> [(first day, last day), ...] (consecutive days are one group)."""
        if self._holidays is None:
            raw: dict[str, list[date]] = {}
            for d in self.days:
                if d.holiday:
                    raw.setdefault(norm(d.holiday), []).append(d.solar)
            groups: dict[str, list[tuple[date, date]]] = {}
            for name, ds in raw.items():
                ds.sort()
                cur = [ds[0], ds[0]]
                out = []
                for x in ds[1:]:
                    if (x - cur[1]).days <= 1:
                        cur[1] = x
                    else:
                        out.append((cur[0], cur[1]))
                        cur = [x, x]
                out.append((cur[0], cur[1]))
                groups[name] = out
            self._holidays = groups
        return self._holidays

    def tet_window(self, y: int) -> tuple[date, date] | None:
        """Rulebook window for Tết of year Y: 23/12 (lunar year Y-1) .. 10/1 (lunar year Y), both included."""
        s = self.lunar_to_solar(12, 23, y - 1)
        e = self.lunar_to_solar(1, 10, y)
        return (s, e) if s and e else None


# Fallback festivals for calendars without a `holiday` column: phrase -> (lunar month, lunar day).
FESTIVALS: dict[str, tuple[int, int]] = {
    "ram thang gieng": (1, 15),
    "nguyen tieu": (1, 15),
    "gio to hung vuong": (3, 10),
    "vu lan": (7, 15),
    "trung thu": (8, 15),
    "ong cong ong tao": (12, 23),
}

_TET_DAY = re.compile(r"\bm(?:ung|ong) (\d{1,2}) tet(?! duong)(?: (?:nam (nay|ngoai|truoc|roi|\d{4})|(\d{4})))?\b")
_TET = re.compile(r"\b(truoc )?tet(?! duong)(?: (?:nguyen dan|am lich))?(?: (?:nam (nay|ngoai|truoc|roi|\d{4})|(\d{4})))?\b")


def _year_token(tok: str | None, as_of: date) -> int | None:
    if tok is None:
        return None
    if tok.isdigit():
        return int(tok)
    return as_of.year if tok == "nay" else as_of.year - 1


def _default_tet_year(cal: LunarCalendar, as_of: date) -> int:
    w = cal.tet_window(as_of.year)
    return as_of.year if (w and w[0] <= as_of) else as_of.year - 1


def resolve_lunar(question: str, cal: LunarCalendar, as_of: date) -> list[Span]:
    """Tết / festival / holiday expressions -> solar spans. Year = explicit, else the latest one not after today."""
    nz = Normalized(question)
    text = nz.text  # == norm(question)
    spans: list[tuple[int, Span]] = []
    taken: list[tuple[int, int]] = []

    def put(m: re.Match, span: Span) -> None:
        span.pos = nz.to_raw(m.start(), m.end())
        spans.append((m.start(), span))

    def free(m: re.Match) -> bool:
        return not any(m.start() < e and s < m.end() for s, e in taken)

    def take(m: re.Match) -> None:
        taken.append((m.start(), m.end()))

    # "mùng N Tết": one lunar day of the first month
    for m in _TET_DAY.finditer(text):
        if not free(m):
            continue
        take(m)
        y = _year_token(m.group(2) or m.group(3), as_of) or _default_tet_year(cal, as_of)
        d = cal.lunar_to_solar(1, int(m.group(1)), y)
        if d:
            put(m, Span(m.group(0), d, d, "lunar_festival", f"lunar {m.group(1)}/1 of {y}"))

    # Tết season, and "trước Tết" (equal-length window right before it)
    for m in _TET.finditer(text):
        if not free(m):
            continue
        take(m)
        y = _year_token(m.group(2) or m.group(3), as_of) or _default_tet_year(cal, as_of)
        w = cal.tet_window(y)
        if w is None:
            continue
        start, end = w
        if m.group(1):  # "trước Tết"
            n = (end - start).days + 1
            put(m, Span(m.group(0), start - timedelta(days=n), start - timedelta(days=1), "lunar_festival",
                        f"{n} days right before 23 tháng Chạp ({start.isoformat()}) of Tết {y}"))
        else:
            put(m, Span(m.group(0), start, end, "lunar_festival",
                        f"Tết {y}: 23 tháng Chạp .. mùng 10 tháng Giêng, both included"))

    # holidays named in the calendar's own `holiday` column (longest names first)
    ym = re.search(r"\bnam (nay|ngoai|truoc|roi|\d{4})\b", text)
    year_hint = _year_token(ym.group(1), as_of) if ym else None
    for name, groups in sorted(cal.holiday_groups().items(), key=lambda kv: -len(kv[0])):
        for m in re.finditer(rf"\b{re.escape(name)}\b", text):
            if not free(m):
                continue
            take(m)
            cands = [g for g in groups if g[0] <= as_of]
            if year_hint is not None:
                cands = [g for g in groups if g[0].year == year_hint]
            if not cands:
                continue
            s, e = max(cands, key=lambda g: g[0])
            put(m, Span(m.group(0), s, e, "lunar_festival", "from the calendar's holiday column"))

    # fallback lunar festivals
    for phrase, (lm, ld) in sorted(FESTIVALS.items(), key=lambda kv: -len(kv[0])):
        for m in re.finditer(rf"\b{re.escape(phrase)}\b", text):
            if not free(m):
                continue
            take(m)
            if year_hint is not None:
                solar = cal.lunar_to_solar(lm, ld, year_hint)
            else:
                solar = next((c for y in (as_of.year, as_of.year - 1)
                              if (c := cal.lunar_to_solar(lm, ld, y)) is not None and c <= as_of), None)
            if solar:
                put(m, Span(m.group(0), solar, solar, "lunar_festival", f"lunar {ld}/{lm}"))

    spans.sort(key=lambda x: x[0])
    return [s for _, s in spans]


def recurring_lunar_days(question: str) -> list[int]:
    """Lunar days mentioned as recurring ('rằm' = 15, 'mùng/mồng N')."""
    text = norm(question)
    out: list[int] = []
    if re.search(r"\bram\b", text) and "ram thang" not in text:
        out.append(15)
    if "tet" not in text:
        out += [int(n) for n in re.findall(r"\bm(?:ung|ong) (\d{1,2})\b", text)]
    return sorted(set(out))
