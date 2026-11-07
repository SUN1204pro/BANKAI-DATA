"""Propose column roles from values when a README is silent.

Results are candidates, not facts. A pattern in values can nominate a role, but correlation does not establish
a unit conversion or a date definition: loyalty points can equal quantity times a constant, and a price can
parse as an Excel date. So each role below requires evidence independent of the pattern that nominated it, and
anything unsettled stays unresolved for the health checks in `src/semantic.py` to block.

Only unglossed columns with role `other` can change. A README definition always wins.
"""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any

from src.normalizers import normalize_order_status, parse_date_value, parse_money, parse_quantity
from src.text_norm import fold_key

SAMPLE_LIMIT = 500
_ORDER_STATES = {"COMPLETED", "SHIPPING", "CANCELLED", "RETURNED"}
_CODE_RE = re.compile(r"^[A-Za-z]{1,6}[-_]?\d+$")
_DATE_SHAPE_RE = re.compile(r"\d[/\-.]\d|thg|tháng", re.IGNORECASE)
_CURRENCY_RE = re.compile(r"(?:đ|₫|vnd|vnđ|k|tr|triệu)\s*$|^\d{1,3}(?:[.,]\d{3})+$", re.IGNORECASE)
_UNIT_CELL_RE = re.compile(r"^\s*[+-]?\d+(?:[.,]\d+)?\s*[^\d\s.,+-]")
_IDENTIFIER_RE = re.compile(r"^0\d{7,}$")
_ARITHMETIC_ROLES = ("qty", "unit_price", "line_amount")


def _text(cell: Any) -> str:
    value = getattr(cell, "value", cell)
    return "" if value is None else str(value).strip()


def _is_date_formatted(cell: Any) -> bool:
    return bool(getattr(cell, "is_date_formatted", False))


def sample_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """Evenly spaced rows across the whole table, so a proposal is not based only on its start."""
    if len(rows) <= SAMPLE_LIMIT:
        return rows
    step = len(rows) / SAMPLE_LIMIT
    return [rows[int(i * step)] for i in range(SAMPLE_LIMIT)]


def _column(rows: list[list[Any]], index: int) -> list[Any]:
    return [r[index] for r in rows if index < len(r) and _text(r[index]) != ""]


def _is_date_column(cells: list[Any], context_year: int | None) -> bool:
    if not cells:
        return False
    for cell in cells:
        text = _text(cell)
        if _is_date_formatted(cell):
            result = parse_date_value(text, is_excel_date=True)
        elif _DATE_SHAPE_RE.search(text):
            # An unformatted bare number is never date evidence: a price can sit in the serial range.
            result = parse_date_value(text, context_year=context_year, allow_serial=False)
        else:
            return False
        if result.value is None or result.diagnostic is not None:
            return False
    return True


def _is_status_column(cells: list[Any]) -> bool:
    values = [_text(c) for c in cells]
    if len(values) < 2 or len(set(values)) > 30:
        return False
    known = sum(1 for v in values if normalize_order_status(v, from_notes=False).value in _ORDER_STATES)
    return known * 10 >= len(values) * 9


def _number_of(text: str) -> Decimal | None:
    if _IDENTIFIER_RE.match(text):
        return None
    if _UNIT_CELL_RE.match(text) and not _CURRENCY_RE.search(text):
        value = parse_quantity(text).value
        return Decimal(value) if value is not None else None
    parsed = parse_money(text)
    return parsed.value if parsed.value is not None and parsed.diagnostic is None else None


def _anchor_of(cells: list[Any]) -> str | None:
    """`unit` when every cell carries a unit word, `money` when every cell carries a currency mark."""
    texts = [_text(c) for c in cells]
    if not texts:
        return None
    if all(_CURRENCY_RE.search(t) for t in texts):
        return "money"
    if all(_UNIT_CELL_RE.match(t) for t in texts):
        return "unit"
    return None


def _product_holds(rows: list[list[Any]], a: int, b: int, c: int) -> bool:
    """`a × b = c` exactly on every row where all three cells are present, and at least once."""
    checked = 0
    for r in rows:
        if max(a, b, c) >= len(r):
            continue
        ta, tb, tc = _text(r[a]), _text(r[b]), _text(r[c])
        if not (ta and tb and tc):
            continue
        na, nb, nc = _number_of(ta), _number_of(tb), _number_of(tc)
        if na is None or nb is None or nc is None or na * nb != nc:
            return False
        checked += 1
    return checked > 0


def _arithmetic_candidates(rows: list[list[Any]], roles: list[str], open_columns: set[int]) -> tuple[dict[int, str], str | None]:
    """Find qty × price = amount. Arithmetic alone cannot name the columns: an anchor must distinguish them."""
    numeric = [i for i in range(len(roles)) if (i in open_columns or roles[i] in _ARITHMETIC_ROLES)]
    numeric = [i for i in numeric if _column(rows, i) and all(_number_of(_text(c)) is not None for c in _column(rows, i))]
    found: list[tuple[int, int, int]] = []
    for c in numeric:
        for a in numeric:
            for b in numeric:
                if a < b and c not in (a, b) and _product_holds(rows, a, b, c):
                    found.append((a, b, c))
    if not found:
        return {}, None
    if len(found) > 1:
        return {}, "several columns satisfy quantity × price = amount"
    a, b, c = found[0]
    anchors = {i: ("money" if roles[i] in {"unit_price", "line_amount"} else "unit" if roles[i] == "qty" else _anchor_of(_column(rows, i))) for i in (a, b, c)}
    if anchors[a] == "unit" or anchors[b] == "money" and anchors[a] != "money":
        qty, price = a, b
    elif anchors[b] == "unit" or anchors[a] == "money" and anchors[b] != "money":
        qty, price = b, a
    else:
        qty, price = None, None
    out: dict[int, str] = {}
    if anchors[c] == "money" or qty is not None:
        out[c] = "line_amount"
    if qty is not None:
        out[qty] = "qty"
        out[price] = "unit_price"
    message = None if qty is not None else "quantity and price are interchangeable"
    return {i: r for i, r in out.items() if i in open_columns}, message


