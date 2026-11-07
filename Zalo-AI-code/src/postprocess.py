"""Deterministic postprocessing, rounding, and submission formatting for DataMate."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import functools
import math
import re
from typing import Any
import unicodedata

VALID_SUBMISSION_TYPES = frozenset(
    {
        "number",
        "percent",
        "string",
        "date",
        "boolean",
        "list",
        "table",
        "unanswerable",
        "abstain",
    }
)

_DATE_FULL_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")
_DATE_MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_WHITESPACE_RE = re.compile(r"\s+")


def is_valid_json_number(val: Any) -> bool:
    """Return True if val is a finite int or float (excluding bool)."""
    if isinstance(val, bool):
        return False
    if isinstance(val, int):
        return True
    if isinstance(val, float):
        return math.isfinite(val)
    return False


def to_decimal(val: Decimal | int | float | str) -> Decimal:
    """Convert a numeric value to an exact Decimal, rejecting bool/NaN/Inf."""
    if isinstance(val, bool):
        raise TypeError("Boolean is not a valid numeric value")
    if isinstance(val, Decimal):
        if not val.is_finite():
            raise ValueError("Non-finite Decimal is not allowed")
        return val
    if isinstance(val, int):
        return Decimal(val)
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError("Non-finite float is not allowed")
        return Decimal(str(val))
    if isinstance(val, str):
        s = val.strip()
        if not s:
            raise ValueError("Empty string is not a valid Decimal")
        try:
            d = Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"Invalid numeric string: {val!r}") from exc
        if not d.is_finite():
            raise ValueError("Non-finite Decimal string is not allowed")
        return d
    raise TypeError(f"Unsupported numeric type: {type(val).__name__}")


def round_money_vnd(val: Decimal | int | float | str) -> int:
    """Round a monetary value (including monetary averages) half-up to integer VND."""
    d = to_decimal(val)
    rounded = d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(rounded)


def round_decimal_2(val: Decimal | int | float | str) -> int | float:
    """Round a non-monetary average or percentage half-up to 2 decimal places."""
    d = to_decimal(val)
    rounded = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral_value():
        return int(rounded)
    return float(rounded)


def format_number(
    val: Decimal | int | float | str,
    measure_kind: str = "general",
) -> int | float:
    """Format a number answer according to Rulebook §9 rounding rules.

    Args:
        val: Raw numeric value.
        measure_kind: One of:
            - "money", "monetary_average", "vnd": integer VND with ROUND_HALF_UP.
            - "count", "integer": exact integer (rounds half-up if Decimal/float is integral).
            - "average", "ratio", "decimal_2": 2 decimal places with ROUND_HALF_UP.
            - "general": preserves exact integer if integral, else 2 decimal places.
    """
    kind = measure_kind.lower().strip()
    if kind in {"money", "monetary_average", "aov", "vnd"}:
        return round_money_vnd(val)
    if kind in {"count", "integer"}:
        d = to_decimal(val)
        rounded = d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(rounded)
    if kind in {"average", "average_other", "ratio", "ratio_percent", "decimal_2"}:
        return round_decimal_2(val)
    if kind in {"general", "quantity", "scalar_other"}:
        d = to_decimal(val)
        if d == d.to_integral_value():
            return int(d)
        if kind == "quantity":
            return float(d.normalize())
        return round_decimal_2(d)
    raise ValueError(f"Unknown measure_kind: {measure_kind!r}")


def format_percent(val: Decimal | int | float | str) -> int | float:
    """Format a percentage answer in percentage points (e.g. 25.5 for 25.5%).

    Does not clamp negative values or values exceeding 100.
    """
    return round_decimal_2(val)


@functools.lru_cache(maxsize=65536)
def normalize_display_string(val: str) -> str:
    """NFC-normalize and trim a string for output while preserving diacritics."""
    if not isinstance(val, str):
        raise TypeError(f"Expected str, got {type(val).__name__}")
    nfc = unicodedata.normalize("NFC", val)
    return _WHITESPACE_RE.sub(" ", nfc).strip()


@functools.lru_cache(maxsize=65536)
def normalize_comparison_string(val: str) -> str:
    """Normalize a string for Rulebook §9 comparison.

    Case-insensitive, ignores redundant whitespace and punctuation, and
    preserves Vietnamese diacritics in NFC form.
    """
    if not isinstance(val, str):
        raise TypeError(f"Expected str, got {type(val).__name__}")
    nfc = unicodedata.normalize("NFC", val).lower()
    # Strip Unicode punctuation characters while preserving letters, numbers, and diacritics.
    chars: list[str] = []
    for ch in nfc:
        cat = unicodedata.category(ch)
        if cat.startswith("P"):
            chars.append(" ")
        else:
            chars.append(ch)
    cleaned = "".join(chars)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


@functools.lru_cache(maxsize=65536)
def normalize_comparison_string_compact(val: str) -> str:
    """Secondary punctuation-stripped comparison key without spaces inside codes."""
    if not isinstance(val, str):
        raise TypeError(f"Expected str, got {type(val).__name__}")
    nfc = unicodedata.normalize("NFC", val).lower()
    chars = [ch for ch in nfc if not unicodedata.category(ch).startswith("P")]
    return _WHITESPACE_RE.sub(" ", "".join(chars)).strip()


def is_valid_date_string(val: Any) -> bool:
    """Return True if val is a valid YYYY-MM-DD or YYYY-MM string."""
    if not isinstance(val, str):
        return False
    s = val.strip()
    return bool(_DATE_FULL_RE.match(s) or _DATE_MONTH_RE.match(s))


def normalize_nested_value(val: Any, measure_kind: str = "general") -> Any:
    """Normalize strings (NFC + trim) and numbers inside list/table answers."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float, Decimal)):
        return format_number(val, measure_kind=measure_kind)
    if isinstance(val, str):
        return normalize_display_string(val)
    if isinstance(val, list):
        return [normalize_nested_value(item, measure_kind=measure_kind) for item in val]
    if isinstance(val, dict):
        return {
            normalize_display_string(str(k)): normalize_nested_value(v, measure_kind=measure_kind)
            for k, v in val.items()
        }
    return val


