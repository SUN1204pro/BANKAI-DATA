"""Prompt templates and response parsing.

Prompt layout is chosen for vLLM prefix caching: the system message (instructions + the whole
rulebook) is identical for every question, the first user-message sections (README, schema) are
identical for every question of a business, and only the tail changes per question.
"""
from __future__ import annotations

import json
import re

SYSTEM = """You are a data analyst for small Vietnamese businesses. Every question is about ONE business whose files are loaded as SQLite tables. Answer by writing ONE SQLite query.

How to work:
- Use only the tables and columns listed in SCHEMA. Never invent columns. The business README explains what each column means (original headers can be misleading) and how units convert; trust it.
- Follow the RULEBOOK below exactly. It defines revenue, which orders count, rankings, ties, debts, rounding and the answer formats. When the question and the rulebook differ, the question wins.
- Dates are ISO text (YYYY-MM-DD). Use RESOLVED TIME RANGES exactly as given and never recompute relative dates yourself. A column named <x>_time holds the HH:MM:SS of column <x>.
- Match names accent-insensitively with norm(column) LIKE '%...%' (norm() lower-cases and strips Vietnamese accents). Prefer the exact values listed under ENTITY HINTS.
- Questions may use slang or miss accents; ABBREVIATION HINTS may help. AMOUNTS gives numbers written as 5k / 2,5tr.
- TIME BASIS says whether the question counts activity inside the period (FLOW) or the balance at the end of a day (STOCK: everything from the first day of the data). DATA COVERAGE warns when a period lies outside the days the shop has data (then answerable=false, not 0).
- UNIT CONVERSIONS gives "1 thùng = 24 lon" for the products in the question; SHIFTS gives ca sáng / ca chiều hours; AMBIGUOUS ENTITIES lists values a phrase could mean.
- After the QUESTION, RULES FOR THIS QUESTION repeats the rulebook rows for this kind of question (which orders count differs between revenue, debt and cancel rate). Apply them.
- Rounding: money is a whole number of VND, so ROUND(x, 0). Percentages and other averages: ROUND(x, 2). A percent is in percentage points (12.4, never 0.124).
- Return the minimal result. Scalar types (number, percent, string, date, boolean): exactly one row and one column. list: one column, or one row of several numbers when the question asks for several numbers. table: first column = the entity name, then the measures in the order the question names them. Apply the rulebook's ordering and tie-break rules (ORDER BY ... , code ASC).
- If the data cannot support the question (rulebook section 7: the information is not recorded, or the period lies completely outside the data), set "answerable": false and give a short reason. Do not guess.
- answer_type is one of: number, percent, string, date, boolean, list, table.

Reply with a single JSON object and nothing else:
{"answerable": true|false, "reason": "<short>", "answer_type": "<type>", "sql": "<one SELECT query>"}"""


def build_system(rulebook_text: str) -> str:
    rb = (rulebook_text or "").strip()
    return SYSTEM + (f"\n\n# RULEBOOK\n{rb}" if rb else "")


# README and SCHEMA come first: they are identical for every question of a business (prefix cache).
_BEFORE_QUESTION = (
    "BUSINESS README", "SCHEMA", "AS-OF DATE", "RESOLVED TIME RANGES", "TIME BASIS", "DATA COVERAGE", "SHIFTS",
    "AMOUNTS", "UNIT CONVERSIONS", "ENTITY HINTS", "AMBIGUOUS ENTITIES", "ABBREVIATION HINTS",
)
# After the question, so they are the last thing the model reads.
_AFTER_QUESTION = ("QUESTION TYPE", "RULES FOR THIS QUESTION")


def render_user(question: str, as_of: str, sections: dict[str, str]) -> str:
    def block(title: str) -> list[str]:
        body = sections.get(title)
        return [f"## {title}\n{body}"] if body else []

    parts: list[str] = []
    for title in _BEFORE_QUESTION:
        parts += block(title)
    parts.append(f"## QUESTION\n{question}")
    for title in _AFTER_QUESTION:
        parts += block(title)
    return "\n\n".join(parts)


def make_messages(system: str, user_text: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user_text}]


def repair_messages(base: list[dict], assistant_text: str, error: str) -> list[dict]:
    return base + [
        {"role": "assistant", "content": assistant_text},
        {
            "role": "user",
            "content": f"That query failed: {error}\nFix it and reply again with the same JSON format only.",
        },
    ]


_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"```(?:json|sql)?\s*(.*?)```", re.S)


def extract_response(text: str) -> dict | None:
    """Parse the model's JSON reply; fall back to a bare SQL block."""
    text = _THINK.sub("", text or "").strip()
    if not text:
        return None
    candidates = [text] + _FENCE.findall(text)
    for cand in candidates:
        start, end = cand.find("{"), cand.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(cand[start : end + 1])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    m = re.search(r"(?is)\b(select|with)\b.*", text)
    if m:
        return {"answerable": True, "answer_type": None, "sql": m.group(0).strip().strip("`")}
    return None
