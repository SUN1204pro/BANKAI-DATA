"""Canonical `sem_*` tables built on top of the ingested per-business tables.

Ingestion keeps each source table as found. Questions need a stable shape, so this module derives one
table per logical concept (orders, lines, payments, purchases, stock, lots, expenses, catalogs) and a
capability list saying which concepts this business actually records.

Mappings come from README glosses, header words, and measured key overlap, never from business IDs. A
concept with no reliable source is simply absent, and the capability list says so: metrics then return
`unanswerable` or abstain instead of a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable

from zalo_engine.ingest import classify_sales_row_role
from zalo_engine.normalizers import parse_date_value, parse_money, parse_quantity
from zalo_engine.text_norm import fold, fold_key
from zalo_engine.verifier import _sql_extract_time

REVENUE_STATES = ("COMPLETED", "SHIPPING")

_SEM_SCHEMA = """
CREATE TABLE sem_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sem_orders (
    order_key TEXT PRIMARY KEY, raw_id TEXT, order_date TEXT, order_time TEXT, status TEXT, eligible INTEGER,
    customer_key TEXT, customer_code TEXT, customer_name TEXT, customer_label TEXT, customer_group TEXT,
    staff TEXT, channel TEXT, payment_method TEXT,
    revenue INTEGER, payable INTEGER, discount INTEGER, shipping INTEGER, tradein INTEGER,
    paid_recorded INTEGER, debt_recorded INTEGER, has_lines INTEGER
);
CREATE TABLE sem_lines (
    order_key TEXT, role TEXT, item_key TEXT, item_code TEXT, item_name TEXT, category TEXT,
    qty REAL, unit TEXT, base_qty REAL, base_unit TEXT, unit_price INTEGER, amount INTEGER,
    line_discount INTEGER, cost_unit INTEGER, raw_label TEXT
);
CREATE TABLE sem_items (
    item_key TEXT PRIMARY KEY, item_code TEXT, item_name TEXT, category TEXT, base_unit TEXT, cost INTEGER,
    price INTEGER, is_rx INTEGER, ingredient TEXT, brand TEXT, size TEXT, active INTEGER
);
CREATE TABLE sem_item_names (name_key TEXT PRIMARY KEY, item_key TEXT, label TEXT);
CREATE TABLE sem_units (item_key TEXT, unit TEXT, factor REAL, PRIMARY KEY (item_key, unit));
CREATE TABLE sem_customers (
    customer_key TEXT PRIMARY KEY, code TEXT, name TEXT, phone TEXT, grp TEXT, registered TEXT
);
CREATE TABLE sem_payments (order_key TEXT, pay_date TEXT, pay_time TEXT, amount INTEGER, method TEXT);
CREATE TABLE sem_due (order_key TEXT, due_date TEXT);
CREATE TABLE sem_purchases (
    receipt_key TEXT PRIMARY KEY, receipt_date TEXT, supplier_key TEXT, supplier_name TEXT, amount INTEGER,
    status TEXT, cancelled INTEGER
);
CREATE TABLE sem_purchase_lines (
    receipt_key TEXT, receipt_date TEXT, item_key TEXT, item_name TEXT, qty REAL, unit TEXT, base_qty REAL,
    unit_price INTEGER, amount INTEGER, lot TEXT, expiry TEXT
);
CREATE TABLE sem_stock_moves (item_key TEXT, moved_at TEXT, after_qty REAL);
CREATE TABLE sem_stock_snapshot (item_key TEXT PRIMARY KEY, qty REAL);
CREATE TABLE sem_lots (lot TEXT PRIMARY KEY, item_key TEXT, expiry TEXT, qty_in REAL, unit_price REAL, received TEXT);
CREATE TABLE sem_lot_alloc (order_key TEXT, lot TEXT, qty REAL, returned REAL);
CREATE TABLE sem_expenses (expense_date TEXT, amount INTEGER, category TEXT);
CREATE TABLE sem_price_history (item_key TEXT, effective_date TEXT, price INTEGER, old_price INTEGER);
CREATE TABLE sem_deliveries (order_key TEXT, batch TEXT, delivery_date TEXT);
CREATE TABLE sem_service (service_date TEXT, plate TEXT, model TEXT, jobs_key TEXT, mechanic TEXT, amount INTEGER, is_warranty INTEGER);
CREATE TABLE sem_staff (name_key TEXT PRIMARY KEY, code TEXT, name TEXT);
CREATE TABLE sem_unit_issues (item_key TEXT, unit TEXT, reason TEXT);
"""


@dataclass(frozen=True)
class Col:
    sql: str
    header: str
    role: str
    gloss: str

    @property
    def h(self) -> str:
        return fold_key(self.header)

    @property
    def g(self) -> str:
        return fold_key(self.gloss)


@dataclass
class Table:
    name: str
    grain: str
    cols: list[Col]

    @property
    def key(self) -> str:
        return fold_key(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", self.name))

    @property
    def compact_key(self) -> str:
        return self.key.replace(" ", "")

    def role(self, *roles: str) -> Col | None:
        return next((c for r in roles for c in self.cols if c.role == r), None)

    def roles(self, *roles: str) -> list[Col]:
        return [c for c in self.cols if c.role in roles]

    def by(self, headers: Iterable[str] = (), glosses: Iterable[str] = ()) -> Col | None:
        """First column whose folded header equals one of `headers` or gloss contains one of `glosses`."""
        hs = set(headers)
        for c in self.cols:
            if c.h in hs or c.h.replace(" ", "") in hs:
                return c
        for needle in glosses:
            for c in self.cols:
                if needle in c.g:
                    return c
        return None


def load_tables(conn: sqlite3.Connection) -> list[Table]:
    grains = dict(conn.execute("SELECT DISTINCT logical_table, table_grain FROM _ingested_tables").fetchall())
    by_table: dict[str, list[Col]] = {}
    for t, sql, header, role, gloss in conn.execute(
        "SELECT logical_table, sql_col_name, raw_header, role, readme_gloss FROM _ingested_columns ORDER BY col_index"
    ):
        by_table.setdefault(t, []).append(Col(sql, header, role, gloss or ""))
    return [Table(t, grains.get(t, "other"), cols) for t, cols in by_table.items()]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _rows(conn: sqlite3.Connection, table: Table, cols: dict[str, str | None]) -> list[dict[str, Any]]:
    present = {k: v for k, v in cols.items() if v}
    select = ", ".join(f"{_q(v)} AS {_q(k)}" for k, v in present.items())
    out = []
    for row in conn.execute(f"SELECT {select} FROM {_q(table.name)}"):
        d = dict(zip(present.keys(), row))
        for k in cols:
            d.setdefault(k, None)
        out.append(d)
    return out


def _money(val: Any) -> int | None:
    if val is None or val == "":
        return None
    if isinstance(val, int):
        return val
    res = parse_money(val)
    if res.value is None or res.diagnostic is not None:
        return None
    return int(res.value.to_integral_value())


def _number(val: Any) -> float | None:
    if val is None or val == "":
        return None
    res = parse_quantity(val)
    return float(res.value) if res.value is not None else None


def _date(val: Any, year: int | None = None) -> str | None:
    if val is None or val == "":
        return None
    return parse_date_value(val, context_year=year).value


def _clean(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


_ORDER_FK_HINTS = ("ma don", "so hd", "ma hoa don", "ma bill", "so hoa don", "don", "hoa don", "so ct")


def _best_overlap_col(
    conn: sqlite3.Connection, table: Table, cols: list[Col], universe: set[str], *, min_ratio: float = 0.5
) -> Col | None:
    """Pick the column whose distinct values overlap `universe` the most. Proves a key by measurement."""
    best: tuple[float, Col | None] = (0.0, None)
    for c in cols:
        vals = [
            str(r[0]).strip()
            for r in conn.execute(f"SELECT DISTINCT {_q(c.sql)} FROM {_q(table.name)} WHERE {_q(c.sql)} IS NOT NULL")
        ]
        if not vals:
            continue
        ratio = sum(v in universe for v in vals) / len(vals)
        if ratio > best[0]:
            best = (ratio, c)
    return best[1] if best[0] >= min_ratio else None


# ---------------------------------------------------------------------------
# Catalogs
# ---------------------------------------------------------------------------

_CATEGORY_HEADERS = {
    "nhom hang", "nhom hang hoa", "nhom hang 3 cap", "nhom hang(3 cap)", "danhmuc", "phan loai", "loai hang",
    "nhom", "loai",
}
_NAME_HEADERS = {"tengoi", "ten hang", "ten san pham", "ten mat hang", "ten sp", "item", "san pham", "hang", "dien giai"}
_BASE_UNIT_HEADERS = {"donvicoso", "dvt", "don vi", "don vi tinh", "dv goc", "don vi goc", "don vi co ban"}


class Catalog:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.name_to_key: dict[str, str] = {}
        self.code_to_key: dict[str, str] = {}
        # Items from true product lists. A conversion table names only items that have conversions.
        self.listed_keys: set[str] = set()

    def get_or_create(self, code: str | None, name: str | None) -> str | None:
        code_u = code.strip().upper() if code and code.strip() else None
        name_k = fold_key(name) if name and name.strip() else None
        key = None
        if code_u and code_u in self.code_to_key:
            key = self.code_to_key[code_u]
        elif name_k and name_k in self.name_to_key:
            key = self.name_to_key[name_k]
        if key is None:
            key = code_u or name_k
            if key is None:
                return None
            self.items[key] = {
                "item_key": key, "item_code": code_u, "item_name": name, "category": None, "base_unit": None,
                "cost": None, "price": None, "is_rx": None, "ingredient": None, "brand": None, "size": None,
                "active": None,
            }
        item = self.items[key]
        if code_u:
            self.code_to_key[code_u] = key
            item["item_code"] = item["item_code"] or code_u
        if name_k:
            self.name_to_key.setdefault(name_k, key)
            item["item_name"] = item["item_name"] or name
        return key


_RX_TRUE = {"1", "true", "co", "x", "ke don", "yes", "rx", "dung"}


def _build_item_catalog(conn: sqlite3.Connection, tables: list[Table], line_codes: set[str]) -> Catalog:
    cat = Catalog()
    candidates = [
        t for t in tables
        if t.grain in {"catalog", "stock", "conversion", "other"}
        and not t.by(headers=("ngay ap dung", "ngayapdung"))
        and not re.search(r"khach|customer|nhan vien|\btho\b|ncc|nha cung|tra hang|bao hanh|chi phi|chi nhanh|thanh toan|ban hang$", t.key)
    ]
    # Tables with names first, so attribute-only tables can attach to existing items by code.
    candidates.sort(key=lambda t: t.role("item_name") is None and t.by(headers=_NAME_HEADERS) is None)
    for t in candidates:
        id_cols = t.roles("item_id") or [c for c in t.cols if c.h in {"ma sp", "id sp", "ma hh", "ma mon"}]
        code_col = None
        if id_cols:
            code_col = _best_overlap_col(conn, t, id_cols, line_codes, min_ratio=0.1) if line_codes else id_cols[0]
            code_col = code_col or (id_cols[0] if not line_codes else None)
        name_col = t.role("item_name") or t.by(headers=_NAME_HEADERS)
        if code_col is None and name_col is None:
            continue
        category = t.by(headers=_CATEGORY_HEADERS, glosses=("nhom hang", "loai hang"))
        base_unit = t.role("base_unit") or (t.role("unit") if t.grain != "conversion" else None) or t.by(headers=_BASE_UNIT_HEADERS)
        cost = t.role("cost_price") or t.by(headers=("giavonhang", "gia von", "gia von hien tai"))
        price = t.role("unit_price")
        rx = t.by(headers=("bantheodon", "ke don", "thuoc ke don"), glosses=("thuoc ke don",))
        ingredient = t.by(headers=("hoat chat", "thanhphanchinh"), glosses=("hoat chat",))
        brand = t.by(headers=("hang", "thuong hieu", "hang sx"), glosses=())
        size = t.by(headers=("so size", "so / size"), glosses=("hoac size",))
        active = t.by(headers=("conkinhdoanh", "dang kinh doanh", "duoc ban truc tiep"))
        if brand is name_col:
            brand = None
        if category is name_col:
            category = None
        if t.grain == "conversion" and name_col is None and code_col is None:
            continue
        for r in _rows(conn, t, {
            "code": code_col.sql if code_col else None, "name": name_col.sql if name_col else None,
            "category": category.sql if category else None, "base_unit": base_unit.sql if base_unit else None,
            "cost": cost.sql if cost else None, "price": price.sql if price else None, "rx": rx.sql if rx else None,
            "ingredient": ingredient.sql if ingredient else None, "brand": brand.sql if brand else None,
            "size": size.sql if size else None, "active": active.sql if active else None,
        }):
            key = cat.get_or_create(_clean(r["code"]), _clean(r["name"]))
            if key is None:
                continue
            if t.grain in {"catalog", "stock"}:
                cat.listed_keys.add(key)
            item = cat.items[key]
            for field in ("category", "ingredient", "brand", "size"):
                if not item[field] and _clean(r[field]):
                    item[field] = _clean(r[field])
            if not item["base_unit"] and _clean(r["base_unit"]):
                item["base_unit"] = _clean(r["base_unit"])
            if item["cost"] is None:
                item["cost"] = _money(r["cost"])
            if item["price"] is None:
                item["price"] = _money(r["price"])
            if rx is not None and _clean(r["rx"]) is not None:
                item["is_rx"] = 1 if fold_key(str(r["rx"])) in _RX_TRUE else 0
            if active is not None and _clean(r["active"]) is not None:
                item["active"] = 0 if fold_key(str(r["active"])) in {"0", "false", "khong", "ngung", "ngung kinh doanh"} else 1
    return cat


def _load_customers(conn: sqlite3.Connection, tables: list[Table]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for t in tables:
        if t.grain not in {"catalog", "other"} or not re.search(r"khach", t.key):
            continue
        code = t.role("customer_id")
        name = t.role("customer") or t.by(headers=("tengoi", "ten kh", "ten khach hang", "khach le si"), glosses=("ten khach",))
        if code is None and name is None:
            continue
        phone = t.role("phone")
        group = t.by(headers=("nhom khach hang", "loaikhachhang", "nhom", "loai khach", "nhom khach", "hang", "hang khach"), glosses=("nhom khach",))
        registered = t.by(headers=("ngay dang ky",), glosses=("ngay dang ky",))
        for r in _rows(conn, t, {
            "code": code.sql if code else None, "name": name.sql if name else None,
            "phone": phone.sql if phone else None, "group": group.sql if group else None,
            "reg": (registered.sql + "__norm") if registered and registered.role in {"date", "secondary_date"} else None,
        }):
            c = _clean(r["code"])
            n = _clean(r["name"])
            key = c.upper() if c else (fold_key(n) if n else None)
            if key:
                out[key] = {
                    "customer_key": key, "code": c, "name": n, "phone": _clean(r["phone"]),
                    "grp": _clean(r["group"]), "registered": _clean(r["reg"]),
                }
    return out


_STAFF_HEADERS = {"nguoi ban", "ma nv ban", "nhan vien ban", "manhanvien", "tho", "nv ban", "ma nv", "thu ngan", "nhan vien"}
_CHANNEL_HEADERS = {"kenh", "kenh ban", "nguon", "ban qua", "kenhbanhang", "kenh dat", "nguon don", "kenh ban hang"}
_METHOD_HEADERS = {"phuong thuc thanh toan", "phuongthucthanhtoan", "phuong thuc tt", "thanh toan bang", "hinh thuc tt", "pttt", "loai tt", "phuongthuctt", "hinh thuc thanh toan"}
_GROUP_HEADERS = {"nhom khach hang", "nhom khach"}
_MAPPED_HEADERS = _STAFF_HEADERS | _CHANNEL_HEADERS | _METHOD_HEADERS | _GROUP_HEADERS | {"phuong thuc", "phuongthuc"}
_GOODS_GLOSS = ("tien hang", "tong tien hang", "gia tri hang")
_GOODS_HEADERS = {"tong tien hang", "tongtienhang", "tien hang truoc giam", "tien hang", "gia tri hang"}


_PAYABLE_HEADERS = {"tong thanh toan", "tongthanhtoan", "tong sau giam gia", "phai thu", "tong sau giam"}


def _order_money_cols(t: Table) -> tuple[Col | None, Col | None, list[Col]]:
    """Goods total, payable total, and totals whose meaning is not stated. Column order never decides."""
    totals = t.roles("order_total")
    goods = next((c for c in totals if c.h in _GOODS_HEADERS or any(g in c.g for g in _GOODS_GLOSS)), None)
    others = [c for c in totals if c is not goods]
    payable = next((c for c in others if c.g or c.h in _PAYABLE_HEADERS), None)
    return goods, payable, [c for c in others if c is not payable]


def _classify_ambiguous_totals(orders: dict[str, dict[str, Any]], line_problems: dict[str, int]) -> None:
    """Use canonical line components as evidence for totals with no stated meaning.

    A column is goods or payable only if it equals that value exactly on every order, on evidence that includes
    orders where the two differ. Otherwise it stays unresolved, and debt calculations that need it abstain.
    """
    columns: dict[str, list[dict[str, Any]]] = {}
    for o in orders.values():
        for col in o["ambiguous_totals"]:
            columns.setdefault(col, []).append(o)
    assignments: dict[str, str] = {}
    for col, seen in columns.items():
        usable = [o for o in seen if o["has_lines"] and o["amount_seen"]]
        if not usable:
            line_problems["payable_unresolved"] = 1
            continue
        goods_ok = all(o["ambiguous_totals"][col] == o["line_rev"] for o in usable)
        payable_ok = all(
            o["ambiguous_totals"][col] == o["all_amount"] - (o["discount"] or 0) - (o["tradein"] or 0) + (o["shipping"] or 0)
            for o in usable
        )
        if goods_ok != payable_ok:
            assignments[col] = "goods" if goods_ok else "payable"
        else:
            line_problems["payable_unresolved"] = 1
    for col, meaning in assignments.items():
        for o in columns[col]:
            if o[meaning] is None:
                o[meaning] = o["ambiguous_totals"][col]


def _time_col(conn: sqlite3.Connection, t: Table) -> Col | None:
    """Find the column carrying time of day: an explicit time role, or a timestamp in a date column."""
    explicit = t.role("time") or t.by(glosses=("gio dat don", "gio tao don"))
    if explicit is not None:
        return explicit
    # A creation timestamp is the order-creation time the shift and peak-hour rules ask about.
    for c in sorted(t.roles("date", "secondary_date"), key=lambda col: "tao" not in col.h):
        sample = conn.execute(
            f"SELECT {_q(c.sql)} FROM {_q(t.name)} WHERE {_q(c.sql)} IS NOT NULL LIMIT 200"
        ).fetchall()
        with_time = sum(1 for (v,) in sample if _time_of(v) not in (None, "00:00:00"))
        if sample and with_time >= len(sample) * 0.5:
            return c
    return None


def _time_of(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    try:
        f = float(s)
    except ValueError:
        return _sql_extract_time(s)
    frac = f % 1.0
    if frac == 0.0 and f >= 1:
        return None
    secs = int(round(frac * 86400)) % 86400
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"


# ---------------------------------------------------------------------------
# Orders and lines
# ---------------------------------------------------------------------------


def _build_orders_and_lines(
    conn: sqlite3.Connection,
    tables: list[Table],
    catalog: Catalog,
    customers: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    unit_evidence: dict[str, dict[str, dict[str, Any]]],
    line_problems: dict[str, int],
) -> tuple[dict[str, dict[str, Any]], list[tuple[Any, ...]], dict[str, str]]:
    orders: dict[str, dict[str, Any]] = {}
    has_real_catalog = bool(catalog.listed_keys)
    raw_to_key: dict[str, str] = {}
    sales_tables = [t for t in tables if t.grain == "sales_line"]
    header_tables = [t for t in tables if t.grain == "order_header"]
    has_codes = False
    meta_flags = dict(conn.execute("SELECT key, value FROM _business_metadata").fetchall())
    # Real order numbers identify one order across monthly sheets: a late return row can sit in the next
    # month's sheet and still belongs to the original order. Only a per-sheet STT restarts.
    ids_restart_per_sheet = meta_flags.get("stt_is_order_id") == "1"
    status_rank = {"CANCELLED": 3, "RETURNED": 2, "SHIPPING": 1, "COMPLETED": 0}

    def order_fields(t: Table) -> dict[str, str | None]:
        goods, payable, ambiguous = _order_money_cols(t)
        time_c = _time_col(conn, t)
        staff = t.by(headers=_STAFF_HEADERS, glosses=("nhan vien ban", "tho lam"))
        channel = t.by(headers=_CHANNEL_HEADERS, glosses=("kenh ban",))
        method = t.by(headers=_METHOD_HEADERS, glosses=("hinh thuc thanh toan",))
        group = t.by(headers=_GROUP_HEADERS, glosses=("nhom khach",))
        tradein = t.by(headers=("thu may cu",), glosses=("tien thu may cu",))
        return {
            "_order_id": "_order_id", "_raw_order_id": "_raw_order_id", "_order_date": "_order_date",
            "_effective_order_status": "_effective_order_status",
            "_customer_id": "_customer_id", "_customer_name": "_customer_name", "_customer_raw": "_customer_raw",
            "_is_walk_in": "_is_walk_in",
            "goods": (goods.sql + "__norm") if goods else None,
            "payable": (payable.sql + "__norm") if payable else None,
            "_order_discount": "_order_discount", "_shipping_fee": "_shipping_fee",
            "_paid_amount": "_paid_amount", "_debt_amount": "_debt_amount",
            "time": time_c.sql if time_c else None, "staff": staff.sql if staff else None,
            "channel": channel.sql if channel else None, "method": method.sql if method else None,
            "group": group.sql if group else None, "tradein": tradein.sql if tradein else None,
            **{f"ambiguous::{c.h}": c.sql + "__norm" for c in ambiguous},
        }

    def absorb(rec: dict[str, Any], source_has_lines: bool) -> str | None:
        nonlocal has_codes
        key = rec["_order_id"]
        if key is None:
            return None
        raw_id = _clean(rec["_raw_order_id"])
        if raw_id and not ids_restart_per_sheet and not raw_id.startswith("__BLANK_ORD_"):
            key = raw_id
        o = orders.get(key)
        if o is None:
            o = {
                "order_key": key, "raw_id": _clean(rec["_raw_order_id"]), "order_date": None, "order_time": None,
                "status": None, "customer_code": None, "customer_name": None, "customer_label": None,
                "customer_group": None, "staff": None, "channel": None, "payment_method": None, "goods": None,
                "payable": None, "discount": None, "shipping": None, "tradein": None, "paid_recorded": None,
                "debt_recorded": None, "walk_in": 1, "has_lines": 0, "line_rev": 0, "all_amount": 0, "amount_seen": False, "ambiguous_totals": {},
            }
            orders[key] = o
            if o["raw_id"]:
                raw_to_key.setdefault(o["raw_id"], key)
        if rec["_order_date"] and (o["order_date"] is None or rec["_order_date"] < o["order_date"]):
            o["order_date"] = rec["_order_date"]
        new_status = rec["_effective_order_status"]
        if new_status and (o["status"] is None or status_rank.get(new_status, 0) > status_rank.get(o["status"], 0)):
            o["status"] = new_status
        o["order_time"] = o["order_time"] or _time_of(rec["time"])
        code = _clean(rec["_customer_id"])
        if code:
            has_codes = True
            o["customer_code"] = o["customer_code"] or code
        if rec["_customer_name"] and not rec["_is_walk_in"]:
            o["customer_name"] = o["customer_name"] or rec["_customer_name"]
            o["customer_label"] = o["customer_label"] or rec["_customer_raw"]
        if not rec["_is_walk_in"]:
            o["walk_in"] = 0
        for field, src in (
            ("customer_group", "group"), ("staff", "staff"), ("channel", "channel"), ("payment_method", "method"),
        ):
            o[field] = o[field] or _clean(rec[src])
        for field, src in (
            ("goods", "goods"), ("payable", "payable"), ("discount", "_order_discount"),
            ("shipping", "_shipping_fee"), ("paid_recorded", "_paid_amount"), ("debt_recorded", "_debt_amount"),
        ):
            if o[field] is None and rec[src] is not None:
                o[field] = rec[src]
        if o["tradein"] is None:
            o["tradein"] = _money(rec["tradein"])
        for name, value in rec.items():
            if name.startswith("ambiguous::") and value is not None:
                o["ambiguous_totals"].setdefault(name, value)
        return key

    for t in header_tables:
        fields = order_fields(t)
        for rec in _rows(conn, t, fields):
            absorb(rec, False)

    lines: list[tuple[Any, ...]] = []
    for t in sales_tables:
        has_date = t.role("date") is not None
        has_amount = t.role("line_amount") is not None
        category_col = t.by(headers=("nhom hang", "loai hang", "phan loai", "nhom hang hoa"), glosses=("nhom hang",))
        preconverted = t.role("base_qty")
        fields = order_fields(t)
        fields.update({
            "_row_role": "_row_role", "_item_id": "_item_id", "_item_name": "_item_name", "_qty": "_qty",
            "_unit": "_unit", "_base_qty": "_base_qty", "_unit_price": "_unit_price", "_line_amount": "_line_amount",
            "_line_discount": "_line_discount", "_cost_price": "_cost_price",
            "_is_derived_component": "_is_derived_component",
            "category": category_col.sql if category_col else None,
            "preconverted": (preconverted.sql + "__norm") if preconverted else None,
        })
        for rec in _rows(conn, t, fields):
            raw = _clean(rec["_raw_order_id"])
            if has_date or (raw and raw not in raw_to_key):
                key = absorb(rec, True)
            else:
                key = raw_to_key.get(raw) if raw else None
            if key is None:
                continue
            o = orders[key]
            o["has_lines"] = 1
            amount = rec["_line_amount"]
            role = rec["_row_role"] or "ITEM"
            if amount is not None:
                o["amount_seen"] = True
                o["all_amount"] += amount
                if role in {"ITEM", "SERVICE_FEE"} and not ((rec["_qty"] or 0) < 0):
                    o["line_rev"] += amount
            elif not has_amount and rec["goods"] is None and rec["payable"] is not None and o["goods"] is None:
                o["goods"] = rec["payable"]
            if has_amount and amount is None and role in {"ITEM", "SERVICE_FEE"}:
                line_problems["missing_amount"] = line_problems.get("missing_amount", 0) + 1
            if rec["_item_id"] is None and rec["_item_name"] is None:
                continue
            label = _clean(rec["_item_name"])
            canonical = aliases.get(fold_key(label)) if label else None
            item_key = catalog.get_or_create(_clean(rec["_item_id"]), canonical or label)
            if item_key is None:
                continue
            item = catalog.items[item_key]
            # Line tables keyed by code have no name at ingestion time; classify again with the catalog name.
            if label is None and role == "ITEM" and item["item_name"]:
                role = classify_sales_row_role(item["item_name"], amount, None)
                if role not in {"ITEM", "SERVICE_FEE"} and amount is not None:
                    o["line_rev"] -= amount
            if not item["category"] and _clean(rec["category"]):
                item["category"] = _clean(rec["category"])
            if role == "ITEM":
                if has_real_catalog and item_key not in catalog.listed_keys:
                    unit_evidence.setdefault(item_key, {})["__identity__"] = {"unresolved": True}
                unit_name = fold_key(_clean(rec["_unit"]) or "")
                seen = unit_evidence.setdefault(item_key, {}).setdefault(
                    unit_name, {"plain": 0, "pre": 0, "pre_missing": 0, "pre_ratios": set()}
                )
                if preconverted is None:
                    seen["plain"] += 1
                elif rec["preconverted"] is None:
                    seen["pre_missing"] += 1
                else:
                    seen["pre"] += 1
                    if rec["_qty"]:
                        seen["pre_ratios"].add(round(rec["preconverted"] / rec["_qty"], 6))
            lines.append((
                key, role, item_key, item["item_code"], item["item_name"], None, rec["_qty"], _clean(rec["_unit"]),
                rec["_base_qty"], None, rec["_unit_price"], amount, rec["_line_discount"], rec["_cost_price"], label,
            ))

    _classify_ambiguous_totals(orders, line_problems)
    for o in orders.values():
        code = o["customer_code"]
        if has_codes:
            ckey = code.upper() if code else None
        else:
            ckey = fold_key(o["customer_name"]) if (o["customer_name"] and not o["walk_in"]) else None
        o["customer_key"] = ckey
        info = customers.get(ckey) if ckey else None
        if info:
            o["customer_name"] = info["name"] or o["customer_name"]
            o["customer_group"] = o["customer_group"] or info["grp"]
        if ckey is None:
            o["customer_group"] = None  # Rulebook §1: walk-in orders belong to no group.
        # A line table with no readable amount proves nothing about revenue: leave it unknown, never 0.
        if o["has_lines"] and o["amount_seen"]:
            o["revenue"] = o["line_rev"] if (o["line_rev"] or o["goods"] is None) else o["goods"]
        else:
            o["revenue"] = o["goods"]
        if o["payable"] is None and o["has_lines"]:
            o["payable"] = (
                o["all_amount"] - (o["discount"] or 0) - (o["tradein"] or 0) + (o["shipping"] or 0)
            )
    return orders, lines, raw_to_key


# ---------------------------------------------------------------------------
# Payments, purchases, stock, lots, expenses, prices, deliveries
# ---------------------------------------------------------------------------


def _build_payments(conn, tables, raw_to_key) -> tuple[list[tuple], list[tuple]]:
    pays: list[tuple] = []
    dues: list[tuple] = []
    universe = set(raw_to_key)
    for t in tables:
        if t.grain != "payment":
            continue
        cand = t.roles("order_id") + [c for c in t.cols if c.role == "other" and c.h in _ORDER_FK_HINTS]
        fk = _best_overlap_col(conn, t, cand, universe) if cand else None
        if fk is None:
            continue
        amount = t.role("line_amount")
        if amount is not None and t.role("date") is not None:
            method = t.by(headers=_METHOD_HEADERS | {"phuong thuc", "phuongthuc"}, glosses=("hinh thuc thanh toan",))
            time_c = t.role("date")
            for r in _rows(conn, t, {
                "fk": fk.sql, "d": "_order_date", "a": "_line_amount", "m": method.sql if method else None,
                "t": time_c.sql,
            }):
                k = raw_to_key.get(_clean(r["fk"]) or "")
                if k and r["a"] is not None and r["d"]:
                    pays.append((k, r["d"], _time_of(r["t"]), r["a"], _clean(r["m"])))
        due = t.by(headers=("han thanh toan", "han tra", "ngay phai tra", "han no"), glosses=("han thanh toan", "ngay phai tra"))
        if due is not None and amount is None:
            for r in _rows(conn, t, {"fk": fk.sql, "d": due.sql + "__norm" if due.role in {"date", "secondary_date"} else due.sql}):
                k = raw_to_key.get(_clean(r["fk"]) or "")
                if k and r["d"]:
                    dues.append((k, _date(r["d"])))
    return pays, dues


_RECEIPT_HEADERS = {"ma phieu nhap", "maphieunhap", "so phieu", "ma nhap hang", "ma don nhap", "ma phieu"}
_SUPPLIER_HEADERS = {"nha cung cap", "ma ncc", "ma nha cung cap", "manhacungcap", "ncc"}


def _build_purchases(conn, tables, catalog: Catalog, aliases, rules):
    suppliers: dict[str, str] = {}
    for t in tables:
        if t.grain in {"other", "catalog"} and re.search(r"nha cung cap|ncc", t.key):
            code = t.by(headers=_SUPPLIER_HEADERS | {"ma"})
            name = t.by(headers=("tengoi", "ten", "ten ncc", "ten nha cung cap"), glosses=("ten nha cung cap",))
            if code and name:
                for r in _rows(conn, t, {"c": code.sql, "n": name.sql}):
                    if _clean(r["c"]) and _clean(r["n"]):
                        suppliers[_clean(r["c"]).upper()] = _clean(r["n"])

    purchase_tables = [t for t in tables if t.grain in {"purchase_header", "purchase_line"} and "thu may" not in t.key and "thumay" not in t.compact_key]
    receipts: dict[str, dict[str, Any]] = {}
    plines: list[tuple] = []

    def receipt_col(t: Table) -> Col | None:
        return t.by(headers=_RECEIPT_HEADERS) or t.role("order_id")

    for t in sorted(purchase_tables, key=lambda x: x.grain != "purchase_header"):
        rc = receipt_col(t)
        if rc is None:
            continue
        date_c = t.role("date")
        sup = t.by(headers=_SUPPLIER_HEADERS, glosses=("nha cung cap",))
        status = t.role("purchase_status", "entity_status", "status")
        total = t.role("order_total")
        is_line = t.grain == "purchase_line" or t.role("qty", "base_qty") is not None
        for r in _rows(conn, t, {
            "rid": rc.sql, "d": "_order_date" if date_c else None, "sup": sup.sql if sup else None,
            "st": status.sql if status else None, "tot": (total.sql + "__norm") if total else None,
            "notes": (t.role("notes").sql if t.role("notes") else None),
        }):
            rid = _clean(r["rid"])
            if rid is None:
                continue
            rec = receipts.setdefault(rid, {
                "receipt_key": rid, "receipt_date": None, "supplier_key": None, "supplier_name": None, "amount": None,
                "line_sum": 0, "status": None, "cancelled": 0,
            })
            rec["receipt_date"] = rec["receipt_date"] or r["d"]
            if sup is not None and _clean(r["sup"]):
                s = _clean(r["sup"])
                rec["supplier_key"] = rec["supplier_key"] or fold_key(s).upper()
                rec["supplier_name"] = rec["supplier_name"] or suppliers.get(s.upper(), s)
            if _clean(r["st"]):
                rec["status"] = rec["status"] or _clean(r["st"])
                if "huy" in fold(_clean(r["st"])) or "cancel" in fold(_clean(r["st"])):
                    rec["cancelled"] = 1
            if _clean(r["notes"]) and re.search(r"\bhuy\b|cancel", fold(_clean(r["notes"]))):
                rec["cancelled"] = 1
            if r["tot"] is not None and rec["amount"] is None:
                rec["amount"] = r["tot"]
        if not is_line:
            continue
        item_id = t.role("item_id")
        item_name = t.role("item_name")
        amount = t.role("line_amount")
        lot = t.by(headers=("ma lo", "malo", "lo", "so lo"), glosses=("so lo",))
        expiry = t.by(headers=("han su dung", "hansudung", "hsd"), glosses=("han su dung", "han dung"))
        for r in _rows(conn, t, {
            "rid": rc.sql, "d": "_order_date" if date_c else None, "code": item_id.sql if item_id else None,
            "name": item_name.sql if item_name else None, "qty": "_qty", "unit": "_unit", "base": "_base_qty",
            "price": "_unit_price", "amt": "_line_amount" if amount else None, "lot": lot.sql if lot else None,
            "exp": (expiry.sql) if expiry else None,
        }):
            rid = _clean(r["rid"])
            if rid is None:
                continue
            label = _clean(r["name"])
            canonical = aliases.get(fold_key(label)) if label else None
            key = catalog.get_or_create(_clean(r["code"]), canonical or label)
            qty = r["qty"] if r["qty"] is not None else r["base"]
            amt = r["amt"]
            if amt is None and qty is not None and r["price"] is not None:
                amt = int(Decimal(str(qty)) * r["price"])
            if amt is not None:
                receipts[rid]["line_sum"] += amt
            plines.append((
                rid, receipts[rid]["receipt_date"] or r["d"], key, label or (catalog.items[key]["item_name"] if key else None),
                qty, _clean(r["unit"]), r["base"], r["price"], amt, _clean(r["lot"]), _date(r["exp"]),
            ))
    rows = []
    for rec in receipts.values():
        amount = rec["amount"] if rec["amount"] is not None else (rec["line_sum"] or None)
        rows.append((
            rec["receipt_key"], rec["receipt_date"], rec["supplier_key"], rec["supplier_name"], amount, rec["status"],
            rec["cancelled"],
        ))
    # Lines can precede their header date only when tables were processed out of order; settle here.
    dates = {r[0]: r[1] for r in rows}
    plines = [(p[0], dates.get(p[0]) or p[1], *p[2:]) for p in plines]
    return rows, plines


def _build_stock(conn, tables, catalog: Catalog, aliases):
    moves: list[tuple] = []
    snapshot: dict[str, float] = {}
    lots: list[tuple] = []
    allocs: list[tuple] = []
    for t in tables:
        if t.grain == "lot_allocation":
            continue
        stock_c = t.role("stock_qty")
        if stock_c is None and not (t.grain == "stock" and t.by(headers=("hsd", "hansudung", "han su dung"))):
            continue
        code = t.role("item_id") or t.by(headers=("ma sp", "id sp", "ma hh", "ma mon"))
        name = t.role("item_name") or t.by(headers=_NAME_HEADERS)
        ts = t.by(headers=("ngaygiophatsinh", "ngay gio phat sinh", "thoi gian", "ngay"))
        lot = t.by(headers=("ma lo", "malo", "lo", "so lo"), glosses=("so lo",))
        expiry = t.by(headers=("han su dung", "hansudung", "hsd"), glosses=("han su dung", "han dung"))
        if stock_c is not None and ts is not None:
            for r in _rows(conn, t, {"c": code.sql if code else None, "n": name.sql if name else None, "t": ts.sql, "q": stock_c.sql + "__norm"}):
                key = catalog.get_or_create(_clean(r["c"]), aliases.get(fold_key(_clean(r["n"]) or "")) or _clean(r["n"]))
                if key and r["q"] is not None and r["t"]:
                    moves.append((key, str(r["t"]), r["q"]))
        elif stock_c is not None:
            for r in _rows(conn, t, {"c": code.sql if code else None, "n": name.sql if name else None, "q": stock_c.sql + "__norm"}):
                key = catalog.get_or_create(_clean(r["c"]), aliases.get(fold_key(_clean(r["n"]) or "")) or _clean(r["n"]))
                if key and r["q"] is not None:
                    snapshot[key] = r["q"]
        if lot is not None and expiry is not None and stock_c is None:
            base = t.role("base_qty")
            price = t.role("unit_price")
            for r in _rows(conn, t, {
                "c": code.sql if code else None, "n": name.sql if name else None, "lot": lot.sql, "exp": expiry.sql,
                "q": (base.sql + "__norm") if base else None, "p": (price.sql + "__norm") if price else None,
            }):
                key = catalog.get_or_create(_clean(r["c"]), _clean(r["n"]))
                if key and _clean(r["lot"]):
                    lots.append((_clean(r["lot"]), key, _date(r["exp"]), r["q"], r["p"], None))
    return moves, snapshot, lots, allocs


def _build_lot_allocs(conn, tables, raw_to_key):
    rows = []
    for t in tables:
        if t.grain != "lot_allocation":
            continue
        fk = _best_overlap_col(conn, t, t.roles("order_id"), set(raw_to_key)) if t.roles("order_id") else None
        lot = t.by(headers=("ma lo", "malo", "lo", "so lo"), glosses=("so lo",))
        base = t.role("base_qty")
        ret = t.role("return_qty")
        if fk is None or lot is None or base is None:
            continue
        for r in _rows(conn, t, {"fk": fk.sql, "lot": lot.sql, "q": base.sql + "__norm", "r": (ret.sql + "__norm") if ret else None}):
            k = raw_to_key.get(_clean(r["fk"]) or "")
            if k:
                rows.append((k, _clean(r["lot"]), r["q"] or 0, r["r"] or 0))
    return rows


def _build_expenses(conn, tables):
    rows = []
    for t in tables:
        if not re.search(r"chi phi|chiphi", t.key):
            continue
        if t.role("date") is None or t.role("line_amount") is None:
            continue
        cat = t.by(headers=("muc", "nhom", "loai", "khoan muc"))
        for r in _rows(conn, t, {"d": "_order_date", "a": "_line_amount", "c": cat.sql if cat else None}):
            if r["d"] and r["a"] is not None:
                rows.append((r["d"], r["a"], _clean(r["c"])))
    return rows


def _build_price_history(conn, tables, catalog: Catalog, aliases):
    rows = []
    for t in tables:
        eff = t.by(headers=("ngay ap dung", "ngayapdung"))
        if eff is None:
            continue
        code = t.role("item_id") or t.by(headers=("ma sp", "ma hh"))
        name = t.role("item_name") or t.by(headers=_NAME_HEADERS)
        new = t.by(headers=("gia moi", "giabanle", "gia ban", "gia"))
        old = t.by(headers=("gia cu",))
        if new is None:
            continue
        for r in _rows(conn, t, {
            "c": code.sql if code else None, "n": name.sql if name else None, "e": eff.sql,
            "p": new.sql, "o": old.sql if old else None,
        }):
            key = catalog.get_or_create(_clean(r["c"]), aliases.get(fold_key(_clean(r["n"]) or "")) or _clean(r["n"]))
            if key and _date(r["e"]):
                rows.append((key, _date(r["e"]), _money(r["p"]), _money(r["o"])))
    return rows


def _build_deliveries(conn, tables, raw_to_key):
    rows = set()
    for t in tables:
        if t.grain != "delivery":
            continue
        fk = t.role("order_id")
        batch = t.by(headers=("lan giao", "luot giao", "dot giao"), glosses=("dot giao",))
        if fk is None or batch is None or t.role("date") is None:
            continue
        for r in _rows(conn, t, {"fk": fk.sql, "b": batch.sql, "d": "_order_date"}):
            raw = _clean(r["fk"])
            if raw and r["d"]:
                rows.add((raw_to_key.get(raw, raw), _clean(r["b"]), r["d"]))
    return sorted(rows, key=str)


_PLATE_RE = re.compile(r"\b(\d{2}[a-z]\d?[-\s]?\d{3}[.\-]?\d{2})\b", re.IGNORECASE)


def _build_service(conn, tables):
    """A repair ledger: vehicle, jobs, mechanic and money on one row, with warranty marked in the notes."""
    rows = []
    for t in tables:
        vehicle = t.by(headers=("xe",), glosses=("bien so",))
        jobs = t.by(headers=("lam gi",), glosses=("cac viec da lam",))
        mechanic = t.by(headers=("tho",), glosses=("tho lam",))
        amount = t.role("order_total", "line_amount")
        notes = t.role("notes")
        if not (vehicle and jobs and mechanic and amount) or t.role("date") is None:
            continue
        for r in _rows(conn, t, {
            "d": "_order_date", "v": vehicle.sql, "j": jobs.sql, "m": mechanic.sql,
            "a": amount.sql + "__norm", "n": notes.sql if notes else None,
        }):
            v = _clean(r["v"]) or ""
            m = _PLATE_RE.search(v)
            plate = fold_key(m.group(1)).replace(" ", "") if m else None
            model = fold_key(v[: m.start()]) if m else fold_key(v)
            warranty = 1 if "bao hanh" in fold_key(_clean(r["n"]) or "") or "bao hanh" in fold_key(_clean(r["j"]) or "") else 0
            rows.append((r["d"], plate, model, fold_key(_clean(r["j"]) or ""), _clean(r["m"]), r["a"], warranty))
    return rows


def _load_unit_rules(conn, catalog: Catalog, aliases) -> list[tuple[str, str, float]]:
    out: dict[tuple[str, str], float] = {}
    for name, code, from_unit, factor in conn.execute("SELECT item_name, item_code, from_unit, factor FROM _unit_rules"):
        canonical = aliases.get(fold_key(name)) if name else None
        key = catalog.get_or_create(code, canonical or name)
        if key and from_unit:
            out[(key, from_unit)] = float(factor)
    return [(k, u, f) for (k, u), f in out.items()]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_OPAQUE_HEADER_RE = re.compile(r"^(?:[a-z]{1,2}|[a-z]{1,3}\d+|col \d+|cot \d+|field \d*)$")


def _unit_issues(catalog: Catalog, evidence: dict[str, dict[str, dict[str, Any]]], rules: list[tuple]) -> list[tuple[str, str, str]]:
    """Quantity is usable only with one base unit and a complete, consistent path from each sold unit."""
    item_rules: dict[str, dict[str, set[tuple[float, str]]]] = {}
    for key, unit, factor, to_unit in rules:
        item_rules.setdefault(key, {}).setdefault(unit, set()).add((factor, to_unit))

    issues: list[tuple[str, str, str]] = []
    for key, by_unit in evidence.items():
        if by_unit.pop("__identity__", None):
            issues.append((key, "", "Item label not found in the catalog"))
        base = fold_key(catalog.items[key]["base_unit"] or "") or None
        rules_here = item_rules.get(key, {})
        sold_units = [u for u in by_unit if u]
        for unit, seen in by_unit.items():
            candidates = rules_here.get(unit, set())
            factors = {f for f, _ in candidates}
            if len(factors) > 1:
                issues.append((key, unit, f"Conflicting conversion factors {sorted(factors)}"))
                continue
            if seen["pre_missing"]:
                issues.append((key, unit, "Base-quantity column is missing on some rows"))
                continue
            if seen["pre"]:
                # Without a recorded unit, different rows may be different units, so ratios can vary.
                if unit and len(seen["pre_ratios"]) > 1:
                    issues.append((key, unit, "Base quantities imply several factors for one unit"))
                elif unit and factors and seen["pre_ratios"] and abs(next(iter(seen["pre_ratios"])) - next(iter(factors))) > 1e-6:
                    issues.append((key, unit, "Base quantity disagrees with the conversion rule"))
                continue
            if not seen["plain"] or not unit or unit == base:
                continue
            if candidates:
                targets = {to for _, to in candidates if to}
                if base and targets and base not in targets:
                    issues.append((key, unit, f"Conversion ends at {sorted(targets)}, not base unit {base}"))
                continue
            # Rulebook §10: a unit with no conversion row is the base unit, so only one such unit can exist.
            if base is None and len(sold_units) == 1:
                continue
            issues.append((key, unit, f"No conversion from {unit} to base unit {base}"))
    return issues


def _purchase_money_recorded(tables: list[Table]) -> bool:
    return any(
        t.roles("order_total", "line_amount", "unit_price") for t in tables if t.grain.startswith("purchase")
    )


def _is_unexplained_attribute(conn: sqlite3.Connection, t: Table, c: Col) -> bool:
    """No definition and no known meaning, with coded or category-like values."""
    if c.role != "other" or c.gloss or c.h in _MAPPED_HEADERS or c.h.replace(" ", "") in _MAPPED_HEADERS:
        return False
    if _OPAQUE_HEADER_RE.match(c.h):
        return True
    values = [
        str(v).strip() for (v,) in conn.execute(
            f"SELECT DISTINCT {_q(c.sql)} FROM {_q(t.name)} WHERE {_q(c.sql)} IS NOT NULL LIMIT 31"
        )
    ]
    return 2 <= len(values) <= 30 and not any(any(ch.isdigit() for ch in v) for v in values)


def _health_problems(
    conn: sqlite3.Connection,
    tables: list[Table],
    orders: dict[str, dict[str, Any]],
    receipts: list[tuple],
    line_problems: dict[str, int],
) -> dict[str, str]:
    """Unresolved evidence per capability. Each entry blocks only the calculations that depend on it."""
    problems: dict[str, str] = {}
    if not tables:
        problems["global"] = "No source tables were identified"
        return problems
    grain_of = {t.name: t.grain for t in tables}
    sales_tables = [t for t in tables if t.grain in {"sales_line", "order_header"}]
    rows_in = lambda t: conn.execute(f"SELECT COUNT(*) FROM {_q(t.name)}").fetchone()[0]

    if not orders and any(t.grain == "other" and rows_in(t) for t in tables):
        reason = "Tables exist but no sales table could be identified"
        problems.update(orders=reason, status=reason, amount=reason, lines=reason)

    undated = sum(1 for o in orders.values() if not o["order_date"])
    if undated:
        problems["orders"] = f"{undated} orders have no usable date"
    for t in sales_tables:
        documented = any(c.gloss for c in t.cols)
        if t.role("order_id") is None and t.grain == "sales_line" and not documented and rows_in(t):
            problems.setdefault("orders", f"Order identity is unresolved in {t.name}")

    no_revenue = sum(1 for o in orders.values() if o["revenue"] is None)
    if no_revenue:
        problems["amount"] = f"{no_revenue} orders have no readable goods amount"
    if line_problems.get("missing_amount"):
        problems["amount"] = f"{line_problems['missing_amount']} sold lines have no readable amount"

    if line_problems.get("payable_unresolved"):
        problems["payable"] = "A total column could be goods or payable, and no evidence separates them"

    unresolved_status = sum(1 for o in orders.values() if o["status"] not in {"COMPLETED", "SHIPPING", "CANCELLED", "RETURNED"})
    if unresolved_status:
        problems["status"] = f"{unresolved_status} orders have an unresolved status"
    if orders and not any(t.roles("status", "notes") for t in sales_tables):
        problems["status"] = "No order status or notes column was identified"

    for table, delta_rows in conn.execute(
        "SELECT logical_table, COUNT(*) FROM _subtotal_reconciliation WHERE delta != 0 GROUP BY logical_table"
    ).fetchall():
        grain = grain_of.get(table, "")
        if grain in {"sales_line", "order_header"}:
            problems.setdefault("amount", f"{delta_rows} subtotals do not reconcile in {table}")
        elif grain.startswith("purchase"):
            problems.setdefault("purchases", f"{delta_rows} subtotals do not reconcile in {table}")

    table_of_source = dict(conn.execute("SELECT source_file || '|' || source_sheet, logical_table FROM _ingested_tables").fetchall())
    for source, code, count in conn.execute(
        "SELECT source_file || '|' || source_sheet, code, COUNT(*) FROM _ingestion_diagnostics WHERE severity = 'error' GROUP BY 1, 2"
    ).fetchall():
        grain = grain_of.get(table_of_source.get(source, ""), "")
        if grain in {"sales_line", "order_header"}:
            problems.setdefault("amount", f"{count} {code} diagnostics in sales data")
        elif grain.startswith("purchase"):
            problems.setdefault("purchases", f"{count} {code} diagnostics in purchase data")

    active = [r for r in receipts if not r[6]]
    if any(r[1] is None for r in active):
        problems["purchases"] = "Some purchase receipts have no usable date"
    elif any(r[4] is None for r in active) and _purchase_money_recorded(tables):
        problems["purchases"] = "Some purchase receipts have no usable amount"

    # A column nobody explained may hold what a question asks about, so its presence blocks "not recorded".
    unexplained = [
        c.header for t in sales_tables + [
            # A reference table the README documents has its meaning stated; only an undocumented one is open.
            t for t in tables
            if re.search(r"khach", t.key) or (t.grain in {"catalog", "stock", "conversion"} and not any(c.gloss for c in t.cols))
        ]
        for c in t.cols if _is_unexplained_attribute(conn, t, c)
    ]
    if unexplained:
        problems["attributes"] = "Unexplained columns could hold the attribute: " + ", ".join(sorted(set(unexplained))[:8])

    for t in tables:
        if t.grain == "lot_allocation" and t.role("base_qty") is None and rows_in(t):
            problems["lots"] = f"Sold quantities per lot are unresolved in {t.name}"
    return problems


def build_semantic_tables(db_path: str | Path) -> None:
    """(Re)build every `sem_*` table in a finished ingestion database."""
    conn = sqlite3.connect(str(db_path))
    try:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sem\\_%' ESCAPE '\\'").fetchall():
            conn.execute(f'DROP TABLE "{name}"')
        conn.executescript(_SEM_SCHEMA)
        tables = load_tables(conn)
        aliases = dict(conn.execute("SELECT alias_norm, canonical_name FROM _item_aliases").fetchall())
        aliases = {fold_key(k): v for k, v in aliases.items()}

        line_codes: set[str] = set()
        for t in tables:
            if t.grain == "sales_line" and t.role("item_id"):
                line_codes |= {str(r[0]).strip() for r in conn.execute(f'SELECT DISTINCT "_item_id" FROM {_q(t.name)} WHERE "_item_id" IS NOT NULL')}

        catalog = _build_item_catalog(conn, tables, line_codes)
        customers = _load_customers(conn, tables)
        unit_evidence: dict[str, dict[str, dict[str, Any]]] = {}
        line_problems: dict[str, int] = {}
        orders, lines, raw_to_key = _build_orders_and_lines(conn, tables, catalog, customers, aliases, unit_evidence, line_problems)
        pays, dues = _build_payments(conn, tables, raw_to_key)
        receipts, plines = _build_purchases(conn, tables, catalog, aliases, None)
        moves, snapshot, lots, _ = _build_stock(conn, tables, catalog, aliases)
        allocs = _build_lot_allocs(conn, tables, raw_to_key)
        expenses = _build_expenses(conn, tables)
        prices = _build_price_history(conn, tables, catalog, aliases)
        deliveries = _build_deliveries(conn, tables, raw_to_key)
        units = _load_unit_rules(conn, catalog, aliases)

        for o in orders.values():
            o["eligible"] = 1 if o["status"] in REVENUE_STATES else 0
        conn.executemany(
            "INSERT OR REPLACE INTO sem_orders VALUES (" + ",".join(["?"] * 22) + ")",
            [
                (
                    o["order_key"], o["raw_id"], o["order_date"], o["order_time"], o["status"], o["eligible"],
                    o["customer_key"], o["customer_code"], o["customer_name"], o["customer_label"], o["customer_group"],
                    o["staff"], o["channel"], o["payment_method"], o["revenue"], o["payable"], o["discount"],
                    o["shipping"], o["tradein"], o["paid_recorded"], o["debt_recorded"], o["has_lines"],
                )
                for o in orders.values()
            ],
        )
        conn.executemany("INSERT OR REPLACE INTO sem_customers VALUES (?,?,?,?,?,?)", [
            (c["customer_key"], c["code"], c["name"], c["phone"], c["grp"], c["registered"]) for c in customers.values()
        ])
        unit_map = {}
        for key, unit, factor in units:
            unit_map[(key, unit.lower())] = factor
        # Base unit: catalog first, then a conversion rule's target, then the unit sold at factor 1.
        for name, code, to_unit in conn.execute("SELECT item_name, item_code, to_unit FROM _unit_rules WHERE to_unit IS NOT NULL"):
            key = catalog.get_or_create(code, (aliases.get(fold_key(name)) or name) if name else None)
            if key and not catalog.items[key]["base_unit"]:
                catalog.items[key]["base_unit"] = to_unit
        sold_units: dict[str, dict[str, int]] = {}
        for ln in lines:
            if ln[7]:
                bucket = sold_units.setdefault(ln[2], {})
                bucket[ln[7]] = bucket.get(ln[7], 0) + 1
        for key, counts in sold_units.items():
            if not catalog.items[key]["base_unit"]:
                plain = {u: n for u, n in counts.items() if (key, u.lower()) not in unit_map}
                if plain:
                    catalog.items[key]["base_unit"] = max(sorted(plain), key=lambda u: plain[u])
        filled = []
        for ln in lines:
            item = catalog.items[ln[2]]
            filled.append((*ln[:5], item["category"], *ln[6:9], item["base_unit"], *ln[10:]))
        conn.executemany("INSERT INTO sem_lines VALUES (" + ",".join(["?"] * 15) + ")", filled)
        conn.executemany(
            "INSERT OR REPLACE INTO sem_items VALUES (" + ",".join(["?"] * 12) + ")",
            [tuple(i[k] for k in ("item_key", "item_code", "item_name", "category", "base_unit", "cost", "price", "is_rx", "ingredient", "brand", "size", "active")) for i in catalog.items.values()],
        )
        name_rows = {}
        for k, key in catalog.name_to_key.items():
            name_rows[k] = (k, key, catalog.items[key]["item_name"])
        for al, canon in aliases.items():
            target = catalog.name_to_key.get(fold_key(canon))
            if target:
                name_rows.setdefault(al, (al, target, catalog.items[target]["item_name"]))
        conn.executemany("INSERT OR REPLACE INTO sem_item_names VALUES (?,?,?)", list(name_rows.values()))
        conn.executemany("INSERT OR REPLACE INTO sem_units VALUES (?,?,?)", [(k, u, f) for (k, u), f in unit_map.items()])
        conn.executemany("INSERT INTO sem_payments VALUES (?,?,?,?,?)", pays)
        conn.executemany("INSERT INTO sem_due VALUES (?,?)", dues)
        conn.executemany("INSERT OR REPLACE INTO sem_purchases VALUES (?,?,?,?,?,?,?)", receipts)
        conn.executemany("INSERT INTO sem_purchase_lines VALUES (" + ",".join(["?"] * 11) + ")", plines)
        conn.executemany("INSERT INTO sem_stock_moves VALUES (?,?,?)", moves)
        conn.executemany("INSERT OR REPLACE INTO sem_stock_snapshot VALUES (?,?)", list(snapshot.items()))
        health_notes: dict[str, str] = {}
        # Each purchase line carrying a lot number is a lot (a separate lot table may add nothing more).
        lot_ids = {row[0] for row in lots}
        received = {p[9]: p[1] for p in plines if p[9]}
        lots = [(*row[:5], received.get(row[0])) for row in lots]
        factor_of = {(k, u): fac for (k, u), fac in unit_map.items()}
        for p in plines:
            if p[9] and p[9] not in lot_ids and p[2]:
                # Without a base-quantity column or a unit, the purchase quantity's unit is unknown when the
                # item has conversions. Record that instead of treating boxes as base units.
                if p[6] is None and not p[5] and any(k == p[2] for k, _ in factor_of):
                    health_notes["lots"] = "Purchase quantities per lot have no unit or base quantity"
                qty = p[6] if p[6] is not None else (p[4] * factor_of.get((p[2], (p[5] or "").lower()), 1) if p[4] is not None else None)
                price = (float(p[8]) / qty) if (p[8] is not None and qty) else None
                lots.append((p[9], p[2], p[10], qty, price, p[1]))
                lot_ids.add(p[9])
        conn.executemany("INSERT OR REPLACE INTO sem_lots VALUES (?,?,?,?,?,?)", lots)
        conn.executemany("INSERT INTO sem_lot_alloc VALUES (?,?,?,?)", allocs)
        conn.executemany("INSERT INTO sem_expenses VALUES (?,?,?)", expenses)
        conn.executemany("INSERT INTO sem_price_history VALUES (?,?,?,?)", prices)
        conn.executemany("INSERT INTO sem_deliveries VALUES (?,?,?)", deliveries)
        conn.executemany("INSERT INTO sem_service VALUES (?,?,?,?,?,?,?)", _build_service(conn, tables))

        for ddl in (
            "CREATE INDEX ix_so_date ON sem_orders(order_date)", "CREATE INDEX ix_so_cust ON sem_orders(customer_key)",
            "CREATE INDEX ix_sl_order ON sem_lines(order_key)", "CREATE INDEX ix_sl_item ON sem_lines(item_key)",
            "CREATE INDEX ix_sp_order ON sem_payments(order_key)", "CREATE INDEX ix_spl_item ON sem_purchase_lines(item_key)",
            "CREATE INDEX ix_ssm ON sem_stock_moves(item_key, moved_at)", "CREATE INDEX ix_sph ON sem_price_history(item_key, effective_date)",
        ):
            conn.execute(ddl)

        # Coverage: first observed order through the README's stated end date when given.
        readme = dict(conn.execute("SELECT key, value FROM _business_metadata").fetchall())
        start, end = conn.execute(
            "SELECT MIN(order_date), MAX(order_date) FROM sem_orders WHERE order_date IS NOT NULL"
        ).fetchone()
        coverage_end = readme.get("coverage_end_date") or end

        def count(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]

        caps = {
            "lines": count("SELECT COUNT(*) FROM sem_lines WHERE role='ITEM'") > 0,
            "order_time": count("SELECT COUNT(order_time) FROM sem_orders") > 0,
            "customer": count("SELECT COUNT(customer_key) FROM sem_orders") > 0,
            "customer_code": count("SELECT COUNT(customer_code) FROM sem_orders") > 0,
            "customer_group": count("SELECT COUNT(customer_group) FROM sem_orders") > 0,
            "staff": count("SELECT COUNT(staff) FROM sem_orders") > 0,
            "channel": count("SELECT COUNT(channel) FROM sem_orders") > 0,
            "payment_method": count("SELECT COUNT(payment_method) FROM sem_orders") > 0,
            "payments": count("SELECT COUNT(*) FROM sem_payments") > 0,
            "paid_recorded": count("SELECT COUNT(paid_recorded) FROM sem_orders") + count("SELECT COUNT(debt_recorded) FROM sem_orders") > 0,
            "purchases": count("SELECT COUNT(*) FROM sem_purchases") > 0,
            "purchase_lines": count("SELECT COUNT(*) FROM sem_purchase_lines") > 0,
            "stock_moves": count("SELECT COUNT(*) FROM sem_stock_moves") > 0,
            "stock_snapshot": count("SELECT COUNT(*) FROM sem_stock_snapshot") > 0,
            "lots": count("SELECT COUNT(*) FROM sem_lots") > 0,
            "line_cost": count("SELECT COUNT(cost_unit) FROM sem_lines") > 0,
            "expenses": count("SELECT COUNT(*) FROM sem_expenses") > 0,
            "price_history": count("SELECT COUNT(*) FROM sem_price_history") > 0,
            "deliveries": count("SELECT COUNT(*) FROM sem_deliveries") > 0,
            "category": count("SELECT COUNT(category) FROM sem_items") > 0,
            "rx": count("SELECT COUNT(is_rx) FROM sem_items") > 0,
            "ingredient": count("SELECT COUNT(ingredient) FROM sem_items") > 0,
            "catalog_cost": count("SELECT COUNT(cost) FROM sem_items") > 0,
        }
        def column_has_values(headers: set[str]) -> bool:
            for t in tables:
                for c in t.cols:
                    if c.h in headers or c.h.replace(" ", "") in headers:
                        if conn.execute(f"SELECT 1 FROM {_q(t.name)} WHERE TRIM(COALESCE({_q(c.sql)}, '')) != '' LIMIT 1").fetchone():
                            return True
            return False

        staff_rows = {}
        for t in tables:
            if re.search(r"nhan vien|danh sach tho|\btho\b", t.key) and t.grain in {"other", "catalog"}:
                code = t.by(headers=("manhanvien", "ma nhan vien", "ma nv"))
                name = t.by(headers=("tengoi", "ho ten", "ten", "ten nhan vien"))
                if name:
                    for r in _rows(conn, t, {"c": code.sql if code else None, "n": name.sql}):
                        n = _clean(r["n"])
                        if n:
                            staff_rows[fold_key(n)] = (fold_key(n), _clean(r["c"]) or n, n)
        conn.executemany("INSERT OR REPLACE INTO sem_staff VALUES (?,?,?)", list(staff_rows.values()))
        raw_rules = [
            (catalog.get_or_create(code, (aliases.get(fold_key(name)) or name) if name else None), fold_key(u or ""), float(fac), fold_key(to or ""))
            for name, code, u, fac, to in conn.execute("SELECT item_name, item_code, from_unit, factor, to_unit FROM _unit_rules")
        ]
        conn.executemany("INSERT INTO sem_unit_issues VALUES (?,?,?)", _unit_issues(catalog, unit_evidence, raw_rules))
        caps["service_tickets"] = count("SELECT COUNT(*) FROM sem_service") > 0
        caps["purchase_amount"] = _purchase_money_recorded(tables)
        caps["status_source"] = any(t.roles("status", "notes") for t in tables if t.grain in {"sales_line", "order_header"})
        caps["due_dates"] = bool(dues)
        caps["separate_debt_ledger"] = any(
            t.grain == "payment" and "no" in t.key.split() and t.role("debt_amount") is not None for t in tables
        ) or any(t.grain == "payment" and "gop" in t.key for t in tables)
        caps["address"] = column_has_values({"dia chi", "addr", "dia chi lien he", "dialienhe", "diachilienhe", "dia chi giao"})
        caps["gender"] = column_has_values({"gioi tinh", "namnu", "nam nu"})
        caps["return_reason"] = column_has_values({"nguyen nhan", "nguyennhan", "ly do", "ly do tra"})
        purchase_start, purchase_end = conn.execute(
            "SELECT MIN(receipt_date), MAX(receipt_date) FROM sem_purchases WHERE receipt_date IS NOT NULL"
        ).fetchone()
        meta_rows = [
            ("coverage_start", start), ("coverage_end", coverage_end),
            ("purchases_start", purchase_start), ("purchases_end", purchase_end),
        ]
        meta_rows += [
            (f"health_problem_{k}", v)
            for k, v in {**health_notes, **_health_problems(conn, tables, orders, receipts, line_problems)}.items()
        ]
        meta_rows += [(f"cap_{k}", "1" if v else "0") for k, v in caps.items()]
        conn.executemany("INSERT OR REPLACE INTO sem_meta VALUES (?, ?)", meta_rows)
        conn.commit()
    finally:
        conn.close()
