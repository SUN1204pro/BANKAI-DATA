"""Rulebook metric calculators over the canonical `sem_*` tables.

Each function encodes one rulebook clause and returns an `Answer`. Money is computed from integers and
rounded with Decimal half-up. A concept the shop does not record raises `Unanswerable`; a state the code
cannot interpret safely raises `Abstain`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import sqlite3
from typing import Any

from src.grounder import DateWindow
from src.text_norm import fold_key


class Unanswerable(Exception):
    """Rulebook §7: released data does not record what the question needs."""


class Abstain(Exception):
    """The answer cannot be established safely."""


@dataclass
class Answer:
    type: str
    value: Any
    measure_kind: str = "general"


@dataclass
class Filters:
    item_keys: list[str] = field(default_factory=list)
    category: str | None = None
    customer_key: str | None = None
    staff: str | None = None
    group: str | None = None
    channel: str | None = None
    method: str | None = None
    rx: bool | None = None
    ingredient: str | None = None
    shift: tuple[str, str] | None = None
    weekday: int | None = None
    size_suffix: str | None = None
    brand: str | None = None
    catalog_size: str | None = None

    @property
    def needs_lines(self) -> bool:
        return bool(self.brand or self.catalog_size or self.item_keys or self.category or self.rx is not None or self.ingredient or self.size_suffix)


CHANNEL_CLASSES = {
    "store": {"store", "tai tiem", "tai quay", "tai cua hang", "cua hang", "offline", "truc tiep"},
    "phone": {"phone", "dien thoai"},
    "zalo": {"zalo", "zalo oa", "zalo_oa"},
    "marketplace": {"san tmdt", "san", "shopee", "lazada", "tiki"},
}
METHOD_CLASSES = {
    "cash": {"cash", "tien mat"},
    "transfer": {"transfer", "chuyen khoan", "ck"},
    "ewallet": {"ewallet", "vi dien tu", "momo"},
    "cod": {"cod", "thu ho cod", "thu ho"},
    "debt": {"debt", "ghi no", "no"},
}
SHIFTS = {"sang": ("06:00:00", "13:59:59"), "chieu": ("14:00:00", "21:59:59")}


def money(val: Decimal | int | float) -> int:
    return int(Decimal(str(val)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def ratio_pct(num: Decimal | int, den: Decimal | int) -> Decimal:
    return Decimal(num) * 100 / Decimal(den)


def clean_qty(val: float | None) -> int | float:
    if val is None:
        return 0
    rounded = round(val, 6)
    return int(rounded) if rounded == int(rounded) else rounded


# Attributes a coded column could hold, unlike concepts with their own whole tables.
ATTRIBUTE_CAPABILITIES = frozenset({"customer", "customer_code", "customer_group", "staff", "channel", "payment_method", "rx", "ingredient", "category", "lines", "order_time"})
ORDER_DEPENDENCIES = ("orders", "status")
REVENUE_DEPENDENCIES = ("orders", "status", "amount")


class ShopData:
    """One business's semantic tables plus coverage and capability checks."""

    def __init__(self, conn: sqlite3.Connection, as_of: str) -> None:
        self.conn = conn
        self.as_of = as_of
        self.meta = dict(conn.execute("SELECT key, value FROM sem_meta").fetchall())
        self.start = self.meta.get("coverage_start")
        self.end = self.meta.get("coverage_end")

    def cap(self, name: str) -> bool:
        return self.meta.get(f"cap_{name}") == "1"

    def require_healthy_global(self) -> None:
        problem = self.meta.get("health_problem_global")
        if problem:
            raise Abstain(problem)

    def require_healthy(self, *capabilities: str) -> None:
        """Raise when evidence for a capability this calculation depends on is unresolved."""
        for name in capabilities:
            problem = self.meta.get(f"health_problem_{name}")
            if problem:
                raise Abstain(f"{name}: {problem}")

    def attributes_unresolved(self) -> bool:
        return bool(self.meta.get("health_problem_attributes"))

    def require(self, *names: str) -> None:
        """Absence is `unanswerable` only when proven; an unresolved mapping is not proof of absence."""
        for n in names:
            self.require_healthy(n)
            if not self.cap(n):
                if n in ATTRIBUTE_CAPABILITIES and self.attributes_unresolved():
                    raise Abstain(f"{n}: {self.meta['health_problem_attributes']}")
                raise Unanswerable(f"Shop không ghi thông tin: {n}")

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return self.conn.execute(sql, params).fetchone()[0]

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self.conn.execute(sql, params).fetchall()

    def clip(self, win: DateWindow, *, what: str = "Kỳ hỏi", source: str = "orders") -> tuple[str, str]:
        """Clip a flow period to today and the coverage of the source asked about (§7). Coverage only."""
        if source == "purchases":
            first = self.meta.get("purchases_start")
            last = self.end or self.meta.get("purchases_end")
        else:
            first, last = self.start, self.end
        if not first or not last:
            raise Abstain(f"No dated {source} records")
        hi = min(last, self.as_of)
        end = min(win.end_date, hi)
        start = max(win.start_date, first)
        if win.start_date > hi or win.end_date < first or start > end:
            raise Unanswerable(f"{what} {win.start_date}..{win.end_date} nằm ngoài khoảng dữ liệu {first}..{hi}")
        return start, end

    def require_resolved_units(self, start: str, end: str, f: "Filters") -> None:
        """Quantities need every in-scope item's units resolved, checked before any target-unit filter."""
        if not self.scalar("SELECT COUNT(*) FROM sem_unit_issues"):
            return
        ow, op = _order_where(self, f)
        lw, lp = _line_where(f)
        row = self.rows(
            f"SELECT u.item_key, u.reason FROM sem_unit_issues u WHERE u.item_key IN ("
            f"SELECT l.item_key FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' "
            f"AND o.order_date BETWEEN ? AND ?{ow}{lw}) LIMIT 1",
            (start, end, *op, *lp),
        )
        if row:
            raise Abstain(f"Unresolved units: {row[0][1]}")

    def check_filters(self, f: Filters) -> None:
        if f.needs_lines:
            self.require("lines")
        if f.category:
            self.require("category")
        if f.rx is not None:
            self.require("rx")
        if f.ingredient:
            self.require("ingredient")
        if f.customer_key:
            self.require("customer")
        if f.group:
            self.require("customer_group")
        if f.staff:
            self.require("staff")
        if f.channel:
            self.require("channel")
        if f.method:
            self.require("payment_method")
        if f.shift:
            self.require("order_time")