def format_submission_record(
    qid: str,
    ans_type: str,
    answer: Any = None,
    *,
    measure_kind: str = "general",
    reason: str | None = None,
) -> dict[str, Any]:
    """Build and validate a single JSONL submission record."""
    if not isinstance(qid, str) or not qid.strip():
        raise ValueError("id must be a non-empty string")
    qid_clean = qid.strip()

    if ans_type not in VALID_SUBMISSION_TYPES:
        raise ValueError(f"Invalid submission type: {ans_type!r}")

    if ans_type == "abstain":
        return {"id": qid_clean, "type": "abstain"}

    if ans_type == "unanswerable":
        rec: dict[str, Any] = {"id": qid_clean, "type": "unanswerable"}
        if reason is not None:
            if not isinstance(reason, str):
                raise TypeError("unanswerable reason must be a string")
            rec["reason"] = normalize_display_string(reason)
        return rec

    if ans_type == "number":
        formatted = format_number(answer, measure_kind=measure_kind)
        return {"id": qid_clean, "type": "number", "answer": formatted}

    if ans_type == "percent":
        formatted = format_percent(answer)
        return {"id": qid_clean, "type": "percent", "answer": formatted}

    if ans_type == "string":
        if not isinstance(answer, str):
            raise TypeError(f"string answer must be str, got {type(answer).__name__}")
        return {"id": qid_clean, "type": "string", "answer": normalize_display_string(answer)}

    if ans_type == "date":
        if not isinstance(answer, str):
            raise TypeError(f"date answer must be str, got {type(answer).__name__}")
        s = normalize_display_string(answer)
        if not is_valid_date_string(s):
            raise ValueError(f"Invalid date answer format (expected YYYY-MM-DD or YYYY-MM): {s!r}")
        return {"id": qid_clean, "type": "date", "answer": s}

    if ans_type == "boolean":
        if not isinstance(answer, bool):
            raise TypeError(f"boolean answer must be bool, got {type(answer).__name__}")
        return {"id": qid_clean, "type": "boolean", "answer": answer}

    if ans_type == "list":
        if not isinstance(answer, list):
            raise TypeError(f"list answer must be list, got {type(answer).__name__}")
        norm_list = [normalize_nested_value(el, measure_kind=measure_kind) for el in answer]
        return {"id": qid_clean, "type": "list", "answer": norm_list}

    if ans_type == "table":
        if not isinstance(answer, dict):
            raise TypeError(f"table answer must be dict, got {type(answer).__name__}")
        cols = answer.get("columns")
        rows = answer.get("rows")
        if not isinstance(cols, list) or not cols or not all(isinstance(c, str) for c in cols):
            raise ValueError("table.columns must be a non-empty list of strings")
        if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
            raise ValueError("table.rows must be a list of lists")
        norm_cols = [normalize_display_string(c) for c in cols]
        norm_rows: list[list[Any]] = []
        for r in rows:
            if len(r) != len(norm_cols):
                raise ValueError(
                    f"table row length {len(r)} does not match columns length {len(norm_cols)}"
                )
            norm_rows.append([normalize_nested_value(cell, measure_kind=measure_kind) for cell in r])
        return {
            "id": qid_clean,
            "type": "table",
            "answer": {"columns": norm_cols, "rows": norm_rows},
        }

    raise ValueError(f"Unhandled type: {ans_type!r}")


