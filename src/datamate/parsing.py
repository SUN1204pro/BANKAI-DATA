"""Parsers for messy Vietnamese spreadsheet values: money, numbers, dates."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time

from .normalize import norm

_MULT = {"k": 1e3, "nghin": 1e3, "tr": 1e6, "trieu": 1e6, "ty": 1e9}
_NUM_RE = re.compile(r"(\d[\d.,]*)(?:(k|nghin|tr|trieu|ty)(\d+)?)?")


def _plain(num: str) -> float | None:
    """Parse digits with '.'/',' separators using Vietnamese conventions.

    "1.200.000" -> 1200000, "1,200" -> 1200, "1.5" -> 1.5, "1,5" -> 1.5,
    "0.500" -> 0.5. A single separator followed by exactly 3 digits (and at
    most 3 leading digits, not "0") is treated as a thousands separator.
    """
    if "." in num and "," in num:
        dec = "." if num.rfind(".") > num.rfind(",") else ","
        thou = "," if dec == "." else "."
        num = num.replace(thou, "").replace(dec, ".")
    elif "." in num or "," in num:
        sep = "." if "." in num else ","
        parts = num.split(sep)
        if len(parts) > 2:
            if all(len(p) == 3 for p in parts[1:]):
                num = "".join(parts)
            else:
                return None
        elif len(parts[1]) == 3 and len(parts[0]) <= 3 and parts[0] != "0":
            num = parts[0] + parts[1]
        else:
            num = parts[0] + "." + parts[1]
    try:
        return float(num)
    except ValueError:
        return None


def parse_number(value) -> float | None:
    """Parse "250k", "1.5tr", "2tr5", "1.200.000đ", "(1,200)" -> float.

    Percent strings ("15%") and anything else that is not clearly a number
    return None so the column stays text rather than being guessed at.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and math.isnan(value)) else float(value)
    if isinstance(value, (datetime, date)):
        return None
    s = norm(str(value)).replace("₫", "").replace("$", "")
    s = re.sub(r"\s+", "", s)
    if not s:
        return None
    s = re.sub(r"(vnd|dong|d)$", "", s)
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("-"):
        neg, s = True, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    m = _NUM_RE.fullmatch(s)
    if not m:
        return None
    num, suf, tail = m.groups()
    if tail:  # "2tr5" = 2.5 million, "1tr200" = 1.2 million
        if not num.isdigit():
            return None
        val = float(f"{num}.{tail}")
    else:
        val = _plain(num)
    if val is None:
        return None
    if suf:
        val *= _MULT[suf]
    return -val if neg else val


_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?$"), "ymd"),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:[ T].*)?$"), "dmy"),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2})(?:[ T].*)?$"), "dmy2"),
]


def parse_date(value) -> date | None:
    """Parse ISO, dd/mm/yyyy (Vietnamese day-first) and datetime objects."""
    if value is None:
        return None
    try:
        if value != value:  # NaN / NaT
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for pat, kind in _DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        if kind == "ymd":
            y, mo, d = a, b, c
        else:
            d, mo, y = a, b, c
            if kind == "dmy2":
                y += 2000
            if mo > 12 and d <= 12:  # month-first data
                d, mo = mo, d
        if not 1900 <= y <= 2200:
            return None
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?!\d)")


def parse_time(value) -> str | None:
    """Time of day as "HH:MM:SS", or None when the value has no (non-midnight) time part."""
    if value is None:
        return None
    try:
        if value != value:  # NaN / NaT
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        t = value.time()
    elif isinstance(value, time):
        t = value
    else:
        m = _TIME_RE.search(str(value))
        if not m:
            return None
        t = time(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    if t.hour == 0 and t.minute == 0 and t.second == 0:
        return None
    return t.strftime("%H:%M:%S")
