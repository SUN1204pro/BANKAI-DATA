"""Refuse to answer when a question says something the chosen rule did not use.

Choosing a rule is pattern matching, and a pattern can match while the question also asks for a narrower scope:
"doanh thu khách mới" still contains "doanh thu". Each rule therefore declares the wording it accounts for. A word
or number left over after removing that wording, the fillers, and the parsed placeholders may be a condition the
rule ignored, so the question abstains. That scores 0 where a wrong-scope number scores -1.

The cost is coverage: unfamiliar but harmless wording also abstains.
"""

from __future__ import annotations

import re

_PLACEHOLDER_RE = re.compile(r"<[a-z]+>")
_WORD_RE = re.compile(r"\d[\d.,]*|[^\W\d_]+", re.UNICODE)
# "Not" is never a filler because it flips a scope. Only as the last word is it a tag question ("... không?").
_TAG_QUESTION_RE = re.compile(r"\b(?:khong|hong|hok|hem|ko|k)\W*$")

# Politeness, question words and glue. A word that can change a question's scope must not be listed here.
_FILLER_PHRASES = (
    "cua hang", "bao nhieu", "cho toi", "cho hoi", "tat ca", "tong cong", "the nao", "nhu nao", "chi oi", "be oi", "anh oi", "la chi",
    "tinh rieng",
)
_FILLER_WORDS = frozenset(
    """
    a ah ak oh uh ui ue ua e oi ha he hen hoi nhe nhi ne z za zay zi ta nha nghe na vay ok alo ad thanks
    cho toi xem coi check giup gium giumf tinh biet bit xiu cko la thi ma de ghi mo gi sao nhu da dang se co cua o
    trong tai roi duoc nao ho anh minh mun muon mk may tong het den nay ngay thang quy nam tuan hom qua vao
    voi va tu trong khi luc j do day ke thoi bao
    """.split()
)

_ORDER_WORDS = ("don", "so don", "cac don", "dem", "so", "ban", "ban ra", "nhan", "dat", "tong so", "don hang", "rieng", "nhieu")
_REVENUE_WORDS = (
    "doanh thu", "tien ban", "tien hang", "tien", "ban", "ban ra", "mua", "thu", "thu ve", "thu duoc", "mang ve", "dem ve",
    "nhieu", "lam ra", "xet theo",
)