def validate_submission_record(rec: Any) -> tuple[bool, str]:
    """Validate whether a parsed JSON object is a well-formed submission record.

    Returns:
        (is_valid, reason_if_invalid)
    """
    if not isinstance(rec, dict):
        return False, "record is not a JSON object"
    qid = rec.get("id")
    if not isinstance(qid, str) or not qid.strip():
        return False, "missing or invalid id"
    ans_type = rec.get("type")
    if ans_type not in VALID_SUBMISSION_TYPES:
        return False, f"invalid type: {ans_type!r}"

    if ans_type == "abstain":
        return True, "ok"

    if ans_type == "unanswerable":
        if "reason" in rec and rec["reason"] is not None and not isinstance(rec["reason"], str):
            return False, "unanswerable reason must be a string when present"
        return True, "ok"

    if "answer" not in rec:
        return False, f"missing answer field for type {ans_type!r}"

    ans = rec["answer"]
    if ans_type in {"number", "percent"}:
        if not is_valid_json_number(ans):
            return False, f"{ans_type} answer must be a finite JSON number"
        return True, "ok"

    if ans_type == "string":
        if not isinstance(ans, str):
            return False, "string answer must be a string"
        return True, "ok"

    if ans_type == "date":
        if not is_valid_date_string(ans):
            return False, "date answer must be YYYY-MM-DD or YYYY-MM"
        return True, "ok"

    if ans_type == "boolean":
        if not isinstance(ans, bool):
            return False, "boolean answer must be a JSON boolean"
        return True, "ok"

    if ans_type == "list":
        if not isinstance(ans, list):
            return False, "list answer must be a JSON array"
        for item in ans:
            if isinstance(item, float) and not math.isfinite(item):
                return False, "list contains non-finite float"
        return True, "ok"

    if ans_type == "table":
        if not isinstance(ans, dict):
            return False, "table answer must be a JSON object"
        cols = ans.get("columns")
        rows = ans.get("rows")
        if not isinstance(cols, list) or not cols or not all(isinstance(c, str) for c in cols):
            return False, "table.columns must be a non-empty list of strings"
        if not isinstance(rows, list):
            return False, "table.rows must be a list of row arrays"
        for r in rows:
            if not isinstance(r, list) or len(r) != len(cols):
                return False, "table row must be a list matching columns length"
            for cell in r:
                if isinstance(cell, float) and not math.isfinite(cell):
                    return False, "table cell contains non-finite float"
        return True, "ok"

    return False, f"unhandled type {ans_type!r}"


def write_submission_jsonl(
    output_path: str | Any,
    records: list[dict[str, Any]],
    *,
    expected_ids: list[str] | None = None,
    fill_missing_with_abstain: bool = True,
) -> None:
    """Atomically write validated submission records to a JSONL file.

    Deduplicates by `id` (keeping the first valid record per ID) and optionally fills
    any missing `expected_ids` with `{"id": qid, "type": "abstain"}`.
    """
    import json
    import os
    from pathlib import Path

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    tmp_p = out_p.with_suffix(out_p.suffix + ".tmp")

    by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for rec in records:
        ok, err = validate_submission_record(rec)
        if not ok:
            raise ValueError(f"Invalid submission record {rec!r}: {err}")
        qid = str(rec["id"]).strip()
        if qid not in by_id:
            by_id[qid] = rec
            ordered_ids.append(qid)

    final_ids = list(expected_ids) if expected_ids is not None else ordered_ids
    lines: list[str] = []
    seen: set[str] = set()
    for qid in final_ids:
        if qid in seen:
            continue
        seen.add(qid)
        if qid in by_id:
            lines.append(json.dumps(by_id[qid], ensure_ascii=False))
        elif fill_missing_with_abstain:
            lines.append(json.dumps({"id": qid, "type": "abstain"}, ensure_ascii=False))
        else:
            raise ValueError(f"Missing submission record for expected id {qid!r}")

    with tmp_p.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    tmp_p.replace(out_p)