def _order_where(data: ShopData, f: Filters, alias: str = "o") -> tuple[str, list[Any]]:
    sql, params = [], []
    a = alias + "."
    if f.customer_key:
        sql.append(f"{a}customer_key = ?")
        params.append(f.customer_key)
    if f.staff:
        sql.append(f"UPPER({a}staff) = UPPER(?)")
        params.append(f.staff)
    if f.group:
        sql.append(f"{a}customer_group = ?")
        params.append(f.group)
    if f.channel:
        vals = _dimension_values(data, "channel", CHANNEL_CLASSES[f.channel])
        sql.append(f"{a}channel IN ({','.join('?' * len(vals))})" if vals else "0")
        params += vals
    if f.method:
        vals = _dimension_values(data, "payment_method", METHOD_CLASSES[f.method])
        sql.append(f"{a}payment_method IN ({','.join('?' * len(vals))})" if vals else "0")
        params += vals
    if f.shift:
        sql.append(f"{a}order_time BETWEEN ? AND ?")
        params += list(f.shift)
    if f.weekday:
        # SQLite strftime('%w'): Sunday=0. Rulebook weekdays here are ISO (Monday=1).
        sql.append(f"CAST(strftime('%w', {a}order_date) AS INTEGER) = ?")
        params.append(f.weekday % 7)
    return (" AND " + " AND ".join(sql)) if sql else "", params


def _dimension_values(data: ShopData, column: str, accepted: set[str]) -> list[str]:
    vals = [r[0] for r in data.rows(f"SELECT DISTINCT {column} FROM sem_orders WHERE {column} IS NOT NULL")]
    return [v for v in vals if fold_key(v) in accepted or fold_key(v).replace(" ", "_") in accepted]


def _line_where(f: Filters) -> tuple[str, list[Any]]:
    sql, params = [], []
    if f.item_keys:
        sql.append(f"l.item_key IN ({','.join('?' * len(f.item_keys))})")
        params += f.item_keys
    if f.category:
        sql.append("i.category = ?")
        params.append(f.category)
    if f.rx is True:
        sql.append("COALESCE(i.is_rx, 0) = 1")
    elif f.rx is False:
        sql.append("COALESCE(i.is_rx, 0) = 0")
    if f.ingredient:
        sql.append("i.ingredient = ?")
        params.append(f.ingredient)
    if f.brand:
        sql.append("i.brand = ?")
        params.append(f.brand)
    if f.catalog_size:
        sql.append("i.size = ?")
        params.append(f.catalog_size)
    if f.size_suffix:
        sql.append("LOWER(TRIM(i.item_name)) LIKE ?")
        params.append(f"%({f.size_suffix.lower()})")
    return (" AND " + " AND ".join(sql)) if sql else "", params


_LINES_JOIN = "sem_lines l JOIN sem_orders o ON o.order_key = l.order_key LEFT JOIN sem_items i ON i.item_key = l.item_key"


# §1 Revenue
def revenue(data: ShopData, win: DateWindow, f: Filters, *, what: str = "Kỳ hỏi") -> int:
    data.require_healthy(*REVENUE_DEPENDENCIES)
    data.check_filters(f)
    start, end = data.clip(win, what=what)
    ow, op = _order_where(data, f)
    if f.needs_lines:
        lw, lp = _line_where(f)
        return int(data.scalar(
            f"SELECT COALESCE(SUM(l.amount), 0) FROM {_LINES_JOIN} "
            f"WHERE o.eligible = 1 AND o.order_date BETWEEN ? AND ? AND l.role IN ('ITEM', 'SERVICE_FEE'){ow}{lw}",
            (start, end, *op, *lp),
        ))
    return int(data.scalar(
        f"SELECT COALESCE(SUM(o.revenue), 0) FROM sem_orders o WHERE o.eligible = 1 AND o.order_date BETWEEN ? AND ?{ow}",
        (start, end, *op),
    ))


def order_count(data: ShopData, win: DateWindow, f: Filters, *, min_revenue: tuple[str, int] | None = None) -> int:
    data.require_healthy(*(REVENUE_DEPENDENCIES if min_revenue else ORDER_DEPENDENCIES))
    data.check_filters(f)
    start, end = data.clip(win)
    ow, op = _order_where(data, f)
    if f.needs_lines:
        lw, lp = _line_where(f)
        return int(data.scalar(
            f"SELECT COUNT(DISTINCT o.order_key) FROM {_LINES_JOIN} WHERE o.eligible = 1 "
            f"AND o.order_date BETWEEN ? AND ? AND l.role = 'ITEM'{ow}{lw}",
            (start, end, *op, *lp),
        ))
    having = ""
    params: list[Any] = [start, end, *op]
    if min_revenue:
        having = f" AND o.revenue {min_revenue[0]} ?"
        params.append(min_revenue[1])
    return int(data.scalar(
        f"SELECT COUNT(*) FROM sem_orders o WHERE o.eligible = 1 AND o.order_date BETWEEN ? AND ?{ow}{having}",
        tuple(params),
    ))


def cancelled_count(data: ShopData, win: DateWindow, f: Filters) -> int:
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.check_filters(f)
    start, end = data.clip(win)
    ow, op = _order_where(data, f)
    return int(data.scalar(
        f"SELECT COUNT(*) FROM sem_orders o WHERE o.status = 'CANCELLED' AND o.order_date BETWEEN ? AND ?{ow}",
        (start, end, *op),
    ))


def cancel_rate(data: ShopData, win: DateWindow, f: Filters) -> Decimal:
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.check_filters(f)
    start, end = data.clip(win)
    ow, op = _order_where(data, f)
    total, cancelled = data.rows(
        f"SELECT COUNT(*), SUM(o.status = 'CANCELLED') FROM sem_orders o WHERE o.order_date BETWEEN ? AND ?{ow}",
        (start, end, *op),
    )[0]
    if not total:
        raise Abstain("No orders in period")
    return ratio_pct(cancelled or 0, total)