# Wording each rule accounts for. Filters bind their own words as placeholders, so plain rules stay strict.
_RULE_VOCAB: dict[str, tuple[str, ...]] = {
    "revenue": _REVENUE_WORDS + ("rieng", "don hang", "tien ban hang"),
    "attribute_share": (
        "chiem", "phan tram", "ti le", "ti trong", "so", "tong", "bich", "bim", "bim ta", "hang", "mat hang", "tren", "cac", "ban", "ban duoc", "so luong", "sua",
    ),
    "attribute_revenue": _REVENUE_WORDS + ("bim", "sua", "hang", "mat hang", "cac", "san pham"),
    "attribute_quantity": ("ban", "ban duoc", "ban ra", "duoc", "so", "bich", "mon", "hang", "bim", "sua", "co", "san pham", "mat hang"),
    "phone_rank": _REVENUE_WORDS + ("dong may", "may", "ban chay", "chay", "nhat", "ban nhieu nhat", "cao nhat", "co", "hot nhat", "nhieu nhat"),
    "nearest_lot": ("lo", "gan het han nhat", "co", "han dung", "ngay nao", "han", "dung", "theo", "ton kho", "ton", "kho"),
    "expiring_lot_count": ("co", "lo", "lo hang", "hang", "con", "ton", "se", "het han", "han", "tu nay", "theo", "ton kho", "kho"),
    "chain_claim": _REVENUE_WORDS + ("xu huong", "co phai", "cao hon", "thap hon", "dung", "phai", "khong"),
    "expiring_count": ("co", "mat hang", "san pham", "sp", "hang", "con", "lo", "ton", "lo ton", "sap", "het han", "han", "toi", "tuc", "han dung", "dung"),
    "out_of_stock": ("mat hang", "san pham", "sp", "hang", "het hang", "cuoi", "cuoi ngay", "het ngay", "nao", "nhung", "liet ke", "co", "dem", "so"),
    "revenue_filtered": _REVENUE_WORDS + (
        "cac don", "don", "cac", "don ban", "nhom hang", "nhom", "hang", "mat hang", "cac mat hang", "khach", "khach hang",
        "thuoc", "co", "hoat chat", "tra bang", "nhan vien", "ma", "san pham", "tung", "mon", "cac thuoc", "phan", "con lai", "thuoc phai", "tu", "cac mat", "cong lai",
    ),
    "revenue_list": _REVENUE_WORDS + (
        "lan luot", "moi thang", "tung thang", "moi quy", "tung quy", "theo thu tu", "so", "liet ke", "cac", "thu tu", "thoi gian",
        "lien tiep", "lien nhau", "ket thuc", "cu", "moi", "moi loai", "don", "cac don", "tra bang", "don ban", "ben", "tu cu", "ghi", "tra loi",
    ),
    "weekday_avg": _REVENUE_WORDS + ("trung binh", "moi", "thu"),
    "aov": _REVENUE_WORDS + ("trung binh", "gia tri", "moi don", "mot don", "tren mot don", "1 don", "tren 1 don", "don", "moi", "mot", "tren", "khach", "khach hang", "nhom"),
    "share": _REVENUE_WORDS + (
        "chiem", "ti trong", "ti le", "dong gop", "phan", "phan cua", "phan tram", "bang", "tong", "cac don", "don", "nhom hang", "nhom", "khach",
        "khach hang", "hang", "mat hang", "thuoc", "don ban", "tra bang", "tren", "so voi", "trong", "cac", "ca", "con lai",
    ),
    "change": _REVENUE_WORDS + (
        "tang", "giam", "thay doi", "hon", "kem", "hay", "cao", "thap", "chenh", "chenh lech", "so voi", "so", "cung ky", "truoc", "lien truoc", "ke truoc",
        "truoc do", "phan tram", "ky", "lien", "don", "so don", "len", "xuong", "ngoai", "nam ngoai", "roi", "tang truong", "ky truoc", "bien dong", "muc", "cung thang", "cung quy",
    ),
    "shift_compare": _REVENUE_WORDS + (
        "ca", "sang", "chieu", "tang", "giam", "hon", "kem", "hay", "cao", "thap", "chenh", "chenh lech", "so voi", "so", "phan tram", "so don", "don", "muc",
    ),
    "trend": _REVENUE_WORDS + ("tang", "giam", "lien tuc", "khong", "k", "ko", "hay", "thang", "sau", "cao", "hon", "deu", "luon", "truoc", "co", "dung", "phai"),
    "best_day": _REVENUE_WORDS + ("ngay nao", "cao nhat", "nhat", "dat", "dat nhat", "dat khach", "nhieu nhat", "lon nhat", "khach", "don"),
    "best_month": _REVENUE_WORDS + ("thang nao", "cao nhat", "nhat", "nhieu nhat", "cac", "lon nhat", "so sanh", "dat", "xet"),
    "peak_hour": ("gio cao diem", "gio", "cao diem", "khung gio", "don"),
    "days_threshold": _REVENUE_WORDS + ("so ngay", "bua", "dat", "tro len", "ca ngay", "co", "don", "dem", "dong"),
    "order_count": _ORDER_WORDS + ("khach", "co", "doanh thu", "tien hang", "tro len", "dat", "gia tri", "mat hang", "nhom", "hang", "nhom hang", "mon", "it nhat", "mot", "chua", "san pham"),
    "cancel": _ORDER_WORDS + ("huy", "bi huy", "da huy", "ti le", "phan tram", "don huy", "huy don", "trang thai", "co"),
    "item_rank": (
        "mat hang", "san pham", "sp", "mon", "hang", "ban chay", "chay", "nhat", "ban", "duoc", "nhieu", "hot", "nhung", "ua chuong", "liet ke", "top", "cac",
        "duoc mua", "nhom hang", "best seller", "best", "seller",
    ),
    "customer_rank": (
        "khach", "khach hang", "ai", "nguoi", "mua", "nhieu", "nhat", "tien", "chi tieu", "chi", "cao", "tong", "tong tien", "don", "lan", "so don", "dem", "tinh", "theo",
        "hang", "cho", "trung thanh", "sop", "doi y", "top", "mua hang", "nhung", "liet ke", "cac", "ten", "hay", "tieu", "la",
    ),
    "customer_count": ("khach", "khach hang", "so khach", "so", "mua", "da mua", "co", "dem", "don", "hang"),
    "customers_min_orders": (
        "khach", "khach hang", "so khach", "so", "mua", "da mua", "it nhat", "toi thieu", "tro len", "don", "ti le", "phan tram", "chiem", "trong so", "co", "dem", "tong", "tong so",
        "mua hang", "nhieu",
    ),
    "items_min_orders": ("mat hang", "san pham", "sp", "mon", "hang", "so", "co", "duoc mua", "mua", "xuat hien", "co mat", "mat", "it nhat", "tro len", "don", "khac nhau", "ban", "dem"),
    "staff_rank": ("nhan vien", "tho", "nguoi ban", "ai", "ban", "gioi nhat", "gioi", "nhat", "nhieu", "tien", "cao", "doanh thu", "chay", "co", "la"),
    "staff_days": ("nhan vien", "tho", "ai", "cham chi", "cham", "chi", "nhat", "la"),
    "quantity": (
        "ban", "ban ra", "ban duoc", "duoc", "so", "mat hang", "hang", "san pham", "mua", "mon", "nhieu", "size", "m", "l", "co", "tong", "so ly", "ly", "cai", "dem",
        "cong moi mat hang", "moi mat hang", "tinh theo", "cong moi mon", "cong tat ca cac mon", "cong tat ca", "tinh moi loai do uong", "moi loai do uong", "tinh moi mon", "cong cac mon",
    ),
    "price_on_date": ("gia", "gia ban", "don gia", "mot", "ban", "ban ra", "tien", "moi", "voi"),
    "price_effect": (
        "tang gia", "len gia", "doi gia", "tang", "gia", "len", "ngay", "ke tu", "so", "voi", "ngay truoc", "truoc", "lien truoc", "ban", "ban ra", "duoc", "duoc so voi", "so luong", "hon",
        "giam", "hay", "phan tram", "thay doi", "kem", "nhieu", "it", "khi", "do", "mat hang", "ngay do", "sau", "bao", "tang tu", "doi", "dau", "lien", "ti le", "so voi",
        "tinh theo", "so luong", "luong", "trung binh", "moi",
    ),
    "avg_purchase": ("nhieu", "gia nhap", "nhap", "gia", "trung binh", "tien", "mot", "moi", "1", "vao", "hang", "so", "mat hang", "don vi"),
    "supplier": ("nha cung cap", "ncc", "nhap", "nhieu", "tien", "nhat", "cao", "lon", "chinh", "hang", "tien hang", "tien nhap", "tu", "la", "tong"),
    "purchase_total": (
        "nhap hang", "nhap", "tien nhap", "tien", "hang", "tong", "gia tri", "phieu", "cac phieu", "phieu nhap", "tien hang", "ton", "tong gia tri", "tri", "bo ra", "nhieu", "cac",
    ),
    "purchased_minus_sold": ("nhap", "tru", "ban", "ban ra", "so", "chenh", "lech", "chenh lech", "va", "duoc", "so luong", "lay", "mat hang", "hang"),
    "due": ("don", "ban chiu", "chiu", "ban", "mua chiu", "ngay phai tra", "phai tra", "han tra", "tra", "sau", "muon", "hon", "so", "ghi", "tren", "so sach", "roi", "vao", "co", "cac", "trong", "moi"),
    "debt": (
        "no", "cong no", "con no", "khach", "khach hang", "con", "thieu", "tien", "tong", "so tien", "so", "chua tra", "chua", "tra", "het", "cac", "don", "dat", "qua", "tuoi", "tuoi no",
        "nhom", "chia", "theo", "tren", "top", "lon", "nhat", "nhieu", "kem", "moi", "nguoi", "tung", "bang", "lai", "dang", "thu", "hoi", "thu hoi", "tra no", "tra bot", "bot", "mua chiu", "hon", "hai so", "thu tu", "ghi no them", "ghi", "tra loi", "lan luot", "cach do", "cach", "do",
        "chiu", "mua", "them", "no them", "ban", "duoc", "doi", "doi duoc", "cua", "khoan", "voi", "cac khoan", "lieu", "tach", "ten", "phai", "liet ke", "ky", "mot",
    ),
    "stock": ("ton", "ton kho", "kho", "trong kho", "con", "con lai", "lai", "cuoi", "nguyen", "chan", "le", "duoc", "hien", "so", "trong", "cua", "hang"),
    "expiring_cost": ("het han", "han", "hang", "se", "con", "ton", "tien von", "von", "tien", "toi", "tinh", "gia tri", "trong", "mat hang", "lo", "cac", "han dung", "nam", "dung"),
    "expiring_items": ("het han", "han dung", "han", "hang nao", "sp", "sp nao", "mat hang", "san pham", "nhung", "co", "con", "con hang", "ton", "ton kho", "hang", "hien", "van", "toi", "tinh", "liet ke"),
    "inventory_value": ("tinh theo", "tien hang", "nam trong kho", "gia tri", "tri", "hang", "con", "trong kho", "kho", "ton", "ton kho", "gia von", "von", "theo", "tinh", "tong", "tien", "hien", "cuoi", "so hang"),
    "gross_profit": ("lai gop", "loi nhuan gop", "loi gop", "lai", "gop", "loi nhuan", "tien", "cua", "mat hang", "hang"),
    "net_profit": ("lai rong", "loi nhuan rong", "lai", "rong", "loi nhuan", "tien", "tru het chi phi", "con"),
    "profit_rank": ("hang", "mat hang", "mon", "san pham", "nao", "lai", "loi", "gop", "tong", "cao", "nhat", "nhieu", "lai gop", "tong lai gop", "ca", "mang lai", "loi nhuan gop", "lon"),
    "deliveries": ("dot", "dot giao", "giao", "giao hang", "cho", "cho khach", "xe", "hang", "so", "so dot", "dem", "tong"),
    "unsold": ("mon", "hang", "mat hang", "san pham", "e", "khong", "ban", "duoc", "ra", "ban ra", "cai", "nao", "so", "co", "giai doan", "van", "dang", "gan", "nhat", "cham", "dem", "den", "muc", "con", "sp", "dang ban", "van dang ban", "bi", "e han", "khong ai mua", "ko ai mua", "lan nao"),
    "adjustments": ("voucher", "vau cho", "dung diem", "diem", "phi ship", "phi giao", "phi", "ship", "giam", "tien", "thu", "tong", "khach", "nho", "dung", "giao"),
    "new_members": ("khach", "thanh vien", "thanh vien moi", "moi", "dang ky", "dang ky moi", "so"),
    "tradein": ("thu may cu", "may cu", "thu", "tien", "tong", "may", "cu", "tru", "vao", "don", "cong", "lai"),
    "payable": ("khach phai tra", "khach", "phai tra", "tong", "tien"),
    "service": (
        "phieu", "luot", "xe", "tho", "doanh thu", "tien", "ban", "duoc", "thu", "bao hanh", "ti le", "trung binh", "co", "nhieu", "nhat", "ai", "lam", "so", "chiec", "bien so", "khach quen",
        "xe quen", "lan", "tro len", "toi", "mang", "sua", "vo", "zo", "thay", "dau", "nhot", "may", "lap", "va", "ruot", "bao duong", "bdtq", "dien", "noi", "kim phun", "ve sinh", "vs", "bu gi", "bugi",
        "nuoc mat", "nuoc", "mat", "nhong", "xich", "ac quy", "binh", "tong", "dong", "man", "nhan", "doanh", "cua", "tung", "kim", "phun",
    ),
}