def _is_contiguous_code_column(rows: list[list[Any]], index: int) -> bool:
    """Order-shaped: code values whose rows sit together, as one order's lines do. Blanks continue a run."""
    seen: set[str] = set()
    current = None
    values = 0
    for r in rows:
        text = _text(r[index]) if index < len(r) else ""
        if not text or text == current:
            continue
        if not _CODE_RE.match(text) or text in seen:
            return False
        seen.add(text)
        current = text
        values += 1
    return values > 0


def infer_roles_from_values(
    headers: list[str],
    roles: list[str],
    glosses: list[str | None],
    rows: list[list[Any]],
    context_year: int | None = None,
) -> tuple[list[str], list[str]]:
    """Return candidate roles and a reason per decision, without changing the caller's list."""
    out = list(roles)
    reasons: list[str] = []
    sampled = sample_rows(rows)
    open_columns = {i for i in range(len(headers)) if roles[i] == "other" and not glosses[i]}

    def accept(index: int, role: str, why: str) -> None:
        out[index] = role
        open_columns.discard(index)
        reasons.append(f"{headers[index]} -> {role}: {why}")

    # Dates and statuses first: a date-shaped column can look numeric to later checks.
    if "date" not in out:
        dates = [i for i in sorted(open_columns) if _is_date_column(_column(sampled, i), context_year)]
        if len(dates) == 1:
            accept(dates[0], "date", "all sampled values are date-shaped")
        elif len(dates) > 1:
            reasons.append("UNRESOLVED date: " + ", ".join(headers[i] for i in dates) + " are all date-shaped")
            open_columns -= set(dates)
    if "status" not in out:
        statuses = [i for i in sorted(open_columns) if _is_status_column(_column(sampled, i))]
        if len(statuses) == 1:
            accept(statuses[0], "status", "at least 90% of values are known order states")
        elif len(statuses) > 1:
            reasons.append("UNRESOLVED status: " + ", ".join(headers[i] for i in statuses))

    assigned, problem = _arithmetic_candidates(sampled, out, open_columns)
    for index, role in assigned.items():
        if role not in out:
            accept(index, role, "exact quantity × price = amount with an anchoring unit or currency mark")
    if problem:
        reasons.append(f"UNRESOLVED amount: {problem}")

    has_date = "date" in out
    has_money = bool({"line_amount", "order_total"} & set(out))
    if "order_id" not in out and has_date and has_money:
        codes = [i for i in sorted(open_columns) if _is_contiguous_code_column(sampled, i)]
        if len(codes) == 1:
            accept(codes[0], "order_id", "code values in contiguous runs, in a table with a date and money")
        elif len(codes) > 1:
            reasons.append("UNRESOLVED order_id: " + ", ".join(headers[i] for i in codes))
    return out, reasons


def arithmetic_holds_on_all_rows(roles: list[str], rows: list[list[Any]]) -> bool:
    """A sample can miss a contradiction, so an inferred amount must hold on every row of the table."""
    if not all(role in roles for role in _ARITHMETIC_ROLES):
        return True
    qty, price, amount = (roles.index(role) for role in _ARITHMETIC_ROLES)
    return _product_holds(rows, qty, price, amount)


def match_catalog_column(
    rows: list[list[Any]], roles: list[str], glosses: list[str | None], values: set[str], minimum_ratio: float = 0.95
) -> int | None:
    """The single open column whose distinct values are nearly all in a catalog list, or None if unsettled."""
    if not values:
        return None
    hits = []
    for i in range(len(roles)):
        if roles[i] != "other" or glosses[i]:
            continue
        distinct = {fold_key(_text(c)) for c in _column(rows, i)}
        if distinct and sum(1 for v in distinct if v in values) >= len(distinct) * minimum_ratio:
            hits.append(i)
    return hits[0] if len(hits) == 1 else None


def find_documented_base_qty(
    rows: list[list[Any]], roles: list[str], glosses: list[str | None], factor_of: Any
) -> int | None:
    """A numeric column equal to quantity × documented factor on every row, with a factor other than 1 used.

    `factor_of(item, unit)` must return a factor only from a conversion rule the source documents. A second
    correlated number is not enough: without that rule, loyalty points would pass this test too.
    """
    if not all(r in roles for r in ("qty", "unit", "item_name")):
        return None
    qty, unit, item = roles.index("qty"), roles.index("unit"), roles.index("item_name")
    matches = []
    for i in range(len(roles)):
        if roles[i] != "other" or glosses[i]:
            continue
        used_real_factor = False
        ok = bool(_column(rows, i))
        for r in rows:
            if max(i, qty, unit, item) >= len(r) or not _text(r[i]):
                continue
            value, amount = _number_of(_text(r[i])), _number_of(_text(r[qty]))
            factor = factor_of(_text(r[item]), _text(r[unit]))
            if value is None or amount is None or factor is None or value != amount * factor:
                ok = False
                break
            if factor != 1:
                used_real_factor = True
        if ok and used_real_factor:
            matches.append(i)
    return matches[0] if len(matches) == 1 else None
