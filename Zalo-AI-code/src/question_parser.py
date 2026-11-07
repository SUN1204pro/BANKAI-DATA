"""Deterministic question understanding: entities, time, filters, then one metric rule.

Precision guard: after entities and time are replaced with placeholders, every remaining word must be
known. An unexplained word (a product we failed to match, an unsupported condition) means abstain
rather than answering a broader question than the one asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import re
import unicodedata
import sqlite3
from typing import Any, Callable

from src.grounder import CalendarIndex, DateWindow
from src import metrics as M
from src.metrics import Abstain, Answer, Filters, ShopData, Unanswerable
from src.normalizers import parse_money
from src.qualifier_guard import unbound_terms
from src.temporal import TimeSpec, parse_time, previous_window, same_period_last_year
from src.text_norm import apply_slang, fold_key, prenormalize_question

# Words that carry no condition, or whose meaning is handled by the rules below.
ALLOWED_WORDS = frozenset(
    """
    hai ba nguyen nhuan sap bo bim sua duoi lo phi cach lech tieu xu huong
    a ai ak anh ban bang bao be bi biet binh boi bot bua ca cac cai can cao cap cha chan che chi chia chiem chieu cho
    chua chung chuyen co con cong cu cua cuoi cung da dai dang dat dau day de den deu dem di do doanh doc doi don dong
    dot du dung duoc e gan gi gia giam gian giao gio giup giua gium go gom gop ha hang hay he hem hen het hieu hoi hom
    hon hong huy hut in ke kem khach khac khi khoan khong kia ky la lai lan lay le len lien liet lieu luc luot ma mang
    mat may minh moi mon mot mua muc na nao nay ne ngay nghe nguoi nhan nhap nhat nhe nhi nhieu nho nhom nhu nhung no
    noi nua o oi ong phan phai phieu qua quan ra rang rieng roi rua sang sao se so tai tang tat te the them theo thi
    thiet tho thoi thu thuc thuoc ti tien tinh toi ton tong trang tra tram tren tro trong truoc trung tu tuan tung va
    van ve vi voi vua vay xem xet xin xiu y z za zay zi ta ten da gi nha cho nho tuoi cko bit nhe ah oh uh ui ue ua
    cong tru ngoai chot lieu tiep sot kho tuoi ti trong ty muon bieu lai loi ca chi ma cua hang thang nam quy phan tram
    tron ten nua ok ho giu coi check tim ghi tra loi list top kq ket qua thang total mo ru rua
    thay vao chay size it chiu m l tuc thai nhau loai luong chenh pham uong truong hot sp ben zalo oa tieu hien
    giumf hok k luon xuat j gioi ch cham best seller han san von tm ck tri xuong thap nhanh thanh diem rong chinh alo ad thanks mun mk sanh
    tieng lon vien sau nhut mua ban thieu ncc nv xep
    """.split()
)
_PLACEHOLDER_RE = re.compile(r"<[a-z]+>")


@dataclass
class Parsed:
    text: str
    spec: TimeSpec
    filters: Filters
    items: list[tuple[str, str]]
    prices: list[int]
    as_of: str
    raw_text: str = ""


_PHONE_TAIL_RE = re.compile(r"[\s\-–—(]*\+?\d[\d\s.\-]{6,}\)?\s*$")
_CAPACITY_RE = re.compile(r"^(.+?)\s+\d+\s*gb\b", re.IGNORECASE)


def _phone_groups(conn: sqlite3.Connection, has_brand_column: bool) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """§6 phone shops: model line is the name before `…GB`, or the whole name; brand is the first word; iPhone is Apple."""
    rows = conn.execute("SELECT item_key, item_name FROM sem_items WHERE item_name IS NOT NULL").fetchall()
    if not any(_CAPACITY_RE.match(name) for _, name in rows):
        return {}, {}
    lines: dict[str, list[str]] = {}
    brands: dict[str, list[str]] = {}
    for key, name in rows:
        match = _CAPACITY_RE.match(name)
        lines.setdefault(match.group(1) if match else name.strip(), []).append(key)
        first = name.split()[0]
        brands.setdefault("Apple" if fold_key(first) == "iphone" else first, []).append(key)
    return lines, ({} if has_brand_column else brands)


class EntityIndex:
    """Business-specific names, compiled once per connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.items = dict(conn.execute("SELECT name_key, item_key FROM sem_item_names").fetchall())
        self.categories = {fold_key(c): c for (c,) in conn.execute("SELECT DISTINCT category FROM sem_items WHERE category IS NOT NULL")}
        self.groups = {fold_key(c): c for (c,) in conn.execute("SELECT DISTINCT customer_group FROM sem_orders WHERE customer_group IS NOT NULL")}
        self.ingredients = {fold_key(c): c for (c,) in conn.execute("SELECT DISTINCT ingredient FROM sem_items WHERE ingredient IS NOT NULL")}
        self.staff = {fold_key(c): c for (c,) in conn.execute("SELECT DISTINCT staff FROM sem_orders WHERE staff IS NOT NULL")}
        self.staff_names = dict(conn.execute("SELECT name_key, code FROM sem_staff").fetchall())
        names: dict[str, str] = {}
        for key, name in conn.execute("SELECT DISTINCT customer_key, customer_name FROM sem_orders WHERE customer_key IS NOT NULL AND customer_name IS NOT NULL"):
            names.setdefault(fold_key(name), key)
        stored = list(conn.execute("SELECT DISTINCT customer_key, customer_name FROM sem_orders WHERE customer_key IS NOT NULL AND customer_name IS NOT NULL").fetchall())
        stored += conn.execute("SELECT customer_key, name FROM sem_customers WHERE customer_key IS NOT NULL AND name IS NOT NULL").fetchall()
        bare: dict[str, set[str]] = {}
        for key, name in stored:
            names.setdefault(fold_key(name), key)
            bare.setdefault(fold_key(_PHONE_TAIL_RE.sub("", name)), set()).add(key)
        for name, keys in bare.items():
            if name not in names and len(keys) == 1:
                names[name] = next(iter(keys))
        self.ambiguous_customers = {name for name, keys in bare.items() if len(keys) > 1}
        self.customers = names
        self.units = {fold_key(u) for (u,) in conn.execute("SELECT DISTINCT base_unit FROM sem_items WHERE base_unit IS NOT NULL")}
        self.units |= {fold_key(u) for (u,) in conn.execute("SELECT DISTINCT unit FROM sem_lines WHERE unit IS NOT NULL")}
        self.units |= {fold_key(u) for (u,) in conn.execute("SELECT DISTINCT unit FROM sem_units")}
        self.units.discard("")
        self.brands = {fold_key(c): c for (c,) in conn.execute("SELECT DISTINCT brand FROM sem_items WHERE brand IS NOT NULL")}
        self.sizes = {fold_key(c): c for (c,) in conn.execute("SELECT DISTINCT size FROM sem_items WHERE size IS NOT NULL")}
        self.phone_lines, self.phone_brands = _phone_groups(conn, bool(self.brands))
        self.name_words = frozenset(word for name in (*self.items, *self.customers) for word in name.split())
        self.item_re = _names_regex(self.items)
        self.customer_re = _names_regex(self.customers)