# A rule that uses a number says where. Any other number is a condition it would ignore.
_NUMBER_CONTEXT: dict[str, re.Pattern[str]] = {
    "item_rank": re.compile(r"\btop (\d+)\b|\b(\d+) (?:mat hang|san pham|mon|hang|sp|best seller)\b"),
    "customer_rank": re.compile(r"\btop (\d+)\b|\b(\d+) (?:khach|nguoi)\b"),
    "profit_rank": re.compile(r"\btop (\d+)\b|\b(\d+) (?:mat hang|san pham|mon|hang)\b"),
    "items_min_orders": re.compile(r"\b(\d+) don\b"),
    "customers_min_orders": re.compile(r"\b(\d+) don\b"),
    "price_effect": re.compile(r"\b(\d+) ngay\b"),
    "debt": re.compile(r"\btop (\d+)\b|\b(\d+) (?:khach|nguoi)\b|\b(\d+) ngay\b|\b\d+\s*[–—-]\s*\d+\b|\b(?:tren|qua|hon) (\d+)\b"),
    "expiring_cost": re.compile(r"\b(\d+) ngay\b"),
    "change": re.compile(r"\b(\d+) ngay\b"),
    "expiring_count": re.compile(r"\b(\d+) ngay\b"),
    "expiring_items": re.compile(r"\b(\d+) ngay\b"),
    "aov": re.compile(r"\b1 don\b"),
    "price_on_date": re.compile(r"\b1 \w+\b"),
    "avg_purchase": re.compile(r"\b1 \w+\b"),
    "revenue_list": re.compile(r"\b(\d+) (?:so|thang|quy)\b"),
    "trend": re.compile(r"\b(\d+) thang\b"),
    "best_month": re.compile(r"\b(\d+) thang\b"),
    "service": re.compile(r"\b(\d+) (?:lan|phieu|luot)\b"),
}
_PERIOD_COUNT_METRICS = {"revenue_list", "trend", "best_month"}
_METRICS_WITH_UNITS = {"attribute_quantity", "attribute_share", "quantity", "stock", "avg_purchase", "price_on_date", "price_effect", "purchased_minus_sold"}


