"""Entity grounding: map words in the question to real cell values.

"trà sữa" -> products.ten_hang ~ 'Trà sữa trân châu'. Works on accent-free, lower-case tokens with IDF weighting,
plus a typo-tolerant fallback. Every searchable column is given an entity type (product, customer, staff,
category, payment) so the prompt can say what a phrase refers to, and phrases that fit several values are
reported as ambiguous with the options listed instead of silently picking one.
"""
from __future__ import annotations

import difflib
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from .normalize import norm, tokens

ENTITY_TYPES = ("product", "customer", "staff", "category", "payment", "other")


# ------------------------------------------------------------- entity types
# Matched, in this order, against the accent-free column name without underscores (+ its original header).
_COLUMN_RULES: list[tuple[str, re.Pattern]] = [
    ("other", re.compile(r"soluong|tongtien|thanhtien|dongia|doanhthu|trangthai|ghichu")),  # "so_luong_san_pham" is a quantity
    ("payment", re.compile(r"hinhthuc(thanh)?toan|phuongthuc|pttt|kenhthanhtoan|loaithanhtoan|hinhthuctt|paymentmethod")),
    ("staff", re.compile(r"nhanvien|nvban|nvbh|thungan|nguoiban|nguoilap|nguoitao|salesperson|cashier")),
    ("customer", re.compile(r"khach|nguoimua|customer")),
    ("category", re.compile(r"nhomhang|nhomsp|loaihang|loaisp|loaisanpham|danhmuc|phanloai|nganhhang|nhom|category")),
    ("product", re.compile(r"tenhang|tensanpham|tensp|tenmathang|mathang|hanghoa|sanpham|tenthuoc|thuoc|hoatchat|tenmon|tenvattu|vattu|tenhh|product|item")),
]
# A bare "ten"/"ho_ten" column takes the type of its table.
_TABLE_RULES: list[tuple[str, re.Pattern]] = [
    ("staff", re.compile(r"nhanvien|staff|employee")),
    ("customer", re.compile(r"khachhang|customer")),
    ("category", re.compile(r"nhomhang|danhmuc|category")),
    ("product", re.compile(r"sanpham|hanghoa|mathang|thuoc|product|menu|item")),
]
_NAME_ONLY = re.compile(r"^(ten|hoten|name|tenkhac)$")
_HAS_LETTER = re.compile(r"[^\W\d_]")
_PAYMENT_VALUE = re.compile(r"tien mat|chuyen khoan|\bcod\b|momo|zalopay|vnpay|the (tin dung|ngan hang)|\bvisa\b|vi dien tu|\bqr\b|ck\b|\bpos\b")


def classify(table: str, column: str, origin: str = "", values: list[str] | None = None) -> str:
    """Entity type of a text column, from its name, its table, and (for payment methods) its values."""
    col = norm(column).replace(" ", "").replace("_", "")
    head = norm(origin).replace(" ", "").replace("_", "")
    for etype, pat in _COLUMN_RULES:
        if pat.search(col) or (head and pat.search(head)):
            return etype
    tab = norm(table).replace(" ", "").replace("_", "")
    if _NAME_ONLY.match(col):
        for etype, pat in _TABLE_RULES:
            if pat.search(tab):
                return etype
    if values and len(values) <= 15 and sum(bool(_PAYMENT_VALUE.search(norm(v))) for v in values) * 2 >= len(values):
        return "payment"
    return "other"


# ------------------------------------------------------------- n-gram index
def _grams(word: str) -> set[str]:
    w = f"^{word}$"
    return {w[i:i + 2] for i in range(len(w) - 1)}


class NgramIndex:
    """Typo lookup without scanning the vocabulary: candidates share character bigrams with the query word.

    Same contract as `difflib.get_close_matches(word, vocab, n=1, cutoff)`: the closest word whose
    SequenceMatcher ratio is >= cutoff (ties: the larger string), else None. Bigram postings pick the few
    candidates worth scoring; the ratio itself is only computed on those.
    """

    def __init__(self, vocab: list[str], cutoff: float = 0.84, keep: int = 40):
        self.cutoff, self.keep = cutoff, keep
        self.vocab = vocab
        self.post: dict[str, list[int]] = defaultdict(list)
        for i, w in enumerate(vocab):
            for g in _grams(w):
                self.post[g].append(i)

    def closest(self, word: str) -> str | None:
        la, c = len(word), self.cutoff
        lo, hi = la * c / (2 - c), la * (2 - c) / c  # ratio <= 2*min(la,lb)/(la+lb) prunes by length
        hits: dict[int, int] = defaultdict(int)
        for g in _grams(word):
            for i in self.post.get(g, ()):
                hits[i] += 1
        pool = [(n, i) for i, n in hits.items() if lo <= len(self.vocab[i]) <= hi]
        pool.sort(reverse=True)
        sm = difflib.SequenceMatcher()
        sm.set_seq2(word)
        best: tuple[float, str] | None = None
        for _, i in pool[: self.keep]:
            cand = self.vocab[i]
            sm.set_seq1(cand)
            if sm.real_quick_ratio() < c or sm.quick_ratio() < c:
                continue
            r = sm.ratio()
            if r >= c and (best is None or (r, cand) > best):
                best = (r, cand)
        return best[1] if best else None