def _names_regex(names: Any) -> re.Pattern[str] | None:
    keys = sorted((k for k in names if k), key=len, reverse=True)
    if not keys:
        return None
    parts = [r"[\s\W]*".join(re.escape(tok) for tok in k.split()) for k in keys]
    return re.compile(r"(?<![\w])(" + "|".join(parts) + r")(?![\w])")


_MONEY_TOKEN = r"(\d[\d.,\s]*\d|\d)\s*(?:d|dong|vnd|k|tr|trieu)?\b"


def _parse_threshold(text: str) -> tuple[str, tuple[str, int] | None]:
    """Find a money condition such as `tu 560.000 dong tro len` or `>= 580.000 dong`."""
    amount = r"(\d[\d.,]*)\s*(trieu|tr|cu|k|nghin|ngan)?\s*(?:dong|d\b)?"
    patterns = [
        (rf"(?:tu|dat|it nhat|toi thieu)\s+{amount}\s*tro len", ">="),
        (rf"(?:≥|>=)\s*{amount}", ">="),
        (rf"\bduoi\s+{amount}(?=\s|$)", "<"),
        (rf"(?:tren|hon|qua)\s+{amount}(?=\s|$)(?<!ngay)", ">"),
    ]
    multipliers = {"trieu": 1_000_000, "tr": 1_000_000, "cu": 1_000_000, "k": 1000, "nghin": 1000, "ngan": 1000}
    for pat, sym in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        tail = text[m.end(1) if m.group(2) is None else m.end(2):m.end()]
        if m.group(2) is None and "d" not in tail:
            continue  # A bare number is an order or day count, not money.
        num = m.group(1)
        if m.group(2):
            val = Decimal(num.replace(",", ".")) * multipliers[m.group(2)]
        else:
            parsed_val = parse_money(num)
            if parsed_val.value is None or parsed_val.diagnostic is not None:
                continue
            val = parsed_val.value
        return text[: m.start()] + " <money> " + text[m.end():], (sym, int(val))
    return text, None


_NOT_PRESCRIBED = r"(?:khong|hong|hok|hem|ko|k)(?: can)? (?:ke don|don bac si)"


def prepare(question: str, as_of: str, idx: EntityIndex, calendar: CalendarIndex | None) -> Parsed:
    text = " " + prenormalize_question(question, idx.name_words) + " "
    f = Filters()
    items: list[tuple[str, str]] = []

    # Prices mentioned in a price-change question are context, not filters.
    prices: list[int] = []

    def price_sub(m: re.Match[str]) -> str:
        digits = re.match(r"[\d.,]+", m.group(0)).group(0)
        prices.append(int(re.sub(r"\D", "", digits)))
        return " <price> "

    text = re.sub(r"(?<![\w.,/])\d{1,3}(?:[.,]\d{3})+\s*(?:d|dong)(?!\w)", price_sub, text) if re.search(r"tang gia|len gia|doi gia|tang tu|\blen \d", text) else text

    # §6 phone shops and catalogs with a brand or size column. "Hãng" and "hàng" fold alike, so only the raw text tells them apart.
    if re.search(r"hãng nào", unicodedata.normalize("NFC", question).lower()):
        text = re.sub(r"\bhang nao\b", " <brandrank> ", text)
    group_keys: list[str] = []
    for label in sorted(idx.phone_lines, key=len, reverse=True):
        m = re.search(r"\bdong(?: may)? " + r"[\s\W]*".join(re.escape(tok) for tok in fold_key(label).split()) + r"(?!\w)", text)
        if m:
            group_keys = idx.phone_lines[label]
            text = text[: m.start()] + " <line> " + text[m.end():]
            break
    for label in sorted(idx.phone_brands, key=len, reverse=True):
        m = re.search(r"\bhang " + re.escape(fold_key(label)) + r"(?!\w)", text)
        if m:
            group_keys = idx.phone_brands[label]
            text = text[: m.start()] + " <brand> " + text[m.end():]
            break
    for key in sorted(idx.brands, key=len, reverse=True):
        m = re.search(r"\bhang " + r"[\s\W]*".join(re.escape(tok) for tok in key.split()) + r"(?!\w)", text)
        if m:
            f.brand = idx.brands[key]
            text = text[: m.start()] + " <brand> " + text[m.end():]
            break
    for key in sorted(idx.sizes, key=len, reverse=True):
        m = re.search(r"\bsize " + re.escape(key) + r"(?!\w)|\bsua so " + re.escape(key) + r"(?!\w)", text)
        if m:
            f.catalog_size = idx.sizes[key]
            text = text[: m.start()] + " <size> " + text[m.end():]
            break

    # Items: the longest catalog name found in the question wins.
    if idx.item_re:
        def item_sub(m: re.Match[str]) -> str:
            key = fold_key(m.group(1))
            items.append((idx.items[key], key))
            return " <item> "
        text = idx.item_re.sub(item_sub, text)
    if items or group_keys:
        f.item_keys = sorted({k for k, _ in items} | set(group_keys))

    if idx.customer_re:
        def cust_sub(m: re.Match[str]) -> str:
            f.customer_key = idx.customers[fold_key(m.group(1))]
            return " <cust> "
        text = idx.customer_re.sub(cust_sub, text, count=1)
    for name in idx.ambiguous_customers:
        if re.search(r"(?<!\w)" + r"[\s\W]*".join(re.escape(tok) for tok in name.split()) + r"(?!\w)", text):
            raise Abstain("Two customer codes share this name: " + name)

    text = apply_slang(text)

    for name_key, code in idx.staff_names.items():
        m = re.search(r"\b(?:nhan vien|nv|tho)(?: ma)? " + r"\s+".join(re.escape(x) for x in name_key.split()) + r"\b", text)
        if m:
            f.staff = code
            text = text[: m.start()] + " <staff> " + text[m.end():]
            break
    m = re.search(r"\b(?:nhan vien|nv)(?: ma)? (nv\d+|[\w ]+?)(?= thang| <| ban| trong| $)", text)
    if m and fold_key(m.group(1)) in idx.staff:
        f.staff = idx.staff[fold_key(m.group(1))]
        text = text[: m.start()] + " <staff> " + text[m.end():]

    for key in sorted(idx.ingredients, key=len, reverse=True):
        m = re.search(r"\bhoat chat " + r"[\s\W]*".join(re.escape(tok) for tok in key.split()) + r"(?!\w)", text)
        if m:
            f.ingredient = idx.ingredients[key]
            text = text[: m.start()] + " <ingredient> " + text[m.end():]
            break

    # Rx before category so `thuoc ke don` is not read as a category.
    if re.search(r"\b(?:" + _NOT_PRESCRIBED + r"|tru (?:thuoc )?ke don)\b", text):
        f.rx = False
        text = re.sub(r"\b(?:hang |thuoc )?(?:" + _NOT_PRESCRIBED + r")\b|\btru (?:thuoc )?ke don(?: ra)?\b", " <rx> ", text)
    elif re.search(r"\b(?:thuoc )?ke don\b", text):
        f.rx = True
        text = re.sub(r"\b(?:thuoc )?ke don\b", " <rx> ", text)

    # Customer group vs item category: both can follow `nhom`.
    for key in sorted(idx.groups, key=len, reverse=True):
        m = re.search(r"\bkhach(?: hang)?(?: nhom| hang)? " + re.escape(key) + r"\b|\bnhom khach(?: hang)? " + re.escape(key) + r"\b", text)
        if m:
            f.group = idx.groups[key]
            text = text[: m.start()] + " <group> " + text[m.end():]
            break
    for key in sorted(idx.categories, key=len, reverse=True):
        m = re.search(r"\bnhom(?: hang| hang hoa)? " + re.escape(key) + r"(?!\w)", text)
        if m:
            f.category = idx.categories[key]
            text = text[: m.start()] + " <cat> " + text[m.end():]
            break
    if not f.group and not f.category:
        for key in sorted(idx.groups, key=len, reverse=True):
            m = re.search(r"\bnhom " + re.escape(key) + r"\b", text)
            if m:
                f.group = idx.groups[key]
                text = text[: m.start()] + " <group> " + text[m.end():]
                break

    # Sales channels and payment methods.
    channel_words = [
        (r"\b(?:ban |mua )?(?:tai quay|tai tiem|tai cua hang|truc tiep)\b", "store"),
        (r"\b(?:ban |dat |mua )?qua dien thoai\b", "phone"),
        (r"\b(?:ban |dat )?(?:qua )?zalo(?: oa)?\b", "zalo"),
        (r"\b(?:ban |dat )?(?:qua |tren )?san tmdt\b|\b(?:qua|tren) san\b(?! pham)", "marketplace"),
    ]
    found_channels: list[tuple[int, str]] = []
    for pat, cls in channel_words:
        m = re.search(pat, text)
        if m:
            found_channels.append((m.start(), cls))
            text = re.sub(pat, " <chan> ", text, count=1)
    channels = [c for _, c in sorted(found_channels)]
    if len(channels) == 1:
        f.channel = channels[0]

    text, spec_threshold = _parse_threshold(text)
    spec = parse_time(text, as_of, calendar)
    spec_text = spec.text
    if spec.weekday:
        f.weekday = spec.weekday

    m = re.search(r"\bca (sang|chieu)\b", spec_text)
    if m and not re.search(r"\bca (sang|chieu)\b.*\bca (sang|chieu)\b", spec_text):
        f.shift = M.SHIFTS[m.group(1)]
        spec_text = re.sub(r"\bca (sang|chieu)\b", " <shift> ", spec_text, count=1)

    parsed = Parsed(re.sub(r"\s+", " ", spec_text).strip(), spec, f, items, prices, as_of, prenormalize_question(question, idx.name_words))
    parsed.threshold = spec_threshold  # type: ignore[attr-defined]
    parsed.channels = channels  # type: ignore[attr-defined]
    return parsed