def average_order_value(data: ShopData, win: DateWindow, f: Filters) -> int:
    n = order_count(data, win, f)
    if n == 0:
        raise Abstain("No eligible orders in period")
    return money(Decimal(revenue(data, win, f)) / n)


def growth_pct(current: int, previous: int) -> Decimal:
    if previous == 0:
        raise Abstain("Comparison period has zero baseline")
    return ratio_pct(current - previous, previous)


def best_day(data: ShopData, win: DateWindow, f: Filters, measure: str) -> str:
    data.require_healthy(*(REVENUE_DEPENDENCIES if measure == "revenue" else ORDER_DEPENDENCIES))
    data.check_filters(f)
    start, end = data.clip(win)
    ow, op = _order_where(data, f)
    expr = "SUM(o.revenue)" if measure == "revenue" else "COUNT(*)"
    row = data.rows(
        f"SELECT o.order_date FROM sem_orders o WHERE o.eligible = 1 AND o.order_date BETWEEN ? AND ?{ow} "
        f"GROUP BY o.order_date ORDER BY {expr} DESC, o.order_date ASC LIMIT 1",
        (start, end, *op),
    )
    if not row:
        raise Abstain("No orders in period")
    return row[0][0]


def days_with_revenue(data: ShopData, win: DateWindow, op_sym: str, threshold: int) -> int:
    data.require_healthy(*REVENUE_DEPENDENCIES)
    start, end = data.clip(win)
    return len(data.rows(
        f"SELECT o.order_date FROM sem_orders o WHERE o.eligible = 1 AND o.order_date BETWEEN ? AND ? "
        f"GROUP BY o.order_date HAVING SUM(o.revenue) {op_sym} ?",
        (start, end, threshold),
    ))


def peak_hour(data: ShopData, win: DateWindow, f: Filters) -> int:
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.require("order_time")
    start, end = data.clip(win)
    ow, op = _order_where(data, f)
    row = data.rows(
        f"SELECT CAST(SUBSTR(o.order_time, 1, 2) AS INTEGER) h FROM sem_orders o WHERE o.eligible = 1 "
        f"AND o.order_time IS NOT NULL AND o.order_date BETWEEN ? AND ?{ow} GROUP BY h ORDER BY COUNT(*) DESC, h ASC LIMIT 1",
        (start, end, *op),
    )
    if not row:
        raise Abstain("No timed orders")
    return row[0][0]


# §3 Rankings
ITEM_MEASURES = {
    "qty": "SUM(l.base_qty)",
    "revenue": "SUM(l.amount)",
    "orders": "COUNT(DISTINCT l.order_key)",
    "profit": "SUM(l.amount - l.qty * l.cost_unit)",
}


def top_items(data: ShopData, win: DateWindow, f: Filters, k: int, measure: str) -> list[str]:
    data.require_healthy(*(ORDER_DEPENDENCIES if measure in {"qty", "orders"} else REVENUE_DEPENDENCIES))
    data.check_filters(f)
    data.require("lines")
    if measure == "profit":
        data.require("line_cost")
    start, end = data.clip(win)
    if measure == "qty":
        data.require_resolved_units(start, end, f)
    ow, op = _order_where(data, f)
    lw, lp = _line_where(f)
    rows = data.rows(
        f"SELECT COALESCE(i.item_name, l.item_name) FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' "
        f"AND o.order_date BETWEEN ? AND ?{ow}{lw} GROUP BY l.item_key "
        f"ORDER BY {ITEM_MEASURES[measure]} DESC, COALESCE(i.item_code, 'zzz') ASC, COALESCE(i.item_name, l.item_name) ASC LIMIT ?",
        (start, end, *op, *lp, k),
    )
    return [r[0] for r in rows]


def top_item_group(data: ShopData, win: DateWindow, groups: dict[str, list[str]], measure: str) -> str:
    """The group of items (a phone model line or brand) first by units sold or revenue. Ties go by name."""
    data.require_healthy(*(ORDER_DEPENDENCIES if measure == "qty" else REVENUE_DEPENDENCIES))
    data.require("lines")
    start, end = data.clip(win)
    if measure == "qty":
        data.require_resolved_units(start, end, Filters())
    column = "l.base_qty" if measure == "qty" else "l.amount"
    totals = dict(data.rows(
        f"SELECT l.item_key, SUM({column}) FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' "
        "AND o.order_date BETWEEN ? AND ? GROUP BY l.item_key",
        (start, end),
    ))
    scored = [(sum(totals.get(key) or 0 for key in keys), label) for label, keys in groups.items()]
    scored = [row for row in scored if row[0]]
    if not scored:
        raise Abstain("No sales of these groups in the period")
    return min(scored, key=lambda row: (-row[0], row[1]))[1]


CUSTOMER_MEASURES = {
    "revenue": "SUM(o.revenue)",
    "orders": "COUNT(*)",
    "days": "COUNT(DISTINCT o.order_date)",
}


def top_customers(data: ShopData, win: DateWindow, f: Filters, k: int, measure: str) -> list[str]:
    data.require_healthy(*(REVENUE_DEPENDENCIES if measure in {"revenue", "aov"} else ORDER_DEPENDENCIES))
    data.require("customer")
    start, end = data.clip(win)
    ow, op = _order_where(data, f)
    if measure == "cancelled":
        where = "o.status = 'CANCELLED'"
        expr = "COUNT(*)"
        having = ""
    elif measure == "aov":
        where = "o.eligible = 1"
        expr = "CAST(SUM(o.revenue) AS REAL) / COUNT(*)"
        having = " HAVING COUNT(*) >= 3"
    else:
        where = "o.eligible = 1"
        expr = CUSTOMER_MEASURES[measure]
        having = ""
    rows = data.rows(
        f"SELECT COALESCE(c.name, o.customer_name, o.customer_key) FROM sem_orders o "
        f"LEFT JOIN sem_customers c ON c.customer_key = o.customer_key WHERE {where} AND o.customer_key IS NOT NULL "
        f"AND o.order_date BETWEEN ? AND ?{ow} GROUP BY o.customer_key{having} "
        f"ORDER BY {expr} DESC, o.customer_key ASC LIMIT ?",
        (start, end, *op, k),
    )
    return [r[0] for r in rows]


