"""Unit conversion rules of one shop: "1 thùng = 24 lon" for a product.

Shops record them in different places, so three sources are read and merged:
  * a conversion table (`DanhSachQuyDoi`, `DanhMuc`, `don_vi_sp`, ...), recognised by its column headers.
    Layouts differ: product by name or by code, base unit given or only in the product list.
  * README lines like "- Bia Tiger lon: 1 thùng = 24 lon".
  * a quantity column already converted to the base unit ("Số lượng quy đổi"), reported so it can be used.
A product with no rule is counted in its base unit with factor 1 (rulebook §10).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from .normalize import norm, tokens


def _compact(s) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(s))


# Matched against the compact, accent-free ORIGINAL header of a column.
_FACTOR = re.compile(r"^(tile|tyle|heso|bang$|quydoi$)")
_BIG = re.compile(r"^(donviquydoi|dvnhap|dvlon|donvilon|dvt|donvitinh|donvi)$")
_BASE = re.compile(r"^(donvicoban|donvigoc|dvnho|donvicoso)")
_NAME = re.compile(r"^(tensanpham|tenmon|tenhanghoa|tenhang|tengoi|hang|mon|sanpham)$")
_CODE = re.compile(r"^(mahanghoa|mahang|masanpham|masp|masku|mahh|mamon)$")
_CONVERTED = re.compile(r"soluongquydoi|slquydoi|slqd|soluongqd|slquydoidv")
_README_RULE = re.compile(r"^\s*[-*]\s*(?P<name>.+):\s*1\s+(?P<unit>[^=]+?)\s*=\s*(?P<n>\d+(?:[.,]\d+)?)\s*(?P<base>\S.*?)\s*$")


@dataclass(frozen=True)
class UnitRule:
    product: str
    unit: str       # the larger unit ("thùng")
    factor: float   # base units in one `unit`
    base: str       # the base unit ("lon"); "" when the shop's files do not say
    source: str

    def render(self) -> str:
        n = f"{self.factor:g}"
        return f"1 {self.unit} = {n} {self.base}".rstrip()


@dataclass
class UnitCatalog:
    rules: list[UnitRule] = field(default_factory=list)
    converted_columns: list[str] = field(default_factory=list)  # "table.column" already in base units

    def __bool__(self) -> bool:
        return bool(self.rules or self.converted_columns)

    @property
    def big_units(self) -> set[str]:
        return {norm(r.unit) for r in self.rules}

    def by_product(self) -> dict[str, list[UnitRule]]:
        out: dict[str, list[UnitRule]] = {}
        for r in self.rules:
            out.setdefault(norm(r.product), []).append(r)
        return out

    @classmethod
    def from_business(cls, biz) -> "UnitCatalog":
        return cls.build(biz.readme, biz.tables, biz.origin)

    @classmethod
    def build(cls, readme: str, tables: dict[str, pd.DataFrame], origin: dict[str, dict[str, str]] | None = None) -> "UnitCatalog":
        origin = origin or {}
        head = {t: {c: _compact(origin.get(t, {}).get(c, c)) for c in df.columns} for t, df in tables.items()}

        def find(t: str, pat: re.Pattern) -> str | None:
            return next((c for c, h in head[t].items() if pat.search(h)), None)

        # product master: code -> (name, base unit). A column with unique codes is a product list, not sales lines.
        names: dict[str, str] = {}
        bases: dict[str, str] = {}
        for t, df in tables.items():
            code, name = find(t, _CODE), find(t, _NAME)
            if not (code and name) or not df[code].is_unique:
                continue
            unit = find(t, _BASE) or find(t, _BIG)
            for i, c in enumerate(df[code].astype(str)):
                names.setdefault(c, str(df[name].iat[i]))
                if unit and pd.notna(df[unit].iat[i]):
                    bases.setdefault(c, str(df[unit].iat[i]))

        rules: list[UnitRule] = []
        converted: dict[str, list[str]] = {}
        for t, df in tables.items():
            for c, h in head[t].items():
                if _CONVERTED.search(h):
                    converted.setdefault(c, []).append(t)
            fcol, ucol = find(t, _FACTOR), find(t, _BIG)
            ncol, code, bcol = find(t, _NAME), find(t, _CODE), find(t, _BASE)
            if not (fcol and ucol and (ncol or code)) or ucol == bcol:
                continue
            fac = pd.to_numeric(df[fcol], errors="coerce")
            for i in range(len(df)):
                f, u = fac.iat[i], df[ucol].iat[i]
                if pd.isna(f) or f <= 0 or pd.isna(u):
                    continue
                key = str(df[code].iat[i]) if code else ""
                prod = str(df[ncol].iat[i]) if ncol else names.get(key, key)
                base = str(df[bcol].iat[i]) if bcol else bases.get(key, "")
                if pd.isna(prod) or not prod or norm(u) == norm(base):
                    continue
                rules.append(UnitRule(prod, str(u), float(f), base, t))

        for line in readme.splitlines():
            m = _README_RULE.match(line)
            if m:
                rules.append(UnitRule(m["name"].strip(), m["unit"].strip(), float(m["n"].replace(",", ".")), m["base"].strip(), "README"))

        seen: set = set()
        uniq = []
        for r in rules:
            k = (norm(r.product), norm(r.unit), r.factor)
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        cols = [f"{ts[0]}.{c}" + (f" (+{len(ts) - 1} same-named tables)" if len(ts) > 1 else "") for c, ts in converted.items()]
        return cls(uniq, cols)

    def for_products(self, products: list[str]) -> dict[str, list[UnitRule]]:
        """Rules of the given product names (matched accent-free and case-insensitive)."""
        index = self.by_product()
        return {p: index.get(norm(p), []) for p in products}

    def covering(self, question: str) -> list[str]:
        """Product names whose every word occurs in the question (fallback when grounding found nothing)."""
        qt = set(tokens(question))
        out = []
        for key, rs in self.by_product().items():
            pt = set(tokens(key))
            if pt and pt <= qt:
                out.append(rs[0].product)
        return out

    def by_unit(self, unit: str) -> list[UnitRule]:
        return [r for r in self.rules if norm(r.unit) == unit]