def _unknown_words(text: str, extra: set[str]) -> list[str]:
    cleaned = _PLACEHOLDER_RE.sub(" ", text)
    words = re.findall(r"[^\W\d_]+", cleaned, flags=re.UNICODE)
    return [w for w in words if w not in ALLOWED_WORDS and w not in extra]


_METHOD_WORDS = [
    (r"\b(?:tien mat|tien tuoi|tm)\b", "cash"),
    (r"\b(?:chuyen khoan|ck)\b", "transfer"),
    (r"\bvi dien tu\b", "ewallet"),
    (r"\bcod\b", "cod"),
    (r"\bghi no\b(?! them)", "debt"),
]


def _methods_in_order(text: str) -> list[str]:
    hits = []
    for pat, cls in _METHOD_WORDS:
        m = re.search(pat, text)
        if m:
            hits.append((m.start(), cls))
    return [c for _, c in sorted(hits)]


SUBJECTIVE_RE = re.compile(
    r"lanh nghe|tay nghe|tan tam|quy men|hay khen|hai long|than thien|nen uong|moi lan .*vien|nguoi lon nen|dep nhat|ngon nhat|lich su|vui ve|nhiet tinh|chu dao"
)
REV = r"doanh thu|tien ban|tien hang|bao nhieu tien|may tien|nhieu tien|ban (?:duoc )?(?:tong )?(?:bao nhieu|nhieu|may) tien|thu ve (?:bao nhieu |may )?tien|thu ve|thu duoc (?:tong )?(?:bao nhieu )?tien|ban (?:duoc )?(?:bao nhieu|nhieu)$|lam ra"
ASK_PCT = r"%|phan tram"
TOP_K = r"\btop (\d+)\b|\b(\d+) (?:mat hang|san pham|mon|hang|khach|nguoi|best seller)\b"


def _topk(t: str) -> int | None:
    m = re.search(TOP_K, t)
    if m:
        return int(m.group(1) or m.group(2))
    return None


def _one_window(p: Parsed) -> DateWindow:
    distinct = {(w.start_date, w.end_date): w for w in p.spec.windows}
    if len(distinct) != 1:
        raise Abstain(f"Expected one period, got {len(distinct)}")
    return next(iter(distinct.values()))


def _merged_window(p: Parsed) -> DateWindow:
    if not p.spec.windows:
        raise Abstain("No period")
    if len({(w.start_date, w.end_date) for w in p.spec.windows}) == 1:
        return p.spec.windows[0]
    if p.spec.month_range:
        return DateWindow(p.spec.windows[0].start_date, p.spec.windows[-1].end_date)
    raise Abstain("Several periods")


def _comparison_pair(p: Parsed, t: str) -> tuple[DateWindow, DateWindow]:
    """(asked period, baseline period) per Rulebook §1: growth compares against the earlier period."""
    w = p.spec.windows
    if p.spec.is_tet and w:
        return w[0], p.spec.comparison  # type: ignore[return-value]
    if len(w) == 2:
        # "so voi <t>" marks the baseline regardless of order.
        if re.search(r"so(?: voi)? <t>.*<t>", t) and not re.search(r"<t>.*so(?: voi)? <t>", t):
            return w[1], w[0]
        return w[0], w[1]
    if len(w) != 1:
        raise Abstain("Comparison needs one or two periods")
    if re.search(r"cung ky nam|nam (?:truoc|ngoai)|nam ngoai", t):
        return w[0], same_period_last_year(w[0])
    if re.search(r"cung ky|thang truoc|thang lien truoc|thang ke truoc|ky truoc|tuan truoc|tuan lien truoc|quy truoc|lien truoc|truoc do|hom qua|thang roi", t):
        return w[0], previous_window(w[0], p.as_of, same_day_count=p.spec.same_day_count_basis)  # type: ignore[return-value]
    raise Abstain("Comparison baseline not stated")


# Rules whose calculators apply `Filters`. Any other rule would answer as if a parsed filter were not there.
_APPLIES_FILTERS = frozenset({
    "revenue", "revenue_filtered", "revenue_list", "weekday_avg", "aov", "share", "change", "shift_compare", "trend", "chain_claim",
    "best_day", "best_month", "peak_hour", "order_count", "cancel", "quantity", "item_rank", "customer_rank", "customer_count",
    "gross_profit", "profit_rank", "attribute_share", "attribute_revenue", "attribute_quantity",
})
_APPLIES_ITEM = frozenset({"price_on_date", "price_effect", "avg_purchase", "stock", "purchased_minus_sold", "nearest_lot"})


def _ignored_filters(f: Filters, metric: str) -> list[str]:
    if metric in _APPLIES_FILTERS:
        return []
    scope = {
        "group": f.group, "channel": f.channel, "method": f.method, "staff": f.staff, "category": f.category,
        "ingredient": f.ingredient, "shift": f.shift, "weekday": f.weekday, "brand": f.brand, "size": f.catalog_size,
        "prescription": f.rx is not None, "item": f.item_keys and metric not in _APPLIES_ITEM,
        "customer": f.customer_key and metric != "debt",
    }
    return [name for name, value in scope.items() if value]


