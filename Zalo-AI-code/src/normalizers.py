"""Lossless cell normalizers for Vietnamese SME spreadsheet/CSV data.

Implements the parsing and diagnostic contracts specified in
.agents/skills/datamate-ingestion/SKILL.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import functools
import re
from typing import Any
import unicodedata

from src.postprocess import normalize_comparison_string, normalize_display_string


@dataclass(frozen=True)
class Diagnostic:
    """Explicit diagnostic emitted when a cell value is ambiguous or unparseable."""

    code: str
    message: str
    raw: Any = None
    severity: str = "warning"  # "warning" or "error"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParseResult:
    """Standard return wrapper supporting both attribute access and 3-tuple unpacking."""

    value: Any
    meta: dict[str, Any] = field(default_factory=dict)
    diagnostic: Diagnostic | None = None

    def __iter__(self):
        yield self.value
        yield self.meta
        yield self.diagnostic


# ---------------------------------------------------------------------------
# 1. Money Parser (SKILL.md §4.1)
# ---------------------------------------------------------------------------

_CURRENCY_STRIP_RE = re.compile(
    r"(?:^\s*(?:vnđ|vnd|đồng|đ|₫)\s*|\s*(?:vnđ|vnd|đồng|đ|₫)\s*$)",
    re.IGNORECASE,
)
_COMPOUND_TR_RE = re.compile(r"^([+-]?)\s*(\d+)\s*tr(?:iệu)?\s*(\d+)$", re.IGNORECASE)
_SUFFIX_TR_RE = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*tr(?:iệu)?$", re.IGNORECASE)
_COMPOUND_K_RE = re.compile(r"^([+-]?)\s*(\d+)\s*k\s*(\d+)$", re.IGNORECASE)
_SUFFIX_K_RE = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*(?:k|nghìn|ngàn)$", re.IGNORECASE)

_US_DEC_THOUSANDS_RE = re.compile(r"^([+-]?\d{1,3}(?:,\d{3})+)\.(\d+)$")
_EU_DEC_THOUSANDS_RE = re.compile(r"^([+-]?\d{1,3}(?:\.\d{3})+),(\d+)$")
_DOT_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(?:\.\d{3})+$")
_COMMA_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+$")
_SPACE_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(?:\s+\d{3})+$")
_PLAIN_INT_RE = re.compile(r"^[+-]?\d+$")
_PLAIN_ZERO_DEC_RE = re.compile(r"^([+-]?\d+)[.,]0+$")
_UNSUFFIXED_DEC_RE = re.compile(r"^[+-]?\d+[.,]\d+$")

_EMPTY_PLACEHOLDERS = frozenset({"", "-", "–", "—", "n/a", "na", "null", "none"})


def _normalize_decimal_Scale(d: Decimal) -> Decimal:
    """Return an integral Decimal if fractional part is zero, else normalized Decimal."""
    if d == d.to_integral_value():
        return d.quantize(Decimal("1"))
    return d.normalize()


def parse_money(raw: Any) -> ParseResult:
    """Parse a Vietnamese monetary value into an exact Decimal VND amount.

    Order of precedence (SKILL.md §4.1):
    1. Suffixed shorthand: `1tr2`, `1,2tr`, `25k`, `38k`
    2. Explicit decimal + thousands separator: `25,000.0000`
    3. 3-digit thousands groupings: `25.000đ`, `1.800.000 đ`, `38,000`, `16 076`
    4. Plain integer or `.0000` integral float
    5. Ambiguous unsuffixed decimal -> emits diagnostic rather than guessing scale.
    """
    if raw is None:
        return ParseResult(None, {"raw": raw, "format": "null"}, None)

    if isinstance(raw, bool):
        return ParseResult(
            None,
            {"raw": raw, "format": "invalid_bool"},
            Diagnostic("INVALID_MONEY_BOOL", "Boolean value in money field", raw, severity="error"),
        )

    if isinstance(raw, int):
        return ParseResult(Decimal(raw), {"raw": raw, "format": "int"}, None)

    if isinstance(raw, Decimal):
        if not raw.is_finite():
            return ParseResult(
                None,
                {"raw": raw, "format": "non_finite"},
                Diagnostic("NON_FINITE_MONEY", "Non-finite Decimal in money field", raw, severity="error"),
            )
        return ParseResult(_normalize_decimal_Scale(raw), {"raw": raw, "format": "decimal"}, None)

    if isinstance(raw, float):
        d = Decimal(str(raw))
        if d == d.to_integral_value():
            return ParseResult(d.quantize(Decimal("1")), {"raw": raw, "format": "float_int"}, None)
        return ParseResult(
            d,
            {"raw": raw, "format": "float_decimal"},
            Diagnostic(
                "FLOAT_NON_INTEGRAL_MONEY",
                f"Non-integral float {raw!r} in money field",
                raw,
                severity="warning",
            ),
        )

    if not isinstance(raw, str):
        return ParseResult(
            None,
            {"raw": raw, "format": "unsupported_type"},
            Diagnostic("UNSUPPORTED_MONEY_TYPE", f"Unsupported type {type(raw).__name__}", raw, severity="error"),
        )

    return _parse_money_str(raw)


@functools.lru_cache(maxsize=65536)
def _parse_money_str(raw: str) -> ParseResult:
    s = unicodedata.normalize("NFC", raw).strip()
    if s.lower() in _EMPTY_PLACEHOLDERS:
        return ParseResult(None, {"raw": raw, "format": "empty"}, None)

    # Strip currency markers from start/end (preserving leading +/-)
    s_clean = _CURRENCY_STRIP_RE.sub("", s).strip()
    if s_clean.startswith(("+", "-")) and len(s_clean) > 1:
        sign = s_clean[0]
        rest = _CURRENCY_STRIP_RE.sub("", s_clean[1:]).strip()
        s_clean = sign + rest
    s_clean = _CURRENCY_STRIP_RE.sub("", s_clean).strip()

    if s_clean.lower() in _EMPTY_PLACEHOLDERS:
        return ParseResult(None, {"raw": raw, "format": "empty"}, None)

    # 1a. Compound `<X>tr<Y>` (e.g. `1tr2` -> 1,200,000; `1tr25` -> 1,250,000)
    m = _COMPOUND_TR_RE.match(s_clean)
    if m:
        sign_str, major_str, minor_str = m.groups()
        sign_mult = Decimal(-1) if sign_str == "-" else Decimal(1)
        val = sign_mult * (
            Decimal(major_str) * Decimal(1_000_000)
            + Decimal(f"0.{minor_str}") * Decimal(1_000_000)
        )
        return ParseResult(_normalize_decimal_Scale(val), {"raw": raw, "format": "compound_tr"}, None)

    # 1b. Suffixed `<num>tr` (e.g. `1,2tr` -> 1,200,000; `1.2tr` -> 1,200,000)
    m = _SUFFIX_TR_RE.match(s_clean)
    if m:
        num_str = m.group(1).replace(",", ".")
        val = Decimal(num_str) * Decimal(1_000_000)
        return ParseResult(_normalize_decimal_Scale(val), {"raw": raw, "format": "suffix_tr"}, None)

    # 1c. Compound `<X>k<Y>` (e.g. `15k5` -> 15,500)
    m = _COMPOUND_K_RE.match(s_clean)
    if m:
        sign_str, major_str, minor_str = m.groups()
        sign_mult = Decimal(-1) if sign_str == "-" else Decimal(1)
        val = sign_mult * (
            Decimal(major_str) * Decimal(1_000)
            + Decimal(f"0.{minor_str}") * Decimal(1_000)
        )
        return ParseResult(_normalize_decimal_Scale(val), {"raw": raw, "format": "compound_k"}, None)

    # 1d. Suffixed `<num>k` (e.g. `25k`, `38k`, `92k`, `195k`)
    m = _SUFFIX_K_RE.match(s_clean)
    if m:
        num_str = m.group(1).replace(",", ".")
        val = Decimal(num_str) * Decimal(1_000)
        return ParseResult(_normalize_decimal_Scale(val), {"raw": raw, "format": "suffix_k"}, None)

    # 2a. US thousands + decimal (e.g. `25,000.0000`)
    m = _US_DEC_THOUSANDS_RE.match(s_clean)
    if m:
        int_part = m.group(1).replace(",", "")
        frac_part = m.group(2)
        val = Decimal(f"{int_part}.{frac_part}")
        return ParseResult(_normalize_decimal_Scale(val), {"raw": raw, "format": "us_thousands_dec"}, None)

    # 2b. EU/VN thousands + decimal (e.g. `25.000,00`)
    m = _EU_DEC_THOUSANDS_RE.match(s_clean)
    if m:
        int_part = m.group(1).replace(".", "")
        frac_part = m.group(2)
        val = Decimal(f"{int_part}.{frac_part}")
        return ParseResult(_normalize_decimal_Scale(val), {"raw": raw, "format": "eu_thousands_dec"}, None)

    # 3a. Dot-separated thousands (e.g. `25.000`, `1.800.000`)
    if _DOT_THOUSANDS_RE.match(s_clean):
        val = Decimal(s_clean.replace(".", ""))
        return ParseResult(val, {"raw": raw, "format": "dot_thousands"}, None)

    # 3b. Comma-separated thousands (e.g. `38,000`)
    if _COMMA_THOUSANDS_RE.match(s_clean):
        val = Decimal(s_clean.replace(",", ""))
        return ParseResult(val, {"raw": raw, "format": "comma_thousands"}, None)

    # 3c. Space-separated thousands (e.g. `16 076`)
    if _SPACE_THOUSANDS_RE.match(s_clean):
        val = Decimal(re.sub(r"\s+", "", s_clean))
        return ParseResult(val, {"raw": raw, "format": "space_thousands"}, None)

    # 4a. Plain integer
    if _PLAIN_INT_RE.match(s_clean):
        return ParseResult(Decimal(s_clean), {"raw": raw, "format": "plain_int"}, None)

    # 4b. Plain integer with `.0000` / `,00` trailing zeros
    m = _PLAIN_ZERO_DEC_RE.match(s_clean)
    if m:
        return ParseResult(Decimal(m.group(1)), {"raw": raw, "format": "plain_zero_dec"}, None)

    # 5. Unsuffixed decimal with non-3-digit fractional part -> ambiguous in VND money column!
    if _UNSUFFIXED_DEC_RE.match(s_clean):
        return ParseResult(
            None,
            {"raw": raw, "format": "ambiguous_unsuffixed_decimal"},
            Diagnostic(
                "AMBIGUOUS_UNSUFFIXED_DECIMAL_MONEY",
                f"Unsuffixed decimal {raw!r} in money field is ambiguous (thousands vs millions vs fractional VND)",
                raw,
                severity="error",
            ),
        )

    return ParseResult(
        None,
        {"raw": raw, "format": "unparseable"},
        Diagnostic("UNPARSEABLE_MONEY", f"Cannot parse money value: {raw!r}", raw, severity="error"),
    )


# ---------------------------------------------------------------------------
# 2. Quantity + Unit Parser (SKILL.md §4.2)
# ---------------------------------------------------------------------------

_QTY_UNIT_RE = re.compile(
    r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*(.*?)\s*$",
    re.DOTALL,
)


def parse_quantity(raw: Any) -> ParseResult:
    """Parse a quantity cell into a signed Decimal quantity and preserved unit string.

    Examples (SKILL.md §4.2):
    - `-2 bao` -> value=Decimal('-2'), meta={'unit': 'bao', 'unit_norm': 'bao'}
    - `1 m3` -> value=Decimal('1'), meta={'unit': 'm3', 'unit_norm': 'm3'}
    - `1 xe 4m3` -> value=Decimal('1'), meta={'unit': 'xe 4m3', 'unit_norm': 'xe 4m3'}
    - `1 bao 25kg` -> value=Decimal('1'), meta={'unit': 'bao 25kg', 'unit_norm': 'bao 25kg'}
    """
    if raw is None:
        return ParseResult(None, {"raw": raw, "unit": None, "unit_norm": None}, None)

    if isinstance(raw, bool):
        return ParseResult(
            None,
            {"raw": raw, "unit": None, "unit_norm": None},
            Diagnostic("INVALID_QTY_BOOL", "Boolean value in quantity field", raw, severity="error"),
        )

    if isinstance(raw, (int, Decimal)):
        d = Decimal(raw) if isinstance(raw, int) else raw
        return ParseResult(
            _normalize_decimal_Scale(d),
            {"raw": raw, "unit": None, "unit_norm": None},
            None,
        )

    if isinstance(raw, float):
        d = Decimal(str(raw))
        return ParseResult(
            _normalize_decimal_Scale(d),
            {"raw": raw, "unit": None, "unit_norm": None},
            None,
        )

    if not isinstance(raw, str):
        return ParseResult(
            None,
            {"raw": raw, "unit": None, "unit_norm": None},
            Diagnostic("UNSUPPORTED_QTY_TYPE", f"Unsupported type {type(raw).__name__}", raw, severity="error"),
        )

    return _parse_quantity_str(raw)


@functools.lru_cache(maxsize=65536)
def _parse_quantity_str(raw: str) -> ParseResult:
    s = unicodedata.normalize("NFC", raw).strip()
    if s.lower() in _EMPTY_PLACEHOLDERS:
        return ParseResult(None, {"raw": raw, "unit": None, "unit_norm": None}, None)

    m = _QTY_UNIT_RE.match(s)
    if not m:
        return ParseResult(
            None,
            {"raw": raw, "unit": None, "unit_norm": None},
            Diagnostic("UNPARSEABLE_QTY", f"Cannot parse quantity value: {raw!r}", raw, severity="error"),
        )

    num_str, unit_part = m.groups()
    # Handle comma vs dot in quantity number
    if "," in num_str and "." not in num_str:
        # In Vietnamese quantities, `1,5` is 1.5; what if `1,000` with no unit?
        if _COMMA_THOUSANDS_RE.match(num_str) and not unit_part:
            num_clean = num_str.replace(",", "")
        else:
            num_clean = num_str.replace(",", ".")
    else:
        num_clean = num_str

    try:
        qty = _normalize_decimal_Scale(Decimal(num_clean))
    except InvalidOperation:
        return ParseResult(
            None,
            {"raw": raw, "unit": None, "unit_norm": None},
            Diagnostic("INVALID_QTY_NUMBER", f"Invalid quantity number in {raw!r}", raw, severity="error"),
        )

    unit_display = normalize_display_string(unit_part) if unit_part.strip() else None
    unit_norm = unit_display.lower() if unit_display else None

    return ParseResult(
        qty,
        {
            "raw": raw,
            "unit": unit_display,
            "unit_norm": unit_norm,
            "is_negative": qty < 0,
        },
        None,
    )


# ---------------------------------------------------------------------------
# 3. Date & Timestamp Parser (SKILL.md §4.3)
# ---------------------------------------------------------------------------

_EXCEL_EPOCH_1900 = datetime.datetime(1899, 12, 30, 0, 0, 0)
_EXCEL_EPOCH_1904 = datetime.datetime(1904, 1, 1, 0, 0, 0)
_SERIAL_MIN = Decimal("40000")  # ~2009-07-06
_SERIAL_MAX = Decimal("50000")  # ~2036-11-21

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_ISO_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})[T\s]+(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$"
)
_DMY_DATE_RE = re.compile(
    r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)
_VN_TEXT_DATE_RE = re.compile(
    r"^(?:ngày\s+)?(\d{1,2})\s*(?:thg|tháng)\s*(\d{1,2})(?:[,\s]+|\s+năm\s+)(\d{4})"
    r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$",
    re.IGNORECASE,
)
_YEARLESS_DM_RE = re.compile(r"^(\d{1,2})[/\-](\d{1,2})$")
_NUMERIC_STR_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def _build_date_result(
    raw: Any,
    year: int,
    month: int,
    day: int,
    hour: int | None = None,
    minute: int | None = None,
    second: int | None = None,
    *,
    fmt: str,
    inferred_year: bool = False,
) -> ParseResult:
    try:
        dt_date = datetime.date(year, month, day)
    except ValueError as exc:
        return ParseResult(
            None,
            {"raw": raw, "format": "invalid_calendar_date"},
            Diagnostic("INVALID_CALENDAR_DATE", f"Invalid date {raw!r}: {exc}", raw, severity="error"),
        )

    date_iso = dt_date.isoformat()
    timestamp_iso: str | None = None
    out_hour: int | None = None

    if hour is not None:
        m_val = minute or 0
        s_val = second or 0
        try:
            dt_time = datetime.time(hour, m_val, s_val)
        except ValueError as exc:
            return ParseResult(
                None,
                {"raw": raw, "format": "invalid_time"},
                Diagnostic("INVALID_TIME", f"Invalid time in {raw!r}: {exc}", raw, severity="error"),
            )
        timestamp_iso = f"{date_iso} {dt_time.isoformat()}"
        out_hour = hour

    return ParseResult(
        date_iso,
        {
            "raw": raw,
            "date_iso": date_iso,
            "timestamp_iso": timestamp_iso,
            "hour": out_hour,
            "format": fmt,
            "inferred_year": inferred_year,
        },
        None,
    )


def _parse_excel_serial(
    serial_dec: Decimal,
    raw: Any,
    *,
    epoch_1904: bool = False,
    has_fraction_in_raw: bool = False,
) -> ParseResult:
    epoch = _EXCEL_EPOCH_1904 if epoch_1904 else _EXCEL_EPOCH_1900
    total_seconds = int(
        (serial_dec * Decimal(86400)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    dt = epoch + datetime.timedelta(seconds=total_seconds)
    date_iso = dt.date().isoformat()
    rem_seconds = total_seconds % 86400
    if rem_seconds != 0 or has_fraction_in_raw:
        timestamp_iso = dt.strftime("%Y-%m-%d %H:%M:%S")
        hour = dt.hour
        fmt = "excel_serial_datetime"
    else:
        timestamp_iso = None
        hour = None
        fmt = "excel_serial_date"

    return ParseResult(
        date_iso,
        {
            "raw": raw,
            "date_iso": date_iso,
            "timestamp_iso": timestamp_iso,
            "hour": hour,
            "format": fmt,
            "inferred_year": False,
        },
        None,
    )


def parse_date_value(
    raw: Any,
    *,
    context_year: int | None = None,
    is_excel_date: bool = False,
    epoch_1904: bool = False,
    allow_serial: bool = True,
) -> ParseResult:
    """Parse a date or timestamp cell into canonical YYYY-MM-DD and optional timestamp/hour.

    Supports (SKILL.md §4.3):
    - `YYYY-MM-DD` and `YYYY-MM-DD HH:MM:SS`
    - `DD/MM/YYYY` and `D/M/YYYY` (with optional time)
    - `d thg m yyyy` / `ngày d tháng m năm yyyy`
    - Yearless `d/m` (`1/3`) when `context_year` is supplied
    - Excel serial dates (`45748`, `45717.25`, `"45658"`) when `allow_serial=True`
      and within plausible serial range [40000, 50000] (or `is_excel_date=True`).
    """
    if raw is None:
        return ParseResult(
            None,
            {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "null"},
            None,
        )

    if isinstance(raw, bool):
        return ParseResult(
            None,
            {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "invalid_bool"},
            Diagnostic("INVALID_DATE_BOOL", "Boolean value in date field", raw, severity="error"),
        )

    if isinstance(raw, datetime.datetime):
        return _build_date_result(
            raw,
            raw.year,
            raw.month,
            raw.day,
            raw.hour,
            raw.minute,
            raw.second,
            fmt="datetime_obj",
        )

    if isinstance(raw, datetime.date):
        return _build_date_result(raw, raw.year, raw.month, raw.day, fmt="date_obj")

    if isinstance(raw, (int, float, Decimal)):
        if not allow_serial and not is_excel_date:
            return ParseResult(
                None,
                {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "serial_disallowed"},
                Diagnostic("SERIAL_DATE_DISALLOWED", f"Numeric value {raw!r} not allowed as date", raw),
            )
        serial_dec = Decimal(str(raw))
        if is_excel_date or (_SERIAL_MIN <= serial_dec <= _SERIAL_MAX):
            has_frac = (serial_dec != serial_dec.to_integral_value())
            return _parse_excel_serial(
                serial_dec,
                raw,
                epoch_1904=epoch_1904,
                has_fraction_in_raw=has_frac,
            )
        return ParseResult(
            None,
            {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "out_of_range_number"},
            Diagnostic(
                "OUT_OF_RANGE_DATE_SERIAL",
                f"Numeric value {raw!r} is outside plausible Excel date serial range",
                raw,
            ),
        )

    if not isinstance(raw, str):
        return ParseResult(
            None,
            {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "unsupported_type"},
            Diagnostic("UNSUPPORTED_DATE_TYPE", f"Unsupported type {type(raw).__name__}", raw, severity="error"),
        )

    return _parse_date_str(
        raw,
        context_year=context_year,
        is_excel_date=is_excel_date,
        epoch_1904=epoch_1904,
        allow_serial=allow_serial,
    )


@functools.lru_cache(maxsize=65536)
def _parse_date_str(
    raw: str,
    *,
    context_year: int | None = None,
    is_excel_date: bool = False,
    epoch_1904: bool = False,
    allow_serial: bool = True,
) -> ParseResult:
    s = unicodedata.normalize("NFC", raw).strip()
    if s.lower() in _EMPTY_PLACEHOLDERS:
        return ParseResult(
            None,
            {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "empty"},
            None,
        )

    # 1. ISO date YYYY-MM-DD
    m = _ISO_DATE_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return _build_date_result(raw, y, mo, d, fmt="iso_date")

    # 2. ISO datetime YYYY-MM-DD HH:MM[:SS]
    m = _ISO_DATETIME_RE.match(s)
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups()[:5])
        ss = int(m.group(6)) if m.group(6) is not None else 0
        return _build_date_result(raw, y, mo, d, hh, mm, ss, fmt="iso_datetime")

    # 3. DD/MM/YYYY or D/M/YYYY (with optional HH:MM[:SS])
    m = _DMY_DATE_RE.match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) is not None else None
        mm = int(m.group(5)) if m.group(5) is not None else None
        ss = int(m.group(6)) if m.group(6) is not None else None
        return _build_date_result(raw, y, mo, d, hh, mm, ss, fmt="dmy_slash")

    # 4. Vietnamese text date `1 thg 1 2025` / `ngày 1 tháng 1 năm 2025`
    m = _VN_TEXT_DATE_RE.match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) is not None else None
        mm = int(m.group(5)) if m.group(5) is not None else None
        ss = int(m.group(6)) if m.group(6) is not None else None
        return _build_date_result(raw, y, mo, d, hh, mm, ss, fmt="vn_text_date")

    # 5. Yearless `d/m` (`1/3`)
    m = _YEARLESS_DM_RE.match(s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if context_year is None:
            return ParseResult(
                None,
                {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "yearless_dm_missing_year"},
                Diagnostic(
                    "YEARLESS_DATE_MISSING_CONTEXT_YEAR",
                    f"Yearless date {raw!r} requires context_year from sheet/file metadata",
                    raw,
                    severity="warning",
                ),
            )
        return _build_date_result(raw, context_year, mo, d, fmt="yearless_dm", inferred_year=True)

    # 6. Numeric string Excel serial (`"45658"`, `"45717.25"`)
    if _NUMERIC_STR_RE.match(s):
        if not allow_serial and not is_excel_date:
            return ParseResult(
                None,
                {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "serial_disallowed"},
                Diagnostic("SERIAL_DATE_DISALLOWED", f"Numeric string {raw!r} not allowed as date", raw),
            )
        serial_dec = Decimal(s)
        if is_excel_date or (_SERIAL_MIN <= serial_dec <= _SERIAL_MAX):
            has_frac = (serial_dec != serial_dec.to_integral_value())
            return _parse_excel_serial(
                serial_dec,
                raw,
                epoch_1904=epoch_1904,
                has_fraction_in_raw=has_frac,
            )
        return ParseResult(
            None,
            {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "out_of_range_number"},
            Diagnostic(
                "OUT_OF_RANGE_DATE_SERIAL",
                f"Numeric string {raw!r} is outside plausible Excel date serial range",
                raw,
            ),
        )

    return ParseResult(
        None,
        {"raw": raw, "date_iso": None, "timestamp_iso": None, "hour": None, "format": "unparseable"},
        Diagnostic("UNPARSEABLE_DATE", f"Cannot parse date value: {raw!r}", raw, severity="error"),
    )


# ---------------------------------------------------------------------------
# 4. Customer Field Parser (SKILL.md §4.4)
# ---------------------------------------------------------------------------

_WALK_IN_NORMS = frozenset(
    {
        "",
        "-",
        "khách lẻ",
        "khach le",
        "khách vãng lai",
        "khach vang lai",
        "lẻ",
        "le",
        "kl",
    }
)

# Matches `<Name> - <Phone>` or `<Name> (<Phone>)` where Phone has 8-12 digits (allowing spaces/dots/dashes)
_CUST_DASH_PHONE_RE = re.compile(
    r"^(.*?)\s*[-–—]\s*(\+?\d[\d\s.\-]{6,14}\d)\s*$"
)
_CUST_PAREN_PHONE_RE = re.compile(
    r"^(.*?)\s*\(\s*(\+?\d[\d\s.\-]{6,14}\d)\s*\)\s*$"
)
_PHONE_FIRST_DASH_RE = re.compile(
    r"^\s*(\+?\d[\d\s.\-]{6,14}\d)\s*[-–—]\s*(.+?)\s*$"
)


def _clean_phone(raw_phone: str) -> str | None:
    digits = re.sub(r"[\s.\-]+", "", raw_phone.strip())
    only_digits = digits[1:] if digits.startswith("+") else digits
    if only_digits.isdigit() and 8 <= len(only_digits) <= 13:
        return digits
    return None


def parse_customer_field(raw: Any) -> ParseResult:
    """Normalize a customer cell into walk-in flag, display name, and stripped phone.

    Examples (SKILL.md §4.4):
    - `""`, `"Khách lẻ"`, `"khách lẻ"`, `"lẻ"`, `"kl"` -> is_walk_in=True, name="Khách lẻ"
    - `"Nguyễn Văn A - 0900 000 000"` -> name="Nguyễn Văn A", phone="0900000000"
    - `"Trần Thị B (0900000001)"` -> name="Trần Thị B", phone="0900000001"
    """
    if raw is None:
        return ParseResult(
            "Khách lẻ",
            {
                "raw": raw,
                "customer_raw": None,
                "customer_name": "Khách lẻ",
                "customer_name_norm": "khách lẻ",
                "customer_phone": None,
                "is_walk_in": True,
            },
            None,
        )

    s = normalize_display_string(str(raw))
    s_lower = s.lower()

    if s_lower in _WALK_IN_NORMS:
        return ParseResult(
            "Khách lẻ",
            {
                "raw": raw,
                "customer_raw": s if s else None,
                "customer_name": "Khách lẻ",
                "customer_name_norm": "khách lẻ",
                "customer_phone": None,
                "is_walk_in": True,
            },
            None,
        )

    # Check `<Name> (<Phone>)`
    m = _CUST_PAREN_PHONE_RE.match(s)
    if m:
        name_part, phone_part = m.groups()
        phone_clean = _clean_phone(phone_part)
        if phone_clean and name_part.strip():
            name_disp = normalize_display_string(name_part)
            return ParseResult(
                name_disp,
                {
                    "raw": raw,
                    "customer_raw": s,
                    "customer_name": name_disp,
                    "customer_name_norm": normalize_comparison_string(name_disp),
                    "customer_phone": phone_clean,
                    "is_walk_in": False,
                },
                None,
            )

    # Check `<Name> - <Phone>`
    m = _CUST_DASH_PHONE_RE.match(s)
    if m:
        name_part, phone_part = m.groups()
        phone_clean = _clean_phone(phone_part)
        if phone_clean and name_part.strip():
            name_disp = normalize_display_string(name_part)
            return ParseResult(
                name_disp,
                {
                    "raw": raw,
                    "customer_raw": s,
                    "customer_name": name_disp,
                    "customer_name_norm": normalize_comparison_string(name_disp),
                    "customer_phone": phone_clean,
                    "is_walk_in": False,
                },
                None,
            )

    # Check `<Phone> - <Name>`
    m = _PHONE_FIRST_DASH_RE.match(s)
    if m:
        phone_part, name_part = m.groups()
        phone_clean = _clean_phone(phone_part)
        if phone_clean and name_part.strip():
            name_disp = normalize_display_string(name_part)
            return ParseResult(
                name_disp,
                {
                    "raw": raw,
                    "customer_raw": s,
                    "customer_name": name_disp,
                    "customer_name_norm": normalize_comparison_string(name_disp),
                    "customer_phone": phone_clean,
                    "is_walk_in": False,
                },
                None,
            )

    return ParseResult(
        s,
        {
            "raw": raw,
            "customer_raw": s,
            "customer_name": s,
            "customer_name_norm": normalize_comparison_string(s),
            "customer_phone": None,
            "is_walk_in": False,
        },
        None,
    )


# ---------------------------------------------------------------------------
# 5. Inline Drink + Topping Parser (SKILL.md §4.5)
# ---------------------------------------------------------------------------

# Matches trailing ` + <k> <Topping> (<Price>)`
_TOPPING_TAIL_RE = re.compile(
    r"^(.*?)\s*\+\s*(\d+(?:[.,]\d+)?)\s+(.+?)\s*\(\s*([^()]+?)\s*\)\s*$"
)
_DRINK_SIZE_RE = re.compile(r"^(.*?)\s*\(\s*([SMLsml]|Size\s+[SMLsml]|Nhỏ|Vừa|Lớn)\s*\)\s*$")


def parse_inline_topping(
    raw_name: Any,
    line_qty: Any = 1,
    line_amount: Any = None,
) -> ParseResult:
    """Split an F&B inline drink+topping line into drink and topping components.

    Example (SKILL.md §4.5):
    `Trà sữa truyền thống (L) + 1 Trân châu trắng (7.000đ)` with line_amount=42000:
    - drink component: `Trà sữa truyền thống (L)`, qty=1, amount=35000
    - topping component: `Topping Trân châu trắng` (alias `Trân châu trắng`), qty=1, amount=7000
    - asserts `drink_amount + topping_amount == line_amount` when `line_amount` is provided.
    """
    if not isinstance(raw_name, str) or not raw_name.strip():
        return ParseResult(
            None,
            {"raw": raw_name, "has_topping": False, "components": []},
            Diagnostic("EMPTY_ITEM_NAME", "Empty item name", raw_name),
        )

    s = normalize_display_string(raw_name)
    qty_res = parse_quantity(line_qty)
    base_qty = qty_res.value if qty_res.value is not None else Decimal(1)

    amt_res = parse_money(line_amount) if line_amount is not None else ParseResult(None, {}, None)
    total_amount = amt_res.value

    # Extract one or more trailing `+ <k> <Topping> (<Price>)` segments from right to left
    toppings_reversed: list[dict[str, Any]] = []
    cur = s
    while True:
        m = _TOPPING_TAIL_RE.match(cur)
        if not m:
            break
        prefix, k_str, top_name_raw, top_price_raw = m.groups()
        price_res = parse_money(top_price_raw)
        if price_res.value is None or price_res.diagnostic is not None:
            break
        k_val = Decimal(k_str.replace(",", "."))
        top_clean = normalize_display_string(top_name_raw)
        if top_clean.lower().startswith("topping "):
            canonical_top = "Topping " + normalize_display_string(top_clean[8:])
            short_top = normalize_display_string(top_clean[8:])
        else:
            canonical_top = f"Topping {top_clean}"
            short_top = top_clean

        toppings_reversed.append(
            {
                "kind": "topping",
                "name": canonical_top,
                "short_name": short_top,
                "qty": _normalize_decimal_Scale(k_val),
                "amount": price_res.value,
                "raw_price": top_price_raw,
            }
        )
        cur = normalize_display_string(prefix)

    drink_name = cur
    m_size = _DRINK_SIZE_RE.match(drink_name)
    if m_size:
        drink_base_no_size = normalize_display_string(m_size.group(1))
        drink_size = normalize_display_string(m_size.group(2))
    else:
        drink_base_no_size = drink_name
        drink_size = None

    if not toppings_reversed:
        comp = {
            "kind": "item",
            "name": drink_name,
            "base_name_no_size": drink_base_no_size,
            "size": drink_size,
            "qty": base_qty,
            "amount": total_amount,
        }
        return ParseResult(
            [comp],
            {
                "raw": raw_name,
                "has_topping": False,
                "drink": comp,
                "toppings": [],
                "total_amount": total_amount,
            },
            None,
        )

    toppings = list(reversed(toppings_reversed))
    topping_sum = sum((t["amount"] for t in toppings), Decimal(0))

    diag: Diagnostic | None = None
    if total_amount is not None:
        drink_amount = total_amount - topping_sum
        if drink_amount < 0:
            diag = Diagnostic(
                "TOPPING_EXCEEDS_LINE_AMOUNT",
                f"Topping sum {topping_sum} exceeds line amount {total_amount} in {raw_name!r}",
                raw_name,
                severity="error",
            )
        elif drink_amount + topping_sum != total_amount:
            diag = Diagnostic(
                "TOPPING_RECONCILIATION_MISMATCH",
                f"Drink {drink_amount} + toppings {topping_sum} != {total_amount}",
                raw_name,
                severity="error",
            )
    else:
        drink_amount = None

    drink_comp = {
        "kind": "drink",
        "name": drink_name,
        "base_name_no_size": drink_base_no_size,
        "size": drink_size,
        "qty": base_qty,
        "amount": drink_amount,
    }
    components = [drink_comp] + toppings
    return ParseResult(
        components,
        {
            "raw": raw_name,
            "has_topping": True,
            "drink": drink_comp,
            "toppings": toppings,
            "topping_amount_sum": topping_sum,
            "drink_amount": drink_amount,
            "total_amount": total_amount,
        },
        diag,
    )


# ---------------------------------------------------------------------------
# 6. Order Status Normalizer (SKILL.md §4.6)
# ---------------------------------------------------------------------------

# Note: Vietnamese "hủy" can be written with tone on u ("hủy") or y ("huỷ").
# We canonicalize "huỷ" -> "hủy" and "hoàn tất" after NFC lowercase normalization.
_COMPLETED_EXACT = frozenset(
    {
        "đã hoàn thành",
        "da hoan thanh",
        "hoàn thành",
        "hoan thanh",
        "đã giao",
        "da giao",
        "đã giao hàng",
        "đã xong",
        "da xong",
        "xong",
        "đã lấy",
        "da lay",
        "thành công",
        "thanh cong",
        "ok",
        "đã thanh toán",
        "da thanh toan",
        "đã thu tiền",
        "da thu tien",
        "đã nhận",
        "hoàn tất",
        "hoan tat",
        "completed",
        "complete",
        "done",
        "paid",
        "delivered",
        "success",
    }
)

_SHIPPING_EXACT = frozenset(
    {
        "đang giao",
        "dang giao",
        "đang giao hàng",
        "dang giao hang",
        "đang vận chuyển",
        "dang van chuyen",
        "đang giao dịch",
        "dang giao dich",
        "đang gửi",
        "dang gui",
        "đang ship",
        "dang ship",
        "chưa giao",
        "chua giao",
        "chờ giao",
        "cho giao",
        "chờ lấy hàng",
        "đang xử lý",
        "dang xu ly",
        "shipping",
        "in transit",
        "delivering",
    }
)

_CANCELLED_EXACT = frozenset(
    {
        "đã hủy",
        "da huy",
        "đã hủy đơn",
        "da huy don",
        "hủy",
        "huy",
        "hủy đơn",
        "huy don",
        "đơn hủy",
        "khách hủy",
        "khach huy",
        "khách báo hủy",
        "bom hàng",
        "boom hàng",
        "không giao được",
        "khong giao duoc",
        "cancelled",
        "canceled",
        "cancel",
    }
)

_RETURNED_EXACT = frozenset(
    {
        "đã trả hàng",
        "da tra hang",
        "trả hàng",
        "tra hang",
        "đã hoàn hàng",
        "da hoan hang",
        "hàng trả về",
        "hang tra ve",
        "đổi trả",
        "doi tra",
        "hoàn trả",
        "hoan tra",
        "khách trả",
        "khach tra",
        "trả lại",
        "tra lai",
        "khách mang trả",
        "khach mang tra",
        "trả lại hàng",
        "tra lai hang",
        "khách trả lại",
        "khach tra lai",
        "đã hoàn",
        "da hoan",
        "hoàn đơn",
        "hoan don",
        "hoàn hàng",
        "returned",
        "return",
        "refunded",
    }
)

_NOTE_CANCEL_KEYWORDS = (
    "đã hủy",
    "hủy đơn",
    "khách hủy",
    "khách báo hủy",
    "báo hủy",
    "bom hàng",
    "boom hàng",
    "không giao được",
    "cancel",
)
_NOTE_RETURN_KEYWORDS = (
    "đã trả hàng",
    "trả hàng",
    "đã hoàn hàng",
    "hàng trả về",
    "đổi trả",
    "hoàn trả",
    "khách trả",
    "khách mang trả",
    "trả lại hàng",
    "khách trả lại",
    "hoàn đơn",
    "hoàn hàng",
)
_NOTE_SHIPPING_KEYWORDS = (
    "đang giao dịch",
    "đang giao hàng",
    "đang giao",
    "đang vận chuyển",
    "đang gửi",
    "đang ship",
    "chưa giao",
    "chờ giao",
)


def _normalize_status_token(s: str) -> str:
    nfc = unicodedata.normalize("NFC", s).strip().lower()
    nfc = re.sub(r"\s+", " ", nfc)
    # Canonicalize Vietnamese tone placement on "huỷ" -> "hủy" and "hoà" -> "hòa"
    nfc = nfc.replace("huỷ", "hủy")
    return nfc


def normalize_order_status(
    raw_status: Any,
    *,
    from_notes: bool = False,
    default_if_missing: str | None = None,
) -> ParseResult:
    """Map raw status or note text to canonical order status.

    Canonical statuses (SKILL.md §4.6):
    - `COMPLETED`: included in revenue
    - `SHIPPING`: included in revenue
    - `CANCELLED`: excluded from revenue
    - `RETURNED`: excluded from revenue
    - `UNKNOWN`: unrecognized non-empty status in a dedicated status column (emits diagnostic)
    """
    if raw_status is None or (isinstance(raw_status, str) and not raw_status.strip()):
        if from_notes:
            return ParseResult(
                default_if_missing or "COMPLETED",
                {"raw": raw_status, "from_notes": True, "revenue_eligible": True},
                None,
            )
        if default_if_missing is not None:
            rev_ok = default_if_missing in {"COMPLETED", "SHIPPING"}
            return ParseResult(
                default_if_missing,
                {"raw": raw_status, "from_notes": False, "revenue_eligible": rev_ok},
                None,
            )
        return ParseResult(
            None,
            {"raw": raw_status, "from_notes": False, "revenue_eligible": None},
            None,
        )

    if not isinstance(raw_status, str):
        return ParseResult(
            "UNKNOWN",
            {"raw": raw_status, "from_notes": from_notes, "revenue_eligible": None},
            Diagnostic("INVALID_STATUS_TYPE", f"Non-string status {raw_status!r}", raw_status, severity="error"),
        )

    norm = _normalize_status_token(raw_status)
    if norm in _EMPTY_PLACEHOLDERS:
        resolved = default_if_missing or ("COMPLETED" if from_notes else None)
        rev_ok = resolved in {"COMPLETED", "SHIPPING"} if resolved else None
        return ParseResult(
            resolved,
            {"raw": raw_status, "from_notes": from_notes, "revenue_eligible": rev_ok},
            None,
        )

    # Exact match check
    if norm in _COMPLETED_EXACT:
        return ParseResult(
            "COMPLETED",
            {"raw": raw_status, "norm": norm, "from_notes": from_notes, "revenue_eligible": True},
            None,
        )
    if norm in _SHIPPING_EXACT:
        return ParseResult(
            "SHIPPING",
            {"raw": raw_status, "norm": norm, "from_notes": from_notes, "revenue_eligible": True},
            None,
        )
    if norm in _CANCELLED_EXACT:
        return ParseResult(
            "CANCELLED",
            {"raw": raw_status, "norm": norm, "from_notes": from_notes, "revenue_eligible": False},
            None,
        )
    if norm in _RETURNED_EXACT:
        return ParseResult(
            "RETURNED",
            {"raw": raw_status, "norm": norm, "from_notes": from_notes, "revenue_eligible": False},
            None,
        )

    if from_notes:
        # Substring / phrase check for free-text notes (when no dedicated status column exists)
        if any(kw in norm for kw in _NOTE_CANCEL_KEYWORDS) or re.search(r"\bhủy\b", norm):
            return ParseResult(
                "CANCELLED",
                {"raw": raw_status, "norm": norm, "from_notes": True, "revenue_eligible": False},
                None,
            )
        if any(kw in norm for kw in _NOTE_RETURN_KEYWORDS):
            return ParseResult(
                "RETURNED",
                {"raw": raw_status, "norm": norm, "from_notes": True, "revenue_eligible": False},
                None,
            )
        if any(kw in norm for kw in _NOTE_SHIPPING_KEYWORDS):
            return ParseResult(
                "SHIPPING",
                {"raw": raw_status, "norm": norm, "from_notes": True, "revenue_eligible": True},
                None,
            )
        if any(kw in norm for kw in ("hoàn thành", "đã giao", "đã thanh toán", "hoàn tất", "thành công")):
            return ParseResult(
                "COMPLETED",
                {"raw": raw_status, "norm": norm, "from_notes": True, "revenue_eligible": True},
                None,
            )

        # Free-text note without cancellation/return/shipping marker -> active/completed order
        return ParseResult(
            default_if_missing or "COMPLETED",
            {"raw": raw_status, "norm": norm, "from_notes": True, "revenue_eligible": True},
            None,
        )

    # Dedicated status column with unrecognized text -> emit explicit diagnostic, never guess COMPLETED!
    return ParseResult(
        "UNKNOWN",
        {"raw": raw_status, "norm": norm, "from_notes": False, "revenue_eligible": None},
        Diagnostic(
            "UNKNOWN_ORDER_STATUS",
            f"Unrecognized order status {raw_status!r} (normalized: {norm!r})",
            raw_status,
            severity="error",
        ),
    )