def customers_with_min_orders(data: ShopData, win: DateWindow, n: int) -> tuple[int, int]:
    """Returns (customers with at least n eligible orders, all customers who bought) in the period."""
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.require("customer")
    start, end = data.clip(win)
    rows = data.rows(
        "SELECT COUNT(*) FROM sem_orders o WHERE o.eligible = 1 AND o.customer_key IS NOT NULL "
        "AND o.order_date BETWEEN ? AND ? GROUP BY o.customer_key",
        (start, end),
    )
    return sum(1 for (c,) in rows if c >= n), len(rows)


def distinct_customers(data: ShopData, win: DateWindow, f: Filters) -> int:
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.require("customer")
    start, end = data.clip(win)
    ow, op = _order_where(data, f)
    return int(data.scalar(
        f"SELECT COUNT(DISTINCT o.customer_key) FROM sem_orders o WHERE o.eligible = 1 AND o.order_date BETWEEN ? AND ?{ow}",
        (start, end, *op),
    ))


def items_with_min_orders(data: ShopData, win: DateWindow, n: int) -> int:
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.require("lines")
    start, end = data.clip(win)
    return len(data.rows(
        f"SELECT l.item_key FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' AND o.order_date BETWEEN ? AND ? "
        "GROUP BY l.item_key HAVING COUNT(DISTINCT l.order_key) >= ?",
        (start, end, n),
    ))


def top_staff(data: ShopData, win: DateWindow, measure: str) -> str:
    data.require_healthy(*REVENUE_DEPENDENCIES)
    data.require("staff")
    start, end = data.clip(win)
    expr = {"revenue": "SUM(o.revenue)", "days": "COUNT(DISTINCT o.order_date)", "orders": "COUNT(*)"}[measure]
    row = data.rows(
        f"SELECT o.staff FROM sem_orders o WHERE o.eligible = 1 AND o.staff IS NOT NULL AND o.order_date BETWEEN ? AND ? "
        f"GROUP BY UPPER(o.staff) ORDER BY {expr} DESC, UPPER(o.staff) ASC LIMIT 1",
        (start, end),
    )
    if not row:
        raise Abstain("No staff rows")
    return row[0][0]


def quantity_sold(data: ShopData, win: DateWindow, f: Filters, unit: str | None = None) -> int | float:
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.check_filters(f)
    data.require("lines")
    start, end = data.clip(win)
    data.require_resolved_units(start, end, f)
    ow, op = _order_where(data, f)
    lw, lp = _line_where(f)
    unit_sql, unit_params = "", []
    if unit:
        rows = data.rows("SELECT item_key, base_unit FROM sem_items WHERE base_unit IS NOT NULL")
        keys = [k for k, u in rows if fold_key(u) == unit]
        unit_sql = f" AND l.item_key IN ({','.join('?' * len(keys))})" if keys else " AND 0"
        unit_params = keys
    val = data.scalar(
        f"SELECT SUM(l.base_qty) FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' "
        f"AND o.order_date BETWEEN ? AND ?{ow}{lw}{unit_sql}",
        (start, end, *op, *lp, *unit_params),
    )
    return clean_qty(val)


# §5 Purchases
def purchase_total(data: ShopData, win: DateWindow, supplier: str | None = None) -> int:
    data.require("purchases", "purchase_amount")
    start, end = data.clip(win, source="purchases")
    return int(data.scalar(
        "SELECT COALESCE(SUM(amount), 0) FROM sem_purchases WHERE cancelled = 0 AND receipt_date BETWEEN ? AND ?",
        (start, end),
    ))


def top_supplier(data: ShopData, win: DateWindow, measure: str) -> str:
    data.require("purchases")
    if measure == "amount":
        data.require("purchase_amount")
    start, end = data.clip(win, source="purchases")
    expr = "SUM(amount)" if measure == "amount" else "COUNT(*)"
    row = data.rows(
        f"SELECT supplier_name FROM sem_purchases WHERE cancelled = 0 AND supplier_name IS NOT NULL "
        f"AND receipt_date BETWEEN ? AND ? GROUP BY supplier_key ORDER BY {expr} DESC, supplier_key ASC LIMIT 1",
        (start, end),
    )
    if not row:
        raise Abstain("No purchases in period")
    return row[0][0]


def avg_purchase_price(data: ShopData, win: DateWindow, item_keys: list[str], unit: str | None) -> int:
    data.require("purchase_lines", "purchase_amount", "purchases")
    start, end = data.clip(win, source="purchases")
    marks = ",".join("?" * len(item_keys))
    rows = data.rows(
        f"SELECT pl.unit, SUM(pl.qty), SUM(pl.amount) FROM sem_purchase_lines pl JOIN sem_purchases p ON p.receipt_key = pl.receipt_key "
        f"WHERE p.cancelled = 0 AND pl.receipt_date BETWEEN ? AND ? AND pl.item_key IN ({marks}) GROUP BY pl.unit",
        (start, end, *item_keys),
    )
    if unit:
        rows = [r for r in rows if r[0] and fold_key(r[0]) == unit] or rows
    if len(rows) != 1 or not rows[0][1]:
        raise Abstain("Purchase unit is ambiguous or no purchases")
    return money(Decimal(int(rows[0][2])) / Decimal(str(rows[0][1])))