# ------------------------------------------------------------------ results
@dataclass
class Hint:
    table: str
    column: str
    value: str
    score: float
    etype: str = "other"
    also: int = 0  # same column name + value in this many sibling tables (monthly splits of one sheet)

    def render(self) -> str:
        more = f" (+{self.also} same-named tables)" if self.also else ""
        return f"{self.table}.{self.column} ~ '{self.value}' (match {self.score:.2f}) [{self.etype}]{more}"


@dataclass
class Group:
    """Values of one column that a phrase of the question could mean. `ambiguous` = more than one fits."""
    etype: str
    table: str
    column: str
    options: list[Hint] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return len(self.options) > 1


class Grounder:
    def __init__(self, tables: dict[str, pd.DataFrame], max_distinct: int = 50000,
                 origin: dict[str, dict[str, str]] | None = None):
        self.cols: list[tuple[str, str]] = []
        self.etypes: list[str] = []
        self.values: list[list[str]] = []
        self.vtoks: list[list[list[str]]] = []
        self.df: list[dict[str, int]] = []
        self.inv: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for tname, frame in tables.items():
            for col in frame.columns:
                s = frame[col]
                if pd.api.types.is_numeric_dtype(s):
                    continue
                distinct = [str(v) for v in s.dropna().astype(object).unique()]
                if not distinct or len(distinct) > max_distinct:
                    continue
                cid = len(self.cols)
                self.cols.append((tname, col))
                self.etypes.append(classify(tname, col, (origin or {}).get(tname, {}).get(col, ""), distinct))
                self.values.append(distinct)
                toks = [list(dict.fromkeys(tokens(v))) for v in distinct]
                self.vtoks.append(toks)
                dfc: dict[str, int] = defaultdict(int)
                for vid, tl in enumerate(toks):
                    for t in tl:
                        dfc[t] += 1
                        self.inv[t].append((cid, vid))
                self.df.append(dfc)
        self._vocab = list(self.inv)
        self._ngrams = NgramIndex(self._vocab)

    def _w(self, cid: int, tok: str) -> float:
        n = len(self.values[cid])
        return math.log(1 + n / self.df[cid].get(tok, 1))

    def _scored(self, question: str) -> list[tuple[float, int, int]]:
        qtoks = list(dict.fromkeys(tokens(question)))
        matched: list[tuple[str, float]] = []
        for t in qtoks:
            if t in self.inv:
                matched.append((t, 1.0))
            elif len(t) >= 4:
                near = self._ngrams.closest(t)
                if near:
                    matched.append((near, 0.8))
        acc: dict[tuple[int, int], float] = defaultdict(float)
        for t, factor in matched:
            for cid, vid in self.inv[t]:
                n = len(self.values[cid])
                if n > 50 and self.df[cid][t] > 0.3 * n:
                    continue  # token too common in this column to be informative
                acc[(cid, vid)] += factor * self._w(cid, t)
        scored: list[tuple[float, int, int]] = []
        for (cid, vid), s in acc.items():
            if not _HAS_LETTER.search(self.values[cid][vid]):
                continue  # "3", "-3", "3/10": numbers and dates identify no entity
            total = sum(self._w(cid, t) for t in self.vtoks[cid][vid]) or 1.0
            score = s / total
            if score < 0.5:
                continue
            if self.etypes[cid] == "other" and len(self.values[cid]) > 40 and score < 0.9:
                continue  # a loose match in a free-form column ("1 phần" in a quantity column) says nothing
            scored.append((score, cid, vid))
        scored.sort(key=lambda x: (-x[0], len(self.values[x[1]][x[2]])))
        return scored

    def _hint(self, score: float, cid: int, vid: int) -> Hint:
        return Hint(self.cols[cid][0], self.cols[cid][1], self.values[cid][vid], score, self.etypes[cid])

    def hints(self, question: str, top: int = 10, per_col: int = 3) -> list[Hint]:
        """Best cell values. Typed entities come before "other" columns (units, status...), and the same
        column/value repeated in sibling tables (monthly splits) is listed once with a count."""
        scored = self._scored(question)
        scored.sort(key=lambda x: self.etypes[x[1]] == "other")  # stable: keeps score order inside each group
        out: list[Hint] = []
        count: dict[int, int] = defaultdict(int)
        seen: dict[tuple[str, str], Hint] = {}
        for score, cid, vid in scored:
            key = (self.cols[cid][1], self.values[cid][vid])
            if key in seen:
                seen[key].also += 1
                continue
            if count[cid] >= per_col:
                continue
            count[cid] += 1
            seen[key] = h = self._hint(score, cid, vid)
            out.append(h)
        out = out[:top]
        out.sort(key=lambda h: (h.etype == "other", -h.score))
        return out

    def groups(self, question: str, margin: float = 0.1, max_options: int = 6) -> list[Group]:
        """Per column: the best value and every rival within `margin` of it (unless the best is a full match)."""
        by_col: dict[int, list[tuple[float, int]]] = defaultdict(list)
        for score, cid, vid in self._scored(question):
            by_col[cid].append((score, vid))
        groups = []
        for cid, items in by_col.items():
            best = items[0][0]
            keep = items[:1] if best >= 0.999 else [x for x in items if x[0] >= best - margin][:max_options]
            groups.append(Group(self.etypes[cid], *self.cols[cid], [self._hint(s, cid, v) for s, v in keep]))
        groups.sort(key=lambda g: -g.options[0].score)
        return groups
