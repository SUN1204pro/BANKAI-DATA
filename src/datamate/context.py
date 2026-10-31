"""Assemble everything the model needs about one question (all deterministic).

The rulebook is NOT added here: it is identical for every question, so it lives in the shared
system prompt (see prompts.build_system) where vLLM's prefix cache computes it once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .ingest import Business
from .rules import RuleBook
from .lunar import LunarCalendar, recurring_lunar_days
from .timeparse import Conventions, Span
from .understand import (AmbiguityResolver, BasisResolver, CoverageResolver, GroundResolver, LunarResolver, MoneyResolver,
                         QuestionTypeResolver, Query, ShiftResolver, SlangResolver, TimeResolver, UnitResolver, rules_for,
                         time_columns, understand)


@dataclass
class Context:
    as_of: date
    spans: list[Span] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
    hints: list[str] = field(default_factory=list)


def build_context(
    question: str,
    as_of: date,
    biz: Business,
    cal: LunarCalendar | None,
    conv: Conventions,
    rules: RuleBook | None = None,
) -> Context:
    # Priority = list order. Money first ("5k" is an amount, not the abbreviation "k"); the lunar resolver
    # before the plain time one ("năm 2025" inside "Tết năm 2025" belongs to Tết).
    resolvers = [MoneyResolver()]
    if cal is not None:
        resolvers.append(LunarResolver(cal))
    resolvers += [
        TimeResolver(conv),
        ShiftResolver(time_columns(biz.tables)),
        BasisResolver(biz.date_range[0] if biz.date_range else None),  # reads the dates found above
        CoverageResolver(biz.date_range),
        SlangResolver(),
        GroundResolver(biz.grounder),
        UnitResolver(biz.units),  # reads the products the grounder found
        AmbiguityResolver(biz.grounder),
        QuestionTypeResolver(),
    ]
    found = understand(Query(question, as_of), resolvers)

    def lines(name: str) -> list[str]:
        return list(dict.fromkeys(f.line for f in found if f.resolver == name))

    spans: list[Span] = [f.value for f in found if f.resolver in ("time", "lunar")]
    extra_lines: list[str] = []
    if cal is not None:
        for lday in recurring_lunar_days(question):
            windows = [(s.start, s.end) for s in spans if s.kind != "lunar_festival"] or [(as_of - timedelta(days=365), as_of)]
            for start, end in windows:
                dates = cal.occurrences(lday, start, min(end, as_of))[:14]
                if dates:
                    extra_lines.append(f"lunar day {lday} falls on solar dates: " + ", ".join(d.isoformat() for d in dates))

    sections: dict[str, str] = {}
    if biz.readme:
        sections["BUSINESS README"] = biz.readme.strip()
    sections["SCHEMA"] = biz.schema_card
    sections["AS-OF DATE"] = f'{as_of.isoformat()} (today; "hôm nay" in the question means this date)'
    time_lines = lines("time") + lines("lunar") + extra_lines
    if time_lines:
        sections["RESOLVED TIME RANGES"] = "\n".join(f"- {l}" for l in time_lines)
    if lines("money"):
        sections["AMOUNTS"] = "\n".join(f"- {l}" for l in lines("money"))
    for name, title in (("shift", "SHIFTS"), ("basis", "TIME BASIS"), ("coverage", "DATA COVERAGE")):
        if lines(name):
            sections[title] = "\n".join(f"- {l}" for l in lines(name))
    hints = lines("entity")
    if hints:
        sections["ENTITY HINTS"] = "\n".join(f"- {h}" for h in hints)
    if lines("units"):
        sections["UNIT CONVERSIONS"] = "\n".join(f"- {l}" for l in lines("units"))
    topics = list(dict.fromkeys(f.value for f in found if f.resolver == "qtype"))
    if topics:
        sections["QUESTION TYPE"] = ", ".join(lines("qtype"))
        block = rules_for(topics, rules)
        if block:
            sections["RULES FOR THIS QUESTION"] = block
    if lines("ambiguous"):
        sections["AMBIGUOUS ENTITIES"] = "\n".join(f"- {l}" for l in lines("ambiguous"))
    if lines("slang"):
        sections["ABBREVIATION HINTS"] = "\n".join(f"- {s}" for s in lines("slang"))
    return Context(as_of, spans, sections, hints)