def purchased_minus_sold(data: ShopData, win: DateWindow, f: Filters) -> int | float:
    """§5: purchased quantity minus quantity on revenue-eligible orders, both in base units."""
    data.require_healthy("purchases")
    data.require("purchase_lines")
    start, end = data.clip(win)
    marks = ",".join("?" * len(f.item_keys))
    bought = Decimal(0)
    factors = {(k, fold_key(u)): fac for k, u, fac in data.rows("SELECT item_key, unit, factor FROM sem_units")}
    for key, qty, unit, base in data.rows(
        f"SELECT pl.item_key, pl.qty, pl.unit, pl.base_qty FROM sem_purchase_lines pl JOIN sem_purchases p ON p.receipt_key = pl.receipt_key "
        f"WHERE p.cancelled = 0 AND pl.receipt_date BETWEEN ? AND ? AND pl.item_key IN ({marks})",
        (start, end, *f.item_keys),
    ):
        if base is not None:
            bought += Decimal(str(base))
        elif qty is not None:
            bought += Decimal(str(qty)) * Decimal(str(factors.get((key, fold_key(unit or "")), 1)))
    sold = quantity_sold(data, win, f)
    return clean_qty(float(bought - Decimal(str(sold))))


# §2/§5 Prices
def price_on_date(data: ShopData, item_keys: list[str], day: str) -> int:
    data.require_healthy("orders")
    data.require("lines")
    marks = ",".join("?" * len(item_keys))
    rows = data.rows(
        f"SELECT l.unit_price, l.qty, l.base_qty FROM sem_lines l JOIN sem_orders o ON o.order_key = l.order_key "
        f"WHERE o.order_date = ? AND l.item_key IN ({marks}) AND l.unit_price IS NOT NULL AND l.qty > 0",
        (day, *item_keys),
    )
    prices = set()
    for price, qty, base in rows:
        factor = Decimal(str(base)) / Decimal(str(qty)) if base and qty else Decimal(1)
        prices.add(money(Decimal(price) / factor))
    if len(prices) == 1:
        return prices.pop()
    if len(prices) > 1:
        raise Abstain("Several prices recorded on that day")
    hist = data.rows(
        f"SELECT price FROM sem_price_history WHERE item_key IN ({marks}) AND effective_date <= ? ORDER BY effective_date DESC LIMIT 1",
        (*item_keys, day),
    )
    if hist and hist[0][0] is not None:
        return hist[0][0]
    raise Abstain("No price evidence for that day")


def price_effect_pct(data: ShopData, item_keys: list[str], day: str, n: int) -> Decimal:
    d = date.fromisoformat(day)
    after = DateWindow(day, (d + timedelta(days=n - 1)).isoformat())
    before = DateWindow((d - timedelta(days=n)).isoformat(), (d - timedelta(days=1)).isoformat())
    f = Filters(item_keys=item_keys)
    # Both spans are read as recorded: this clause compares fixed spans, not periods relative to today.
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.require_resolved_units(before.start_date, after.end_date, f)
    a = _raw_qty(data, after, f)
    b = _raw_qty(data, before, f)
    if not b:
        raise Abstain("No sales before the price change")
    return ratio_pct(Decimal(str(a)) - Decimal(str(b)), Decimal(str(b)))


def first_increase_to(data: ShopData, item_keys: list[str], new_price: int) -> str | None:
    """§5: the first day the price list rose to `new_price` from a lower price."""
    marks = ",".join("?" * len(item_keys))
    rows = data.rows(
        f"SELECT effective_date, price FROM sem_price_history WHERE item_key IN ({marks}) ORDER BY effective_date",
        tuple(item_keys),
    )
    previous = None
    for day, price in rows:
        if price == new_price and previous is not None and previous < price:
            return day
        previous = price
    return None


def due_count(data: ShopData, span: tuple[str, str], as_of: str) -> int:
    """Non-cancelled credit orders placed by today whose recorded due date falls in `span`."""
    return int(data.scalar(
        "SELECT COUNT(DISTINCT d.order_key) FROM sem_due d JOIN sem_orders o ON o.order_key = d.order_key "
        "WHERE o.status != 'CANCELLED' AND o.order_date <= ? AND d.due_date BETWEEN ? AND ?",
        (as_of, span[0], span[1]),
    ))


def _raw_qty(data: ShopData, win: DateWindow, f: Filters) -> float:
    lw, lp = _line_where(f)
    return data.scalar(
        f"SELECT COALESCE(SUM(l.base_qty), 0) FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' "
        f"AND o.order_date BETWEEN ? AND ?{lw}",
        (win.start_date, win.end_date, *lp),
    )


# §5 Stock
def stock_at(data: ShopData, item_key: str, day: str) -> float:
    """Physical stock at end of `day`: last movement, or the final-day snapshot when that is the day asked."""
    if data.cap("stock_moves"):
        row = data.rows(
            "SELECT after_qty FROM sem_stock_moves WHERE item_key = ? AND SUBSTR(moved_at, 1, 10) <= ? "
            "ORDER BY moved_at DESC, rowid DESC LIMIT 1",
            (item_key, day),
        )
        return row[0][0] if row else 0.0
    if data.cap("stock_snapshot"):
        if day < (data.end or ""):
            raise Unanswerable("Tồn kho chỉ ghi tại ngày cuối dữ liệu, không có lịch sử để tính ngày trước đó")
        row = data.rows("SELECT qty FROM sem_stock_snapshot WHERE item_key = ?", (item_key,))
        return row[0][0] if row else 0.0
    if data.cap("lots"):
        data.require_healthy(*ORDER_DEPENDENCIES, "lots")
        return data.scalar(
            "SELECT COALESCE((SELECT SUM(qty_in) FROM sem_lots WHERE item_key = ? AND COALESCE(received, '') <= ?), 0) - "
            "COALESCE((SELECT SUM(a.qty - a.returned) FROM sem_lot_alloc a JOIN sem_lots l ON l.lot = a.lot "
            "JOIN sem_orders o ON o.order_key = a.order_key WHERE l.item_key = ? AND o.status != 'CANCELLED' AND o.order_date <= ?), 0)",
            (item_key, day, item_key, day),
        )
    raise Unanswerable("Shop không ghi tồn kho")


def inventory_value(data: ShopData, day: str) -> int:
    """§5: end-of-day stock of each item in stock, times the catalog's base-unit cost."""
    if not (data.cap("stock_moves") or data.cap("stock_snapshot") or data.cap("lots")):
        raise Unanswerable("Shop không ghi tồn kho")
    data.require("catalog_cost")
    total = Decimal(0)
    for key, cost in data.rows("SELECT item_key, cost FROM sem_items WHERE cost IS NOT NULL"):
        qty = stock_at(data, key, day)
        if qty and qty > 0:
            total += Decimal(str(qty)) * cost
    return money(total)