def _bind(p: Parsed, idx: EntityIndex, metric: str) -> None:
    """The rule for `metric` was chosen. Abstain if the question says anything that rule does not account for."""
    ignored = _ignored_filters(p.filters, metric)
    if ignored:
        raise Abstain(f"Rule [{metric}] cannot apply: " + " ".join(ignored))
    leftover = unbound_terms(p.text, metric, set(" ".join(idx.units).split()))
    if leftover:
        raise Abstain(f"Unbound [{metric}]: " + " ".join(leftover[:6]))


def _revenue_rule(f: Filters) -> str:
    filtered = f.item_keys or f.category or f.customer_key or f.staff or f.group or f.channel or f.method or f.rx is not None or f.ingredient
    return "revenue_filtered" if filtered else "revenue"


def answer_parsed(p: Parsed, data: ShopData, idx: EntityIndex) -> Answer:
    t = p.text
    f = p.filters
    threshold = getattr(p, "threshold", None)
    has_item = bool(f.item_keys)

    # ---- §7 information no sales ledger holds
    if SUBJECTIVE_RE.search(t):
        raise Unanswerable("Dữ liệu không ghi đánh giá hay hướng dẫn này")
    if re.search(r"\bkhach(?: hang)?(?: o)?(?: duong| pho| khu| quan)\b|\bo duong\b|\bdia chi\b", t):
        if data.cap("address") or data.attributes_unresolved():
            raise Abstain("Address filter not supported or attribute unresolved")
        raise Unanswerable("Shop không ghi địa chỉ khách")
    if re.search(r"\bkhach (?:nam|nu)\b|\bnam gioi\b|\bnu gioi\b|\bgioi tinh\b", t):
        if data.cap("gender") or data.attributes_unresolved():
            raise Abstain("Gender filter not supported or attribute unresolved")
        raise Unanswerable("Shop không ghi giới tính khách")
    if re.search(r"\btra (?:hang |lai )?vi (?!dien tu)|\bly do tra\b|\bnguyen nhan tra\b|\bhuy don .*vi sao\b|\bhuy(?: don)? vi\b|\bchu yeu vi\b", t):
        if data.cap("return_reason") or data.attributes_unresolved():
            raise Abstain("Return reasons not supported or attribute unresolved")
        raise Unanswerable("Shop không ghi lý do trả hàng")

    if data.cap("service_tickets"):
        return _service_answer(p, data, idx)

    extra_unit_words = set(" ".join(idx.units).split())
    unknown = _unknown_words(t, extra_unit_words | {w for u in ("tien", "mat", "cod", "chuyen", "khoan", "vi", "dien", "tu", "ghi", "tuoi") for w in [u]})
    if unknown:
        raise Abstain("Unexplained words: " + " ".join(unknown[:6]))

    if re.search(r"\bkhach moi\b", t):
        raise Abstain("New customers are not defined")

    # A named group that matched nothing must not fall through to the whole shop.
    if re.search(r"\bnhom\b(?! tuoi| \d| <)", t) and not (f.category or f.group):
        if data.cap("category") or data.cap("customer_group") or data.attributes_unresolved():
            raise Abstain("Named group not found")
        raise Unanswerable("Shop không ghi nhóm hàng hay nhóm khách")

    # Conditions that exclude things need their own rule; do not fall through to a total.
    if re.search(r"\b(?:khong tinh|ngoai tru|loai tru|tru)\b", t) and f.rx is None and not re.search(r"\bnhap tru\b", t):
        raise Abstain("Exclusion condition not supported")

    # ---- §6 mother and baby: order-adjustment rows, reported as positive amounts by invoice date
    if f.brand or f.catalog_size:
        return _attribute_answer(p, data, idx)
    if "<brandrank>" in t or re.search(r"\bdong may nao\b", t):
        _bind(p, idx, "phone_rank")
        groups = idx.phone_brands if "<brandrank>" in t else idx.phone_lines
        if not groups:
            raise Abstain("Shop has no phone lines or derived brands")
        measure = "qty" if re.search(r"ban chay|ban nhieu nhat|ban duoc nhieu nhat|hot nhat", t) else "revenue" if re.search(REV, t) else None
        if measure is None:
            raise Abstain("Ranking measure not understood")
        return Answer("string", M.top_item_group(data, _one_window(p), groups, measure))

    m_adj = re.search(r"\bvoucher\b|\bvau cho\b|\bdung diem\b|\bphi ship\b|\bphi giao\b", t)
    if m_adj and re.search(r"bao nhieu|may|tong", t):
        _bind(p, idx, "adjustments")
        label = m_adj.group(0)
        pattern = "%voucher%" if label in {"voucher", "vau cho"} else ("%diem%" if "diem" in label else "%ship%")
        w = _one_window(p)
        return Answer("number", M.adjustment_total(data, w, pattern), "money")
    if re.search(r"thanh vien moi|dang ky moi", t):
        _bind(p, idx, "new_members")
        return Answer("number", M.new_members(data, _one_window(p)), "count")

    # ---- §6 phone shop: trade-in and payable totals on revenue-eligible orders
    if re.search(r"thu may cu", t) and re.search(r"tien|bao nhieu", t):
        _bind(p, idx, "tradein")
        return Answer("number", M.order_field_total(data, _one_window(p), "tradein"), "money")
    if re.search(r"khach phai tra", t):
        _bind(p, idx, "payable")
        return Answer("number", M.order_field_total(data, _one_window(p), "payable"), "money")

    # ---- §5 price effect
    if re.search(r"tang gia|len gia|doi gia|tang tu|len <price>", t) and has_item:
        _bind(p, idx, "price_effect")
        m_n = re.search(r"(\d+) ngay", t)
        day = p.spec.windows[0].start_date if p.spec.windows else (
            M.first_increase_to(data, f.item_keys, p.prices[-1]) if p.prices else None
        )
        if not m_n or day is None:
            raise Abstain("Price effect needs N and D")
        return Answer("percent", M.price_effect_pct(data, f.item_keys, day, int(m_n.group(1))))

    # ---- price on a day
    if re.search(r"\b(?:don )?gia(?: ban)?\b", t) and has_item and not re.search(r"nhap", t) and p.spec.windows and p.spec.windows[0].start_date == p.spec.windows[0].end_date:
        _bind(p, idx, "price_on_date")
        return Answer("number", M.price_on_date(data, f.item_keys, p.spec.windows[0].start_date), "money")

    # ---- §5 purchases
    if re.search(r"gia nhap|nhap .*(?:binh quan|trung binh)|(?:binh quan|trung binh).*nhap", t) and has_item:
        _bind(p, idx, "avg_purchase")
        unit = next((u for u in sorted(idx.units, key=len, reverse=True) if re.search(rf"\b(?:mot|1|moi) {re.escape(u)}\b", t)), None)
        return Answer("number", M.avg_purchase_price(data, _one_window(p), f.item_keys, unit), "money")
    if re.search(r"nha cung cap", t):
        _bind(p, idx, "supplier")
        if re.search(r"chinh\b", t):
            return Answer("string", M.top_supplier(data, _one_window(p), "count"))
        if re.search(r"nhieu|cao nhat|lon nhat", t):
            return Answer("string", M.top_supplier(data, _one_window(p), "amount"))
        raise Abstain("Supplier question not supported")
    if re.search(r"\bnhap\b", t) and not has_item and re.search(r"tien|gia tri|ton|phieu", t):
        _bind(p, idx, "purchase_total")
        if re.search(r"bao nhieu phieu|may phieu|so phieu", t):
            raise Abstain("Receipt count not supported")
        return Answer("number", M.purchase_total(data, _one_window(p)), "money")

    # ---- recorded payment due dates: stated future dates remain queryable
    if re.search(r"\bngay phai tra\b|\bhan tra\b", t):
        _bind(p, idx, "due")
        if not data.cap("due_dates"):
            raise Unanswerable("Shop không ghi hạn trả")
        if re.search(r"sau <t>|muon hon|sau hom nay|sau ngay", t):
            return Answer("number", M.due_count(data, (p.as_of[:0] + _next_day(p.as_of), "9999-12-31"), p.as_of), "count")
        return Answer("number", M.due_count(data, (_one_window(p).start_date, _one_window(p).end_date), p.as_of), "count")

    # ---- §4 debt
    is_debt = re.search(r"\bno\b|cong no|con thieu|mua chiu|thu hoi", t) and not re.search(r"\bghi no\b(?! them)", t)
    if is_debt:
        _bind(p, idx, "debt")
        if re.search(r"no them|mua chiu them|tang no", t) and re.search(r"tra", t) and f.customer_key:
            w = _one_window(p)
            return Answer("list", [M.debt_added(data, w, f.customer_key), M.debt_collected(data, w, f.customer_key)], "money")
        if re.search(r"no them|mua chiu them|phat sinh", t):
            return Answer("number", M.debt_added(data, _one_window(p), f.customer_key), "money")
        if re.search(r"thu no|tra no|tra bot|thu hoi|doi duoc|thu duoc .*no|tien khach tra no|tra .*no", t) and p.spec.windows:
            return Answer("number", M.debt_collected(data, _one_window(p), f.customer_key), "money")
        if f.customer_key:
            raise Abstain("One customer's balance has no calculator; the totals below would ignore the customer")
        day = p.spec.snapshot or (p.spec.windows[0].end_date if len(p.spec.windows) == 1 and p.spec.windows[0].start_date == p.spec.windows[0].end_date else p.as_of)
        if re.search(r"(?:top|\d+ khach).*no|no .*(?:lon|nhieu) nhat", t):
            k = _topk(t) or 1
            rows = M.top_debtors(data, day, k)
            if re.search(r"so no|tung nguoi|moi nguoi|kem so tien|con no bao|no bao nhieu", t):
                return Answer("table", {"columns": ["khach_hang", "so_tien_no"], "rows": [list(r) for r in rows]}, "money")
            return Answer("string", rows[0][0]) if k == 1 and rows else Answer("list", [r[0] for r in rows])
        if re.search(r"so khach|bao nhieu khach|may khach", t):
            return Answer("number", M.debtor_count(data, day), "count")
        if re.search(r"tuoi no|chia theo", t):
            buckets = re.findall(r"(\d+)\s*[–—-]\s*(\d+)", t)
            m_top = re.search(r"tren (\d+)", t)
            if not buckets and not re.search(r"(?:qua|tren|hon) (\d+) ngay", t):
                raise Abstain("Debt age buckets not understood")
            ranges = [(int(a), int(b)) for a, b in buckets]
            if m_top:
                ranges.append((int(m_top.group(1)) + 1, None))
            if not ranges:
                raise Abstain("Debt age buckets not understood")
            if len(ranges) == 1:
                return Answer("number", M.total_debt(data, day, age_range=ranges[0]), "money")
            return Answer("list", [M.total_debt(data, day, age_range=r) for r in ranges], "money")
        m_age = re.search(r"(?:qua|tren|hon) (\d+) ngay", t)
        if m_age:
            return Answer("number", M.total_debt(data, day, older_than=int(m_age.group(1))), "money")
        return Answer("number", M.total_debt(data, day), "money")

    # ---- §5 stock
    if re.search(r"\blo\b.*gan het han nhat", t) and has_item:
        _bind(p, idx, "nearest_lot")
        day = p.spec.snapshot or (p.spec.windows[0].end_date if p.spec.windows else p.as_of)
        nearest = M.nearest_expiry(data, f.item_keys, day)
        if nearest is None:
            raise Abstain("No lot in stock and not expired")
        return Answer("date", nearest)
    if re.search(r"\b(?:may|bao nhieu) lo\b", t) and re.search(r"\bhet han\b", t):
        _bind(p, idx, "expiring_lot_count")
        until = re.search(r"\bden het(?: ngay)? (\d{1,2})/(\d{1,2})/(\d{4})", p.raw_text)
        if not until:
            raise Abstain("Expiring lots need an end date")
        return Answer("number", M.expiring_lot_count(data, p.as_of, f"{until.group(3)}-{int(until.group(2)):02d}-{int(until.group(1)):02d}"), "count")
    if re.search(r"\bton\b|trong kho|con lai|con may|con bao nhieu", t) and has_item:
        _bind(p, idx, "stock")
        day = p.spec.snapshot or p.as_of
        if len(f.item_keys) != 1:
            raise Abstain("Stock for several items")
        qty = M.stock_at(data, f.item_keys[0], day)
        m_big = re.search(r"may (\S+(?: \S+)?) (?:chan|nguyen)|bao nhieu (\S+(?: \S+)?) (?:chan|nguyen)", t)
        if m_big:
            big = (m_big.group(1) or m_big.group(2)).strip()
            return Answer("list", M.split_stock(data, f.item_keys[0], qty, big))
        return Answer("number", M.clean_qty(qty), "quantity")

    # ---- §5 purchased minus sold
    if re.search(r"nhap tru|chenh nhap|nhap .*tru .*ban|nhap va ban", t) and has_item:
        _bind(p, idx, "purchased_minus_sold")
        return Answer("number", M.purchased_minus_sold(data, _merged_window(p), f), "quantity")

    # ---- §6 lots
    m_exp = re.search(r"het han trong (\d+) ngay", t)
    if m_exp and re.search(r"\b(?:may|bao nhieu) (?:mat hang|san pham|sp|hang)\b", t):
        _bind(p, idx, "expiring_count")
        end = (date.fromisoformat(p.as_of) + timedelta(days=int(m_exp.group(1)))).isoformat()
        return Answer("number", len(M.expiring_items(data, p.as_of, end)), "count")
    if re.search(r"\btien von\b", t) and re.search(r"\bhan dung\b", t) and len(p.spec.windows) == 1:
        _bind(p, idx, "expiring_cost")
        w = p.spec.windows[0]
        if w.start_date != p.as_of:
            raise Abstain("Expiry cost is defined from today")
        return Answer("number", M.expiring_stock_cost(data, p.as_of, (date.fromisoformat(w.end_date) - date.fromisoformat(w.start_date)).days), "money")
    if m_exp and not re.search(r"\b(?:hang nao|sp nao|mat hang nao|san pham nao)\b", t):
        _bind(p, idx, "expiring_cost")
        return Answer("number", M.expiring_stock_cost(data, p.as_of, int(m_exp.group(1))), "money")

    # ---- §5 items out of stock at the end of a day
    if re.search(r"\bhet hang\b", t) and not has_item:
        _bind(p, idx, "out_of_stock")
        day = p.spec.snapshot or (p.spec.windows[0].end_date if len(p.spec.windows) == 1 and p.spec.windows[0].start_date == p.spec.windows[0].end_date else None)
        if day is None:
            raise Abstain("Out of stock needs one day")
        names = M.out_of_stock_items(data, day)
        return Answer("list", names) if re.search(r"\bnao\b|liet ke", t) else Answer("number", len(names), "count")

    # ---- §5 inventory value and expiry listing
    if re.search(r"gia tri .*(?:kho|ton)|(?:kho|ton) .*gia von|gia von.*(?:kho|ton)", t):
        _bind(p, idx, "inventory_value")
        return Answer("number", M.inventory_value(data, p.spec.snapshot or (p.spec.windows[0].end_date if p.spec.windows else p.as_of)), "money")
    if re.search(r"han dung|het han", t) and re.search(r"\b(?:hang nao|sp nao|mat hang nao|san pham nao|nhung sp|nhung mat hang|nhung hang)\b", t):
        _bind(p, idx, "expiring_items")
        if not data.cap("lots"):
            raise Unanswerable("Shop không ghi hạn dùng")
        if m_exp and not p.spec.windows:
            end = (date.fromisoformat(p.as_of) + timedelta(days=int(m_exp.group(1)))).isoformat()
            return Answer("list", M.expiring_items(data, p.as_of, end))
        w = _one_window(p)
        return Answer("list", M.expiring_items(data, w.start_date, w.end_date))

    # ---- §5 profit
    if re.search(r"(?:hang|mat hang|mon|san pham) nao .*(?:lai|loi)|(?:lai gop|loi).*(?:hang|mat hang|mon) nao|(?:hang|mat hang|mon|san pham) .*(?:lai gop|loi nhuan gop) .*nhat", t):
        _bind(p, idx, "profit_rank")
        k = _topk(t)
        res = M.top_items(data, _one_window(p), f, k or 1, "profit")
        return Answer("list", res) if k and k > 1 else Answer("string", res[0])
    if re.search(r"\blai rong\b|loi nhuan rong", t):
        _bind(p, idx, "net_profit")
        return Answer("number", M.net_profit(data, _one_window(p)), "money")
    if re.search(r"lai gop|loi nhuan gop|\bloi gop\b", t):
        _bind(p, idx, "gross_profit")
        return Answer("number", M.gross_profit(data, _one_window(p), f), "money")
    if re.search(r"hang loi nhat|mat hang loi nhat|loi nhat", t):
        _bind(p, idx, "profit_rank")
        k = _topk(t)
        res = M.top_items(data, _one_window(p), f, k or 1, "profit")
        return Answer("list", res) if k and k > 1 else Answer("string", res[0])

    # ---- deliveries
    if re.search(r"\bdot\b", t) and re.search(r"giao|cho", t):
        _bind(p, idx, "deliveries")
        return Answer("number", M.delivery_batches(data, _one_window(p)), "count")

    # ---- §5 unsold
    if re.search(r"\b(?:mon|hang|mat hang)(?: bi)? e\b|\be\b.*khong ban|khong ban (?:duoc|ra)", t):
        _bind(p, idx, "unsold")
        res = M.unsold_items(data, _one_window(p))
        if re.search(r"bao nhieu|may|dem|so mat hang|so mon", t):
            return Answer("number", len(res), "count")
        return Answer("list", res)

    # ---- §3 rankings: staff
    if re.search(r"nhan vien|\btho\b|nguoi ban", t) and re.search(r"gioi nhat|nhieu tien nhat|cao nhat|chay nhat|nhieu nhat", t):
        _bind(p, idx, "staff_rank")
        return Answer("string", M.top_staff(data, _one_window(p), "revenue"))
    if re.search(r"cham chi nhat", t):
        _bind(p, idx, "staff_days")
        return Answer("string", M.top_staff(data, _one_window(p), "days"))

    # ---- items in at least N orders
    m_items = re.search(r"(?:it nhat|tu|o|o tu|trong|trong it nhat|trong tu|o it nhat|≥|>=) ?(\d+) don", t)
    if m_items and re.search(r"mat hang|san pham|\bmon\b|\bsp\b", t) and not re.search(r"\bkhach\b", t):
        _bind(p, idx, "items_min_orders")
        return Answer("number", M.items_with_min_orders(data, _one_window(p), int(m_items.group(1))), "count")

    # ---- §3 customers
    m_min = re.search(r"(?:it nhat|tu|toi thieu|≥|>=) ?(\d+) don", t)
    if m_min and re.search(r"khach", t):
        _bind(p, idx, "customers_min_orders")
        n, total = M.customers_with_min_orders(data, _one_window(p), int(m_min.group(1)))
        if re.search(ASK_PCT + r"|ti le|chiem", t):
            if not total:
                raise Abstain("No customers bought in the period")
            return Answer("percent", M.ratio_pct(n, total))
        return Answer("number", n, "count")
    if re.search(r"\bkhach\b|\bai\b", t) and not f.customer_key and not f.group:
        measure = None
        if re.search(r"trung thanh", t):
            measure = "days"
        elif re.search(r"\bsop\b", t):
            measure = "aov"
        elif re.search(r"doi y", t):
            measure = "cancelled"
        elif re.search(r"nhieu don|nhieu lan|so don|don nhat", t):
            measure = "orders"
        elif re.search(r"chi tieu|nhieu tien|tien mua|chi nhieu|tong tien|mua nhieu", t):
            measure = "revenue"
        if measure:
            _bind(p, idx, "customer_rank")
            k = _topk(t)
            res = M.top_customers(data, _one_window(p), f, k or 1, measure)
            if not res:
                raise Abstain("No customers in period")
            return Answer("list", res) if k and (k > 1 or re.search(r"\b1 khach\b", t) is None and k > 1) else Answer("string", res[0])
        if re.search(r"bao nhieu khach|may khach|so khach", t):
            _bind(p, idx, "customer_count")
            return Answer("number", M.distinct_customers(data, _one_window(p), f), "count")

    # ---- §3 items
    if re.search(r"ban chay|ban nhieu nhat|ban duoc nhieu nhat|hot nhat|chay nhat|ua chuong|best seller", t) and not has_item:
        _bind(p, idx, "item_rank")
        measure = "orders" if "ua chuong" in t else "qty"
        k = _topk(t)
        res = M.top_items(data, _one_window(p), f, k or 1, measure)
        if not res:
            raise Abstain("No items sold in period")
        return Answer("list", res) if k and k > 1 else Answer("string", res[0])

    # ---- §1 cancellations
    if re.search(r"\bhuy\b", t):
        _bind(p, idx, "cancel")
        if re.search(ASK_PCT + r"|ti le", t):
            return Answer("percent", M.cancel_rate(data, _merged_window(p), f))
        if re.search(r"bao nhieu|may|so don|dem", t):
            return Answer("number", M.cancelled_count(data, _merged_window(p), f), "count")
        raise Abstain("Cancellation question not understood")

    # ---- average order value
    if re.search(r"trung binh|gia tri .*(?:mot|1|moi) don", t) and re.search(r"(?:moi|mot|1|tren mot|tren 1) don|don", t):
        _bind(p, idx, "aov")
        if p.spec.weekday:
            raise Abstain("Weekday average of orders not supported")
        if re.search(ASK_PCT, t):
            raise Abstain("AOV percent not supported")
        return Answer("number", M.average_order_value(data, _one_window(p), f), "aov")

    # ---- two-channel revenue lists
    channels = getattr(p, "channels", [])
    if len(channels) >= 2 and re.search(REV, t):
        _bind(p, idx, "revenue_list")
        data.require("channel")
        w = _one_window(p)
        return Answer("list", [M.revenue(data, w, _with_channel(f, c)) for c in channels], "money")

    # ---- two-method revenue lists
    methods = _methods_in_order(t)
    if methods and re.search(REV, t):
        # The method phrases are now bound, so they no longer count as leftovers.
        for pattern, _ in _METHOD_WORDS:
            t = re.sub(pattern, " <meth> ", t)
        p.text = t
        data.require("payment_method")
        if len(methods) >= 2:
            _bind(p, idx, "revenue_list")
            w = _one_window(p)
            return Answer("list", [M.revenue(data, w, _with_method(f, m)) for m in methods], "money")
        f = _with_method(f, methods[0])

    # ---- best day / month
    if re.search(r"ngay nao|ngay mo|ngay dat khach", t) and re.search(r"nhieu tien|doanh thu|dat nhat", t) and not re.search(r"dat khach", t):
        _bind(p, idx, "best_day")
        return Answer("date", M.best_day(data, _one_window(p), f, "revenue"))
    if re.search(r"ngay dat khach", t):
        _bind(p, idx, "best_day")
        return Answer("date", M.best_day(data, _one_window(p), f, "orders"))
    if re.search(r"thang nao", t) and re.search(REV, t) and len(p.spec.windows) >= 2:
        _bind(p, idx, "best_month")
        best = max(p.spec.windows, key=lambda w: (M.revenue(data, w, f), -int(w.start_date.replace("-", ""))))
        return Answer("date", best.start_date[:7])
    if re.search(r"gio cao diem", t):
        _bind(p, idx, "peak_hour")
        return Answer("number", M.peak_hour(data, _one_window(p), f), "count")
    m_days = re.search(r"bao nhieu (?:ngay|bua)|may (?:ngay|bua)|so ngay|nhieu ngay", t)
    if m_days and threshold:
        _bind(p, idx, "days_threshold")
        if threshold[0] == "<":
            raise Abstain("Whether a day with no orders is below a threshold is not defined")
        return Answer("number", M.days_with_revenue(data, _one_window(p), threshold[0], threshold[1]), "count")

    # ---- shift comparison (needs two shifts in the same period)
    if re.search(r"\bca chieu\b.*\bca sang\b|\bca sang\b.*\bca chieu\b", t):
        _bind(p, idx, "shift_compare")
        is_orders = bool(re.search(r"\bso don\b", t)) and not re.search(REV, t)
        w = _one_window(p)
        def val(shift_name: str) -> int:
            flt = _with_shift(f, shift_name)
            return M.order_count(data, w, flt) if is_orders else M.revenue(data, w, flt)
        chieu, sang = val("chieu"), val("sang")
        if re.search(ASK_PCT, t):
            return Answer("percent", M.growth_pct(chieu, sang))
        return Answer("number", chieu - sang, "count" if is_orders else "money")

    # ---- share of revenue
    if re.search(r"chiem|ti trong|dong gop|phan cua|bang .*(?:%|phan tram)|bao nhieu %|may %|may phan tram", t) and re.search(REV + "|nhom|<cat>|<group>|<rx>|<chan>", t) and (f.needs_lines or f.group or f.channel or f.method or f.customer_key):
        _bind(p, idx, "share")
        w = _merged_window(p)
        part = M.revenue(data, w, f)
        whole = M.revenue(data, w, Filters())
        if whole == 0:
            raise Abstain("Zero total revenue")
        return Answer("percent", M.ratio_pct(part, whole))

    # ---- boolean trend
    if re.search(r"thang sau (?:deu |luon |co luon |co deu )?(?:cao |hon )", t) and len(p.spec.windows) >= 2:
        _bind(p, idx, "trend")
        vals = [M.revenue(data, w, f) for w in p.spec.windows]
        return Answer("boolean", all(b > a for a, b in zip(vals, vals[1:])))
    if re.search(r"tang lien tuc|giam lien tuc", t) and len(p.spec.windows) >= 2:
        _bind(p, idx, "trend")
        vals = [M.revenue(data, w, f) for w in p.spec.windows]
        up = "tang" in t
        pairs = list(zip(vals, vals[1:]))
        return Answer("boolean", all(b > a for a, b in pairs) if up else all(b < a for a, b in pairs))

    # ---- growth and differences
    wants_change = re.search(r"tang|giam|thay doi|hon hay kem|cao hay thap|chenh|hon|kem", t)
    if wants_change and re.search(REV + r"|\bso don\b", t) and (len(p.spec.windows) == 2 or p.spec.is_tet or re.search(r"so voi|so |cung ky|thang truoc|thang lien truoc|tuan truoc|quy truoc|ky truoc|truoc do|thang roi", t)):
        _bind(p, idx, "change")
        now, base = _comparison_pair(p, t)
        is_orders = bool(re.search(r"\bso don\b", t)) and not re.search(REV, t)
        if is_orders:
            cur, prev = M.order_count(data, now, f), M.order_count(data, base, f)
        else:
            cur = M.revenue(data, now, f, what="Kỳ hỏi")
            prev = M.revenue(data, base, f, what="Kỳ so sánh")
        if re.search(ASK_PCT, t) or re.search(r"(?:tang|giam|thay doi) (?:hay giam )?(?:bao nhieu|may)$", t) and False:
            return Answer("percent", M.growth_pct(cur, prev))
        if re.search(r"bao nhieu|may|chenh", t):
            return Answer("number", cur - prev, "count" if is_orders else "money")
        raise Abstain("Change question has no unit")

    # ---- order counts
    if re.search(r"\bdon\b", t) and re.search(r"bao nhieu|may|so don|dem|nhieu", t) and not has_item or (has_item and re.search(r"\b(?:bao nhieu|may|so) don\b", t)):
        if not re.search(REV, t) or threshold:
            _bind(p, idx, "order_count")
            return Answer("number", M.order_count(data, _merged_window(p), f, min_revenue=threshold), "count")

    # ---- quantity sold
    unit = next((u for u in sorted(idx.units, key=len, reverse=True) if re.search(rf"\b(?:bao nhieu|may|so|tong|nhieu) {re.escape(u)}\b(?! nhieu)", t)), None)
    m_size = re.search(r"\bsize (m|l)\b", t)
    if m_size:
        f.size_suffix = m_size.group(1)
    if not re.search(REV, t) or unit:
        if has_item or unit or m_size:
            if re.search(r"ban|mua|<item>", t):
                _bind(p, idx, "quantity")
                if has_item:
                    return Answer("number", M.quantity_sold(data, _merged_window(p), f), "quantity")
                return Answer("number", M.quantity_sold(data, _merged_window(p), f, None if m_size else unit), "quantity")

    # ---- a chain of month claims: each is checked against its own month
    claims = re.findall(r"thang (\d{1,2}/\d{4}) (cao|thap) hon thang (\d{1,2}/\d{4})", p.raw_text)
    if claims and re.search(REV, t):
        _bind(p, idx, "chain_claim")
        windows = {f"{int(w.start_date[5:7])}/{w.start_date[:4]}": w for w in p.spec.windows}
        if any(a not in windows or b not in windows for a, _, b in claims):
            raise Abstain("A claimed month is outside the parsed periods")
        values = {key: M.revenue(data, w, f) for key, w in windows.items()}
        return Answer("boolean", all(values[a] > values[b] if word == "cao" else values[a] < values[b] for a, word, b in claims))

    # ---- a chain of claims needs its own rule, not a list of numbers
    if re.search(r"\bdung (?:vay )?khong\b|\bphai khong\b", t) and len(p.spec.windows) >= 2:
        raise Abstain("Chained claim not supported")

    # ---- revenue lists over several months
    if re.search(REV, t) and len(p.spec.windows) > 1:
        _bind(p, idx, "revenue_list")
        wants_list = re.search(r"lan luot|moi thang|tung thang|moi quy|theo thu tu|\d so|liet ke|cac thang", t) or not p.spec.month_range
        if wants_list:
            return Answer("list", [M.revenue(data, w, f) for w in p.spec.windows], "money")
        return Answer("number", M.revenue(data, _merged_window(p), f), "money")

    # ---- weekday average
    if re.search(r"trung binh", t) and p.spec.weekday and re.search(REV, t):
        _bind(p, idx, "weekday_avg")
        w = _one_window(p)
        start, end = data.clip(w)
        count = sum(1 for d in _date_range(start, end) if d.isoweekday() == p.spec.weekday)
        if count == 0:
            raise Abstain("Weekday does not occur")
        return Answer("number", M.money(Decimal(M.revenue(data, w, f)) / count), "money")

    # ---- plain revenue
    if re.search(REV, t) or (f.staff or f.group or f.channel or f.customer_key or f.category or has_item):
        if not p.spec.windows and not p.spec.snapshot:
            raise Abstain("Revenue needs a period")
        if re.search(r"trung binh", t):
            raise Abstain("Average not understood")
        if not re.search(r"ban|tien|doanh thu|thu|mua", t):
            raise Abstain("No revenue wording")
        _bind(p, idx, _revenue_rule(f))
        return Answer("number", M.revenue(data, _one_window(p), f), "money")

    raise Abstain("No rule matched")