def _remove_phrases(text: str, phrases: tuple[str, ...]) -> tuple[str, set[str]]:
    """Remove multi-word phrases, longest first, so `cua hang` never leaves a stray `hang`. Return single words too."""
    singles = set()
    for phrase in sorted({item.strip() for item in phrases}, key=len, reverse=True):
        if " " in phrase or any(ch.isdigit() for ch in phrase):
            text = re.sub(r"\b" + re.escape(phrase) + r"\b", " ", text)
        else:
            singles.add(phrase)
    return text, singles


def unbound_terms(text: str, metric: str, unit_words: set[str] | None = None) -> list[str]:
    """Words and numbers in canonical question text that `metric`'s rule does not account for."""
    if metric in _PERIOD_COUNT_METRICS:
        # A number directly before a period placeholder counts the periods when it equals how many are listed.
        periods = len(_PLACEHOLDER_RE.findall(text))
        words = {"hai": 2, "ba": 3, "bon": 4, "nam": 5, "sau": 6}
        text = re.sub(r"\b(\d+|hai|ba|bon|nam|sau)(?= <)", lambda m: " " if words.get(m.group(1), m.group(1)) in (periods, str(periods)) else m.group(0), text)
    if metric == "chain_claim":
        text = re.sub(r"\b\d+(?= <)", " ", text)
    remaining = _TAG_QUESTION_RE.sub(" ", _PLACEHOLDER_RE.sub(" ", text))
    context = _NUMBER_CONTEXT.get(metric)
    if context:
        # Keep the words of a number's context for the vocabulary check; only the digits are consumed.
        remaining = context.sub(lambda m: re.sub(r"\d+", " ", m.group(0)), remaining)
    if unit_words and metric in _METRICS_WITH_UNITS:
        # Before tokenizing, because units can contain digits ("4m3").
        remaining, unit_singles = _remove_phrases(remaining, tuple(unit_words))
    else:
        unit_singles = set()
    remaining, singles = _remove_phrases(remaining, tuple(_RULE_VOCAB.get(metric, ())) + _FILLER_PHRASES)
    allowed = singles | unit_singles | _FILLER_WORDS
    return [term for term in _WORD_RE.findall(remaining) if term not in allowed]