def expiring_items(data: ShopData, day: str, end: str) -> list[str]:
    """§9: items with remaining stock in a lot expiring in the span, earliest expiry first, ties by code."""
    data.require("lots")
    data.require_healthy(*ORDER_DEPENDENCIES, "lots")
    rows = data.rows(
        "SELECT i.item_name, MIN(l.expiry) e, i.item_code FROM sem_lots l JOIN sem_items i ON i.item_key = l.item_key "
        "WHERE l.expiry BETWEEN ? AND ? AND l.qty_in > COALESCE((SELECT SUM(a.qty - a.returned) FROM sem_lot_alloc a "
        "JOIN sem_orders o ON o.order_key = a.order_key WHERE a.lot = l.lot AND o.status != 'CANCELLED' AND o.order_date <= ?), 0) "
        "GROUP BY l.item_key ORDER BY e ASC, COALESCE(i.item_code, i.item_name) ASC",
        (day, end, day),
    )
    return [r[0] for r in rows]


def split_stock(data: ShopData, item_key: str, qty: float, big_unit: str) -> list[int]:
    """Rulebook §5: [whole large units, remainder in base units]."""
    rows = data.rows("SELECT unit, factor FROM sem_units WHERE item_key = ?", (item_key,))
    factors = [f for u, f in rows if fold_key(u) == big_unit]
    if not factors or factors[0] <= 0:
        raise Abstain("No conversion for requested large unit")
    whole = int(qty // factors[0])
    return [whole, clean_qty(qty - whole * factors[0])]


# §4 Debt
NON_DEBT_METHODS = METHOD_CLASSES["cod"]
NON_DEBT_CHANNELS = CHANNEL_CLASSES["marketplace"]


def _debt_orders(data: ShopData, day: str) -> list[tuple[str, str, str | None, str | None, int]]:
    """Per-order balance at end of `day` for orders that can owe (§4). Positive balances only."""
    data.require_healthy(*ORDER_DEPENDENCIES, "payable")
    if not data.cap("payment_method") and data.attributes_unresolved():
        raise Abstain("Debt excludes COD orders, and a column that may record the method is unexplained")
    if data.cap("payments"):
        rows = data.rows(
            "SELECT o.order_key, o.order_date, o.customer_key, o.customer_name, o.payable - COALESCE(p.paid, 0), "
            "o.payment_method, o.channel "
            "FROM sem_orders o LEFT JOIN (SELECT order_key, SUM(amount) paid FROM sem_payments WHERE pay_date <= ? GROUP BY order_key) p "
            "ON p.order_key = o.order_key WHERE o.status != 'CANCELLED' AND o.order_date <= ? AND o.payable IS NOT NULL",
            (day, day),
        )
    elif data.cap("separate_debt_ledger"):
        raise Abstain("Debt is kept in a separate ledger this code does not read yet")
    elif data.cap("paid_recorded"):
        if day < (data.end or ""):
            raise Unanswerable("Công nợ chỉ ghi tại ngày cuối dữ liệu, không có các lần trả để tính ngày trước đó")
        rows = data.rows(
            "SELECT o.order_key, o.order_date, o.customer_key, o.customer_name, "
            "COALESCE(o.debt_recorded, o.payable - o.paid_recorded), o.payment_method, o.channel "
            "FROM sem_orders o WHERE o.status != 'CANCELLED' AND o.order_date <= ?",
            (day,),
        )
    else:
        raise Unanswerable("Shop không ghi công nợ")
    out = []
    for key, d, ck, name, bal, method, channel in rows:
        if bal is None or bal <= 0:
            continue
        if method and fold_key(method) in NON_DEBT_METHODS:
            continue
        if channel and fold_key(channel) in NON_DEBT_CHANNELS:
            continue
        out.append((key, d, ck, name, int(bal)))
    return out


def total_debt(data: ShopData, day: str, *, older_than: int | None = None, age_range: tuple[int, int | None] | None = None) -> int:
    d0 = date.fromisoformat(day)
    total = 0
    for _, d, _, _, bal in _debt_orders(data, day):
        age = (d0 - date.fromisoformat(d)).days
        if older_than is not None and age <= older_than:
            continue
        if age_range and not (age >= age_range[0] and (age_range[1] is None or age <= age_range[1])):
            continue
        total += bal
    return total


def debtor_count(data: ShopData, day: str) -> int:
    return len({ck for _, _, ck, _, _ in _debt_orders(data, day) if ck})


def top_debtors(data: ShopData, day: str, k: int | None) -> list[tuple[str, int]]:
    per: dict[str, list[Any]] = {}
    for _, _, ck, name, bal in _debt_orders(data, day):
        if ck:
            per.setdefault(ck, [name, 0])[1] += bal
    ordered = sorted(per.items(), key=lambda kv: (-kv[1][1], kv[0]))
    if k:
        ordered = ordered[:k]
    names = dict(data.rows("SELECT customer_key, name FROM sem_customers"))
    return [(names.get(ck) or v[0] or ck, v[1]) for ck, v in ordered]


_UNPAID_ON_PURCHASE_DAY = (
    " AND o.payable > COALESCE((SELECT SUM(p0.amount) FROM sem_payments p0 "
    "WHERE p0.order_key = o.order_key AND p0.pay_date <= o.order_date), 0)"
)


def _credit_condition(data: ShopData) -> tuple[str, list[str]]:
    """Credit orders: recorded method when the shop has one, else orders left unpaid on the purchase day."""
    vals = _dimension_values(data, "payment_method", METHOD_CLASSES["debt"])
    if not vals:
        if not data.cap("payment_method") and data.attributes_unresolved():
            raise Abstain("Credit orders need a payment method, and a column that may hold it is unexplained")
        return _UNPAID_ON_PURCHASE_DAY, []
    return f" AND o.payment_method IN ({','.join('?' * len(vals))})", vals


def debt_added(data: ShopData, win: DateWindow, customer_key: str | None = None) -> int:
    """§4 Nợ thêm: part of the order total unpaid on the purchase day, for credit orders placed in the period."""
    data.require_healthy(*ORDER_DEPENDENCIES, "payable")
    data.require("payments")
    start, end = data.clip(win)
    cond, params = _credit_condition(data)
    cw = " AND o.customer_key = ?" if customer_key else ""
    cp = [customer_key] if customer_key else []
    rows = data.rows(
        "SELECT o.payable - COALESCE((SELECT SUM(p.amount) FROM sem_payments p WHERE p.order_key = o.order_key AND p.pay_date <= o.order_date), 0) "
        f"FROM sem_orders o WHERE o.status != 'CANCELLED' AND o.order_date BETWEEN ? AND ?{cond}{cw}",
        (start, end, *params, *cp),
    )
    return sum(int(r[0]) for r in rows if r[0] and r[0] > 0)


def debt_collected(data: ShopData, win: DateWindow, customer_key: str | None = None) -> int:
    """§4 Thu nợ: payments after the purchase day, dated in the period, on any non-cancelled credit order."""
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.require("payments")
    start, end = data.clip(win)
    cond, params = _credit_condition(data)
    cw = " AND o.customer_key = ?" if customer_key else ""
    cp = [customer_key] if customer_key else []
    return int(data.scalar(
        "SELECT COALESCE(SUM(p.amount), 0) FROM sem_payments p JOIN sem_orders o ON o.order_key = p.order_key "
        f"WHERE o.status != 'CANCELLED' AND p.pay_date BETWEEN ? AND ? AND p.pay_date > o.order_date{cond}{cw}",
        (start, end, *params, *cp),
    ))


# §5 Profit
def gross_profit(data: ShopData, win: DateWindow, f: Filters) -> int:
    data.require_healthy(*REVENUE_DEPENDENCIES)
    data.require("line_cost")
    data.check_filters(f)
    start, end = data.clip(win)
    ow, op = _order_where(data, f)
    lw, lp = _line_where(f)
    missing = data.scalar(
        f"SELECT COUNT(*) FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' AND l.cost_unit IS NULL "
        f"AND o.order_date BETWEEN ? AND ?{ow}{lw}",
        (start, end, *op, *lp),
    )
    if missing:
        raise Abstain("Some sold lines have no recorded cost")
    rev = revenue(data, win, f)
    cost = data.scalar(
        f"SELECT COALESCE(SUM(l.qty * l.cost_unit), 0) FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' "
        f"AND o.order_date BETWEEN ? AND ?{ow}{lw}",
        (start, end, *op, *lp),
    )
    return money(Decimal(rev) - Decimal(str(cost)))


def net_profit(data: ShopData, win: DateWindow) -> int:
    data.require("expenses")
    start, end = data.clip(win)
    exp = data.scalar("SELECT COALESCE(SUM(amount), 0) FROM sem_expenses WHERE expense_date BETWEEN ? AND ?", (start, end))
    return gross_profit(data, win, Filters()) - int(exp)


def delivery_batches(data: ShopData, win: DateWindow) -> int:
    data.require("deliveries")
    start, end = data.clip(win)
    return int(data.scalar("SELECT COUNT(*) FROM sem_deliveries WHERE delivery_date BETWEEN ? AND ?", (start, end)))


def unsold_items(data: ShopData, win: DateWindow) -> list[str]:
    """§5 Ế: items sold before the period (eligible orders) with no eligible order in the period."""
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.require("lines")
    start, end = data.clip(win)
    rows = data.rows(
        f"""SELECT i.item_name FROM sem_items i WHERE i.item_key IN (
              SELECT l.item_key FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' AND o.order_date < ?)
            AND i.item_key NOT IN (
              SELECT l.item_key FROM {_LINES_JOIN} WHERE o.eligible = 1 AND l.role = 'ITEM' AND o.order_date BETWEEN ? AND ?)
            ORDER BY COALESCE(i.item_code, i.item_name)""",
        (start, start, end),
    )
    return [r[0] for r in rows]


def nearest_expiry(data: ShopData, item_keys: list[str], day: str) -> str | None:
    """§6 pharmacy: the earliest expiry among an item's lots still in stock and not expired at `day`."""
    data.require("lots")
    data.require_healthy(*ORDER_DEPENDENCIES, "lots")
    marks = ",".join("?" * len(item_keys))
    return data.scalar(
        f"SELECT MIN(l.expiry) FROM sem_lots l WHERE l.item_key IN ({marks}) AND l.expiry >= ? AND COALESCE(l.received, '') <= ? "
        "AND l.qty_in > COALESCE((SELECT SUM(a.qty - a.returned) FROM sem_lot_alloc a JOIN sem_orders o ON o.order_key = a.order_key "
        "WHERE a.lot = l.lot AND o.status != 'CANCELLED' AND o.order_date <= ?), 0)",
        (*item_keys, day, day, day),
    )


def expiring_lot_count(data: ShopData, day: str, end: str) -> int:
    """Lots with stock left at `day` that expire in [day, end]."""
    data.require("lots")
    data.require_healthy(*ORDER_DEPENDENCIES, "lots")
    return int(data.scalar(
        "SELECT COUNT(*) FROM sem_lots l WHERE l.expiry BETWEEN ? AND ? AND l.qty_in > COALESCE((SELECT SUM(a.qty - a.returned) "
        "FROM sem_lot_alloc a JOIN sem_orders o ON o.order_key = a.order_key WHERE a.lot = l.lot AND o.status != 'CANCELLED' AND o.order_date <= ?), 0)",
        (day, end, day),
    ))


def out_of_stock_items(data: ShopData, day: str) -> list[str]:
    """§5: items bought to sell whose stock is zero at the end of `day`, by code. Items made on site have no stock record."""
    if data.cap("stock_moves"):
        source = "SELECT item_key FROM sem_stock_moves"
    elif data.cap("stock_snapshot"):
        source = "SELECT item_key FROM sem_stock_snapshot"
    elif data.cap("lots"):
        source = "SELECT item_key FROM sem_lots"
    else:
        raise Unanswerable("Shop không ghi tồn kho")
    rows = data.rows(
        f"SELECT item_key, item_name FROM sem_items WHERE item_key IN ({source}) ORDER BY COALESCE(item_code, 'zzz'), item_name"
    )
    return [name for key, name in rows if stock_at(data, key, day) == 0]


def expiring_stock_cost(data: ShopData, day: str, n: int) -> int:
    """§6 pharmacy: remaining lot quantity expiring in [D, D+N], valued at the lot's own base-unit price."""
    data.require("lots")
    data.require_healthy(*ORDER_DEPENDENCIES, "lots")
    end = (date.fromisoformat(day) + timedelta(days=n)).isoformat()
    rows = data.rows(
        "SELECT l.lot, l.qty_in, l.unit_price, "
        "COALESCE((SELECT SUM(a.qty - a.returned) FROM sem_lot_alloc a JOIN sem_orders o ON o.order_key = a.order_key "
        "          WHERE a.lot = l.lot AND o.status != 'CANCELLED' AND o.order_date <= ?), 0) "
        "FROM sem_lots l WHERE l.expiry BETWEEN ? AND ?",
        (day, day, end),
    )
    total = Decimal(0)
    for _, qty_in, price, used in rows:
        left = Decimal(str(qty_in or 0)) - Decimal(str(used or 0))
        if left > 0 and price is not None:
            total += left * Decimal(str(price))
    return money(total)


# §6 Mother and baby
def adjustment_total(data: ShopData, win: DateWindow, label_like: str) -> int:
    """Voucher, points or shipping rows, reported positive, by invoice date, on non-cancelled non-returned orders."""
    data.require_healthy(*ORDER_DEPENDENCIES)
    data.require("lines")
    start, end = data.clip(win)
    rows = data.rows(
        "SELECT l.amount, l.raw_label FROM sem_lines l JOIN sem_orders o ON o.order_key = l.order_key "
        "WHERE o.eligible = 1 AND o.order_date BETWEEN ? AND ? AND l.role IN ('ORDER_DISCOUNT', 'SHIPPING')",
        (start, end),
    )
    needle = label_like.strip("%")
    return sum(abs(int(a)) for a, label in rows if a is not None and label and needle in fold_key(label))


def new_members(data: ShopData, win: DateWindow) -> int:
    start, end = data.clip(win)
    if not data.scalar("SELECT COUNT(registered) FROM sem_customers"):
        raise Unanswerable("Shop không ghi ngày đăng ký")
    return int(data.scalar("SELECT COUNT(*) FROM sem_customers WHERE registered BETWEEN ? AND ?", (start, end)))


# §6 Phone shop
def order_field_total(data: ShopData, win: DateWindow, field_name: str) -> int:
    data.require_healthy(*REVENUE_DEPENDENCIES)
    start, end = data.clip(win)
    if field_name not in {"tradein", "payable"}:
        raise ValueError(field_name)
    return int(data.scalar(
        f"SELECT COALESCE(SUM({field_name}), 0) FROM sem_orders WHERE eligible = 1 AND order_date BETWEEN ? AND ?",
        (start, end),
    ))


# §6 Vehicle repair
def service_count(data: ShopData, start: str, end: str, *, warranty: bool | None = None) -> int:
    cond = "" if warranty is None else (" AND is_warranty = 1" if warranty else " AND is_warranty = 0")
    return int(data.scalar(f"SELECT COUNT(*) FROM sem_service WHERE service_date BETWEEN ? AND ?{cond}", (start, end)))


def service_money(data: ShopData, start: str, end: str) -> int:
    return int(data.scalar("SELECT COALESCE(SUM(amount), 0) FROM sem_service WHERE service_date BETWEEN ? AND ? AND is_warranty = 0", (start, end)))


def top_mechanic(data: ShopData, start: str, end: str, measure: str) -> str:
    expr = "COUNT(*)" if measure == "tickets" else "SUM(amount)"
    row = data.rows(
        f"SELECT mechanic FROM sem_service WHERE service_date BETWEEN ? AND ? AND mechanic IS NOT NULL "
        f"GROUP BY mechanic ORDER BY {expr} DESC, mechanic ASC LIMIT 1",
        (start, end),
    )
    if not row:
        raise Abstain("No tickets in period")
    return row[0][0]


def distinct_vehicles(data: ShopData, start: str, end: str) -> int:
    return int(data.scalar("SELECT COUNT(DISTINCT plate) FROM sem_service WHERE service_date BETWEEN ? AND ?", (start, end)))


def returning_vehicles(data: ShopData, start: str, end: str, n: int) -> int:
    """§6: vehicles by plate with at least n paid tickets in the period; warranty visits do not count."""
    return len(data.rows(
        "SELECT plate FROM sem_service WHERE service_date BETWEEN ? AND ? AND is_warranty = 0 GROUP BY plate HAVING COUNT(*) >= ?",
        (start, end, n),
    ))


def service_job_count(data: ShopData, start: str, end: str, job: str) -> int:
    """§6: paid tickets containing a job; `nhot` alone is engine oil, warranty tickets are not job visits."""
    rows = data.rows("SELECT jobs_key FROM sem_service WHERE service_date BETWEEN ? AND ? AND is_warranty = 0", (start, end))
    import re as _re
    tests = {
        "nhot": lambda s: _re.search(r"\bnhot\b(?! lap)", s),
        "nhot lap": lambda s: _re.search(r"nhot lap|dau lap", s),
        "va": lambda s: _re.search(r"\bva\b", s),
        "bao duong": lambda s: _re.search(r"bao duong|bdtq", s),
        "dien": lambda s: _re.search(r"lam dien|sua dien", s),
        "vs noi": lambda s: _re.search(r"ve sinh noi|\bvs noi\b", s),
        "vs kim phun": lambda s: _re.search(r"ve sinh kim phun|\bvs kim phun\b", s),
        "bu gi": lambda s: _re.search(r"bu gi|bugi", s),
        "nuoc mat": lambda s: _re.search(r"nuoc mat", s),
        "nhong xich": lambda s: _re.search(r"nhong xich", s),
        "binh": lambda s: _re.search(r"ac quy|\bbinh\b", s),
    }
    test = tests[job]
    return sum(1 for (s,) in rows if s and test(s))