def _attribute_answer(p: Parsed, data: ShopData, idx: EntityIndex) -> Answer:
    """§6 Mẹ và bé: a question about a catalog brand or size. Shares divide by all nappies in the same period."""
    t, f = p.text, p.filters
    if re.search(ASK_PCT + r"|ti le|ti trong|chiem", t):
        _bind(p, idx, "attribute_share")
        nappies = [name for key, name in idx.categories.items() if re.search(r"\bbim\b", key)]
        if len(nappies) != 1 or not (f.category == nappies[0] or re.search(r"\bbim\b", t)):
            raise Abstain("A share needs one nappy category as its base")
        window = _one_window(p)
        whole = M.quantity_sold(data, window, Filters(category=nappies[0]))
        if not whole:
            raise Abstain("No nappies sold in the period")
        part = M.quantity_sold(data, window, Filters(category=nappies[0], brand=f.brand, catalog_size=f.catalog_size))
        return Answer("percent", M.ratio_pct(Decimal(str(part)), Decimal(str(whole))))
    if re.search(REV, t):
        _bind(p, idx, "attribute_revenue")
        return Answer("number", M.revenue(data, _one_window(p), f), "money")
    if re.search(r"\bban\b", t) and re.search(r"bao nhieu|\bmay\b", t):
        _bind(p, idx, "attribute_quantity")
        return Answer("number", M.quantity_sold(data, _one_window(p), f), "quantity")
    raise Abstain("Brand or size question not understood")


