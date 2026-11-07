"""Detect rulebook clauses this code was not written against, and hold back the answers they affect.

Metrics implement the rulebooks supplied so far. A later split may add or reword a clause. Rather than
apply outdated logic silently, a changed clause blocks questions it plausibly governs and is reported
at startup so a person can review it. Plan §1.1: a new or conflicting rule needs explicit interpretation
or abstention on the affected questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re

from zalo_engine.grounder import RuleClause, RulebookIndex
from zalo_engine.rule_fingerprints import KNOWN_CLAUSES
from zalo_engine.text_norm import canonicalize_question, fold_key

# Clauses every revenue, order, or ranking answer relies on. Their names rarely appear in questions, so a
# change to one of these blocks the whole family by question wording rather than by the clause name.
_FAMILY_PATTERNS: dict[str, str] = {
    "don tinh vao doanh thu": r"doanh thu|tien|don|ban|khach|mat hang|hang",
    "doanh thu mot don": r"doanh thu|tien|ban",
    "so don": r"\bdon\b",
    "don thuoc ky nao": r"doanh thu|tien|don|ban",
    "ban chay": r"ban chay|ban nhieu|hot nhat",
    "no mot don tai ngay x": r"\bno\b|cong no",
    "don duoc tinh vao no": r"\bno\b|cong no",
    "lai gop": r"lai|loi",
    "tien nhap hang trong ky": r"nhap",
    "giong nhau o moi o": r"doanh thu|tien|don",
}
_STOP_WORDS = {"va", "cua", "mot", "cac", "theo", "trong", "ky", "x", "n"}


def clause_fingerprint(clause: RuleClause) -> str:
    text = "|".join((fold_key(clause.section_title), fold_key(clause.subsection_title or ""), fold_key(clause.rule_name), fold_key(clause.rule_body)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class RuleGuard:
    changed: list[RuleClause] = field(default_factory=list)
    _patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)

    @classmethod
    def from_rulebook(cls, rulebook: RulebookIndex | None) -> "RuleGuard":
        guard = cls()
        if rulebook is None:
            return guard
        for clause in rulebook.clauses:
            if clause_fingerprint(clause) in KNOWN_CLAUSES:
                continue
            guard.changed.append(clause)
            key = fold_key(clause.rule_name)
            family = next((pat for name, pat in _FAMILY_PATTERNS.items() if name in key), None)
            if family is None:
                words = [w for w in key.split() if w not in _STOP_WORDS]
                if not words:
                    continue
                family = r"\b" + r"\b.*\b".join(re.escape(w) for w in words) + r"\b"
            guard._patterns.append((clause.rule_name, re.compile(family)))
        return guard

    def blocking_clause(self, question: str) -> str | None:
        if not self._patterns:
            return None
        text = canonicalize_question(question)
        return next((name for name, pat in self._patterns if pat.search(text)), None)

    def report(self) -> list[str]:
        return [f"Clause not seen in development, affected questions will abstain: {c.rule_name}" for c in self.changed]
