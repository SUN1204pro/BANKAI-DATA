"""Vietnamese text folding, Telex repair, and slang canonicalization for question parsing.

Matching happens on folded text (no diacritics, lowercase, `đ` -> `d`) because roughly a quarter of
questions are typed without diacritics or with raw Telex keystrokes (`tieenf`, `thangs`, `luwowtj`).
Output strings never come from here: answers keep the source spelling.
"""

from __future__ import annotations

import functools
import re
import unicodedata

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


@functools.lru_cache(maxsize=200000)
def fold(text: str) -> str:
    """Lowercase, strip diacritics, and map `đ` to `d`. Keeps punctuation and spacing."""
    s = unicodedata.normalize("NFC", text).lower().replace("đ", "d")
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", s)


@functools.lru_cache(maxsize=200000)
def fold_key(text: str) -> str:
    """Folded text with punctuation removed and whitespace collapsed: a lookup key for names."""
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", fold(text)).replace("_", " ")).strip()


# Folded words that question patterns rely on. Telex repair only rewrites an unknown word into one of
# these, so product and person names (`tiger`, `lan`) are left alone.
VOCAB = frozenset(
    """
    a ai an anh ba ban bang bao bat be bi bia biet bien binh bo bot bua ca cac cai cao chai chi chia chiem
    chieu chinh cho chot chua chuyen co con cong cu cua cuoi cung da dac dai dan dang dat dau day den deu dem
    di dia dich dien dinh do doanh doi don dong dot du dung duoc dua dau gan gap gi gia giam gian giao gio giup
    giua gom goi gop gui ha hai han hang hanh hay het hien hieu hoa hoat hoi hom hon hot hop huy ke kem khach
    khac khai khoan khong khu kia ky la lai lan lau len lieu lien liet lo loai loi lon luot luong ly ma mat may
    moi mon mot mua muc muon nam nao nay ngay nghi nghiep ngoai ngu nguoi nhan nhap nhat nhe nhieu nho nhom nhu
    nhung no noi nua o on phai phan phi phieu phu qua quan quay quy ra rang roi rieng sang sau si so sot sua
    sup tai tang tet tham than thang thanh thap the them theo thi thiet tho thoi thu thuoc thuong ti tien tiep
    tiem tinh toan toi ton tong tot tra trai tren tro trong truoc trung tu tuan tuoi tung tuy uong va van ve vi
    voi vua xem xe xet xin vay gium nha le tram cuoi het chan tru
    vao thoai xuat it nhi thai e tieu chiu lech
    """.split()
)

_TELEX_PAIRS = (("aa", "a"), ("aw", "a"), ("ee", "e"), ("oo", "o"), ("ow", "o"), ("uw", "u"), ("dd", "d"))
_TONE_LETTERS = "sfrxj"


@functools.lru_cache(maxsize=100000)
def repair_telex(word: str) -> str:
    """Undo raw Telex keystrokes in a folded word when the result is a known word."""
    if word in VOCAB or not word.isalpha() or len(word) < 3:
        return word
    candidate = word
    for src, dst in _TELEX_PAIRS:
        candidate = candidate.replace(src, dst)
    if candidate in VOCAB:
        return candidate
    # Tone keystroke can land at the end (`thangs`) or mid-word before a final consonant (`nhoms`).
    if candidate[-1] in _TONE_LETTERS and candidate[:-1] in VOCAB:
        return candidate[:-1]
    # A tone key follows a vowel. `tru` must stay `tru`, not lose its `r` and become `tu`.
    for i in range(1, len(candidate) - 1):
        if candidate[i] in _TONE_LETTERS and candidate[i - 1] in "aeiouy":
            removed = candidate[:i] + candidate[i + 1 :]
            if removed in VOCAB:
                return removed
    return word