_JOB_WORDS = [
    (r"thay nhot|\bnhot\b(?! lap)", "nhot"),
    (r"nhot lap|dau lap", "nhot lap"),
    (r"va ruot|\bva\b", "va"),
    (r"bao duong|bdtq", "bao duong"),
    (r"sua dien|lam dien", "dien"),
    (r"ve sinh noi|\bvs noi\b", "vs noi"),
    (r"ve sinh kim phun|\bvs kim phun\b", "vs kim phun"),
    (r"bu gi|bugi", "bu gi"),
    (r"nuoc mat", "nuoc mat"),
    (r"nhong xich", "nhong xich"),
    (r"ac quy|\bbinh\b", "binh"),
]


def _service_answer(p: Parsed, data: ShopData, idx: EntityIndex) -> Answer:
    """§6 Sửa xe: every ledger row is one ticket; warranty tickets count as visits but not as paid."""
    _bind(p, idx, "service")
    t = p.text
    f = p.filters
    if re.search(r"doanh thu|tien ban|ban duoc|nhieu tien|bao nhieu tien|tien", t) and not re.search(r"phieu|luot|\bxe\b|\btho\b|hoa hong", t):
        return Answer("number", M.revenue(data, _one_window(p), Filters()), "money")
    w = _one_window(p)
    start, end = data.clip(w)
    if re.search(r"hoa hong", t):
        raise Abstain("Commission tables not supported")
    if re.search(r"ti le bao hanh", t):
        warranty = M.service_count(data, start, end, warranty=True)
        paid = M.service_count(data, start, end, warranty=False)
        if not paid:
            raise Abstain("No paid tickets")
        return Answer("percent", M.ratio_pct(warranty, paid))
    if re.search(r"\bbao hanh\b", t) and re.search(r"bao nhieu|may|so", t):
        return Answer("number", M.service_count(data, start, end, warranty=True), "count")
    if re.search(r"trung binh", t) and re.search(r"phieu|luot", t):
        paid = M.service_count(data, start, end, warranty=False)
        if not paid:
            raise Abstain("No paid tickets")
        return Answer("number", M.money(Decimal(M.service_money(data, start, end)) / paid), "money")
    if re.search(r"\btho\b", t) and re.search(r"nhieu|nhat", t):
        if re.search(r"tien", t):
            return Answer("string", M.top_mechanic(data, start, end, "money"))
        return Answer("string", M.top_mechanic(data, start, end, "tickets"))
    m_min = re.search(r"tu (\d+) (?:lan|phieu|luot)", t)
    if m_min and re.search(r"\bxe\b|chiec", t):
        return Answer("number", M.returning_vehicles(data, start, end, int(m_min.group(1))), "count")
    if re.search(r"khach quen|xe quen", t):
        return Answer("number", M.returning_vehicles(data, start, end, 2), "count")
    if re.search(r"so chiec|may chiec|bao nhieu chiec|bien so", t):
        return Answer("number", M.distinct_vehicles(data, start, end), "count")
    for pat, job in _JOB_WORDS:
        if re.search(pat, t):
            return Answer("number", M.service_job_count(data, start, end, job), "count")
    if re.search(r"luot|phieu", t) and re.search(r"bao nhieu|may|so", t):
        return Answer("number", M.service_count(data, start, end), "count")
    raise Abstain("Service question not understood")


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _date_range(start: str, end: str):
    d, stop = date.fromisoformat(start), date.fromisoformat(end)
    while d <= stop:
        yield d
        d += timedelta(days=1)


def _with_method(f: Filters, method: str) -> Filters:
    from dataclasses import replace
    return replace(f, method=method)


def _with_channel(f: Filters, channel: str) -> Filters:
    from dataclasses import replace
    return replace(f, channel=channel)


def _with_shift(f: Filters, shift_name: str) -> Filters:
    from dataclasses import replace
    return replace(f, shift=M.SHIFTS[shift_name])
