"""Turn SQL results into submission answers (rulebook section 9: one exact format per type).

  number      JSON number; integers stay integers (money = whole VND); other values 2 decimals
  percent     JSON number in percentage points: 12.4 means 12.4 %; 2 decimals
  date        "YYYY-MM-DD", or "YYYY-MM" when the question is monthly
  boolean     true / false
  string      the name exactly as in the data
  list        JSON array in order; [3, 10] for big-unit/loose-unit stock
  table       {"columns": [...], "rows": [[...], ...]}
  unanswerable  {"id", "type": "unanswerable", "reason": "..."}
  abstain       {"id", "type": "abstain"}

Scoring is +1 / -1 / 0 (abstain), so a wrong format is as bad as a wrong value.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .normalize import norm
from .parsing import parse_date

VALID_TYPES = {"number", "percent", "string", "date", "boolean", "list", "table"}
SCALAR_TYPES = {"number", "percent", "string", "date", "boolean"}
_MONTH = re.compile(r"^\d{4}-\d{2}$")


def round2(x: float) -> int | float:
    """Round half up to 2 decimals; whole values come back as int."""
    d = Decimal(str(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(d) if d == d.to_integral_value() else float(d)


def py(v):
    """sqlite value -> JSON-friendly python value (numbers rounded as in the rulebook)."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return None if v != v else round2(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def infer_type(cols: list[str], rows: list[tuple]) -> str:
    if len(rows) == 1 and len(cols) >= 1:
        v = rows[0][0]
        return "number" if isinstance(v, (int, float)) and not isinstance(v, bool) else "string"
    return "list" if len(cols) == 1 else "table"


def _to_bool(v) -> bool:
    if isinstance(v, (int, float)):
        return bool(v)
    return norm(v) in {"1", "true", "yes", "co", "dung", "y", "t"}


def format_answer(answer_type: str | None, cols: list[str], rows: list[tuple]) -> tuple[str, object]:
    t = norm(answer_type) if answer_type else ""
    if t not in VALID_TYPES:
        t = infer_type(cols, rows)
    if t in SCALAR_TYPES:
        if not rows:
            raise ValueError("no rows for a scalar answer")
        v = rows[0][0]
        if v is None:
            raise ValueError("NULL scalar answer")
        if t in ("number", "percent"):
            if isinstance(v, str):
                raise ValueError(f"expected a number, got text {v!r}")
            return t, py(float(v)) if isinstance(v, float) else v
        if t == "boolean":
            return t, _to_bool(v)
        if t == "date":
            s = str(v).strip()
            if _MONTH.match(s):
                return t, s
            d = parse_date(s)
            return t, d.isoformat() if d else s
        return t, str(v).strip()
    if t == "list":
        if len(rows) == 1 and len(cols) > 1:  # e.g. [big units, loose base units]
            return t, [py(x) for x in rows[0]]
        return t, [py(r[0]) for r in rows]
    return "table", {"columns": list(cols), "rows": [[py(x) for x in r] for r in rows]}


def make_line(qid: str, answer_type: str, answer=None, reason: str | None = None) -> dict:
    """The exact JSON line written to submission.jsonl."""
    if answer_type == "abstain":
        return {"id": qid, "type": "abstain"}
    if answer_type == "unanswerable":
        line = {"id": qid, "type": "unanswerable"}
        if isinstance(reason, str) and reason.strip():
            line["reason"] = reason.strip()
        return line
    return {"id": qid, "type": answer_type, "answer": answer}


def _cell(x) -> str:
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, (int, float)):
        return f"{float(x):.4f}"
    return norm(x)


def canon_key(answer_type: str, answer) -> str:
    """Stable string used to vote across samples (numbers rounded, strings accent-free, list order kept)."""
    t, a = answer_type, answer
    if t in ("number", "percent"):
        return f"n:{float(a):.4f}"
    if t == "boolean":
        return f"b:{bool(a)}"
    if t in ("string", "date"):
        return f"s:{norm(a)}"
    if t == "list":
        return "l:" + "|".join(_cell(x) for x in a)
    if t == "table":
        return "t:" + "|".join(sorted(",".join(_cell(c) for c in r) for r in a["rows"]))
    return f"?:{a}"
