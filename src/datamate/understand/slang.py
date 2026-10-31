"""Abbreviations and amounts written in shorthand ("dthu", "bnhiu", "5k").

The word list lives in slang.json (not in code). Two resolvers share it:
  * MoneyResolver  - a number glued to k / tr is an amount: "5k" = 5.000 đồng, "2,5tr" = 2.500.000 đồng.
  * SlangResolver  - every other word that is a known abbreviation; a bare "k" is "không", never "nghìn".
Money must run before slang so that the "k" of "5k" is not read a second time.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..normalize import norm
from .base import Finding, Query, Resolver

SLANG_FILE = Path(__file__).with_name("slang.json")

# Not an amount when the number is a calendar field or part of a date/ratio: "tháng 5 k kê đơn", "7/2025 k", "1.5".
_TIME_WORDS = {"thang", "nam", "ngay", "quy", "tuan", "top", "lan", "hom", "tu", "den"}


@lru_cache(maxsize=None)
def load_slang(path: Path = SLANG_FILE) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"plain": data["plain"], "after_number": data["after_number"]}


def parse_number(s: str) -> float:
    """Vietnamese number: '1.500' thousands separator, '2,5' / '2.5' decimal."""
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        return float(s.replace(".", ""))
    return float(s.replace(",", "."))


def expand_slang(text: str, plain: dict[str, str] | None = None) -> tuple[str, list[int]]:
    """Accent-free `text` with every known abbreviation replaced by its (accent-free) expansion.

    Returns the new text and, for each of its characters, the index in `text` it came from, so a match
    on the expanded text can be traced back to the words the user wrote ("bnhiu" -> "bao nhieu").
    """
    plain = plain or load_slang()["plain"]
    out: list[str] = []
    origin: list[int] = []
    last = 0
    for m in re.finditer(r"[a-z0-9]+", text):
        exp = plain.get(m.group(0))
        out.append(text[last:m.start()])
        origin += range(last, m.start())
        piece = norm(exp.split(" / ")[0]) if exp else m.group(0)
        out.append(piece)
        origin += [m.start()] * len(piece) if exp else range(m.start(), m.end())
        last = m.end()
    out.append(text[last:])
    origin += range(last, len(text))
    return "".join(out), origin


class MoneyResolver(Resolver):
    name = "money"
    section = "AMOUNTS"

    def __init__(self, table: dict | None = None):
        units = (table or load_slang())["after_number"]
        self.units = units
        alt = "|".join(sorted(map(re.escape, units), key=len, reverse=True))
        self._pat = re.compile(rf"(?<![a-z0-9/.,])(\d{{1,3}}(?:\.\d{{3}})+|\d+(?:[.,]\d+)?)( ?)({alt})(?![a-z0-9])")

    def resolve(self, q: Query) -> list[Finding]:
        text = q.norm.text
        out = []
        for m in self._pat.finditer(text):
            if m.group(2):  # "5 k": only if the number does not belong to a date/period ("tháng 5 k ...")
                prev = text[: m.start()].split()
                if prev and prev[-1] in _TIME_WORDS:
                    continue
            unit = self.units[m.group(3)]
            amount = parse_number(m.group(1)) * unit["mult"]
            amount = int(amount) if amount == int(amount) else amount
            start, end = q.norm.to_raw(m.start(), m.end())
            shown = f"{amount:,}".replace(",", ".")
            out.append(self.finding(q, "amount", start, end, f'"{q.text[start:end]}" = {shown} đồng ({m.group(1)} {unit["expand"]})', amount))
        return out


class SlangResolver(Resolver):
    name = "slang"
    section = "ABBREVIATION HINTS"

    def __init__(self, table: dict | None = None):
        self.plain = (table or load_slang())["plain"]

    def resolve(self, q: Query) -> list[Finding]:
        return [
            self.finding(q, "abbr", w.start, w.end, f"{w.text} = {self.plain[w.text]}", self.plain[w.text])
            for w in q.words()
            if w.text in self.plain
        ]
