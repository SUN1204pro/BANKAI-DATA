"""Unit hints: "Bia Tiger lon: 1 thùng = 24 lon" for the products the question mentions."""
from __future__ import annotations

from ..normalize import norm
from ..units import UnitCatalog, UnitRule
from .base import Finding, Query, Resolver

MAX_PRODUCTS = 6


def _group(rules: list[UnitRule]) -> str:
    return "; ".join(r.render() for r in sorted(rules, key=lambda r: r.factor))


class UnitResolver(Resolver):
    name = "units"
    section = "UNIT CONVERSIONS"
    claims = False

    def __init__(self, catalog: UnitCatalog | None):
        self.cat = catalog

    def resolve(self, q: Query) -> list[Finding]:
        cat = self.cat
        if not cat:
            return []
        index = cat.by_product()
        # products named in the question: grounded cell values first, then names fully contained in the question
        mentioned: dict[str, tuple[str, int, int]] = {}   # accent-free name -> (name as written, position)
        for f in q.found:
            if f.resolver == "entity" and f.kind == "product" and f.value.score >= 0.65:
                mentioned.setdefault(norm(f.value.value), (f.value.value, f.start, f.end))
        for name in cat.covering(q.text):
            mentioned.setdefault(norm(name), (name, 0, 0))
        out: list[Finding] = []
        for name, start, end in list(mentioned.values())[:MAX_PRODUCTS]:
            rules = index.get(norm(name))
            line = f"{name}: {_group(rules)}" if rules else f"{name}: no conversion rule, its quantity is already in the base unit (factor 1)"
            out.append(self.finding(q, "product", start, end, line, rules or []))
        if not any(f.value for f in out):
            out += self._by_unit(q)
        if out and cat.converted_columns:
            end = len(q.text)  # sorts after the product lines
            out.append(self.finding(q, "columns", end, end, "quantity already converted to the base unit: " + ", ".join(cat.converted_columns[:3])
                                    + " (otherwise multiply the line quantity by the factor of the line's own unit)"))
        return out

    def _by_unit(self, q: Query) -> list[Finding]:
        """The question names a large unit ("bao nhiêu thùng") but no product that has a rule: list who has it."""
        out = []
        units = {u.split()[0]: u for u in self.cat.big_units}
        for w in q.words():
            u = units.get(w.text)
            if not u:
                continue
            rules = self.cat.by_unit(u)
            groups: dict[tuple[float, str], list[str]] = {}
            for r in rules:
                groups.setdefault((r.factor, r.base), []).append(r.product)
            for (factor, base), prods in list(groups.items())[:3]:
                shown = ", ".join(prods[:4]) + (f" (+{len(prods) - 4} more)" if len(prods) > 4 else "")
                out.append(self.finding(q, "unit", w.start, w.end, f"1 {rules[0].unit} = {factor:g} {base}  for: {shown}", rules))
        return out