# Ordered phrase rewrites on folded text. Word boundaries are explicit so names survive.
_SLANG: list[tuple[str, str]] = [
    (r"\bbq\b|\bbinh quan\b|\bbin quan\b", "trung binh"),
    (r"\btrog\b", "trong"),
    (r"\bee\b", "e"),
    (r"\br\b", "roi"),
    (r"\bnhut\b|\bnhứt\b", "nhat"),
    (r"\bjum\b|\bdup\b", "giup"),
    (r"\bkua\b", "cua"),
    (r"\bkon\b", "con"),
    (r"\btih\b", "tinh"),
    (r"\bngui\b", "nguoi"),
    (r"\bdag\b", "dang"),
    (r"\bqa\b", "qua"),
    (r"\bnhieudon\b", "nhieu don"),
    (r"\bngaynao\b", "ngay nao"),
    (r"\bbannhieu\b", "ban nhieu"),
    (r"\b(?:tinh|ke) (?:ca|luon) hom nay(?: thi)?\b", " "),
    (r"\bmung 1 thang\b", "dau thang"),
    (r"\bthang (?:mot|gieng)\b", "thang 1"),
    (r"\bthang hai\b", "thang 2"),
    (r"\bthang ba\b(?! thang)", "thang 3"),
    (r"\bthang tu\b(?= (?:\d|nam|la|co|ban|doanh|cua|<)|$)", "thang 4"),
    (r"\bthang sau\b(?= (?:\d|nam))", "thang 6"),
    (r"\btop ba\b", "top 3"),
    (r"\btop nam\b", "top 5"),
    (r"\btop muoi\b", "top 10"),
    (r"\bdthu\b|\bdoanh so\b|\brevenue\b|\bdoanh thuu\b", "doanh thu"),
    (r"\bbnhiu\b|\bbnhui\b|\bbn\b|\bbao nhiu\b|\bbao nheiu\b|\bbao nhieuu\b|\bbnh\b", "bao nhieu"),
    (r"\bnhiu\b|\bnhìu\b", "nhieu"),
    (r"\b(?:mi|mí)\b(?=\s*(?:%|phan tram|ngay|don))", "may"),
    (r"\bhnay\b|\bhom ni\b|\bbua nay\b|\bbua ni\b|\bhum nay\b", "hom nay"),
    (r"\bhqua\b|\bbua qua\b|\bhum qua\b", "hom qua"),
    (r"\bhkia\b|\bbua kia\b", "hom kia"),
    (r"\bdc\b|\bđc\b|\bduocj\b", "duoc"),
    (r"\bko\b|\bk\b(?=\s+(?:ban|co|tinh))|\bkhong\b", "khong"),
    (r"\bvs\b", "voi"),
    (r"\btrc\b|\btruoc do\b|\brui\b", "truoc"),
    (r"\bwan\b", "quan"),
    (r"\bdh\b|\bđh\b|\bo do\b|\border\b|\bbill\b|\bhoa don\b|\bddown\b|\bddon\b", "don"),
    (r"\bdon hang\b", "don"),
    (r"\bshop\b|\bsop\b|\bsốp\b|\btiem\b|\bquan\b|\bcua tiem\b|\bcuwar hang\b", "cua hang"),
    (r"\btotal\b|\btong cong\b", "tong"),
    (r"\bmik\b|\bminh\b|\bem\b|\banh\b(?=\s+(?:xem|coi))", "toi"),
    (r"\bmo\b(?=\s|$)", "nao"),
    (r"\bni\b", "nay"),
    (r"\bthe nao\b", "nhu nao"),
]
_SLANG_COMPILED = [(re.compile(p), r) for p, r in _SLANG]

# `th11/2025`, `t8/2025`, `th 9/25`, `T1/2025` -> `thang 11/2025`.
_MONTH_ABBR_RE = re.compile(r"(?<![\w/])(?:thang|th|t)\s*(?=\d{1,2}(?:\s*[/\-.]\s*\d{2,4}|\b))")
_THANG_WORD_RE = re.compile(r"\bth\b(?=\s+(?:nay|truoc|roi|lien|ke|do|sau))")


@functools.lru_cache(maxsize=100000)
def prenormalize_question(question: str, keep: frozenset[str] = frozenset()) -> str:
    """Fold and repair Telex only: entity names are still matchable in this form.

    Words in `keep` come from the shop's own names, so they are never repaired: `Air-X` must not become `ai x`.
    """
    text = fold(question)
    text = re.sub(r"[^\w\s%/\-.,:;()≥≤<>=+–—]", " ", text, flags=re.UNICODE)
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0) if m.group(0) in keep else repair_telex(m.group(0)), text, flags=re.UNICODE)


def apply_slang(text: str) -> str:
    """Expand slang and month abbreviations on prenormalized text."""
    for pat, rep in _SLANG_COMPILED:
        text = pat.sub(rep, text)
    text = _THANG_WORD_RE.sub("thang", text)
    text = _MONTH_ABBR_RE.sub("thang ", text)
    return _SPACE_RE.sub(" ", text).strip()


def canonicalize_question(question: str) -> str:
    """Fold a question, repair Telex, expand slang, and normalize month abbreviations."""
    return apply_slang(prenormalize_question(question))
