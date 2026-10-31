"""Entity grounding as resolvers. Matching stays in ground.Grounder; these add where in the question it matched.

GroundResolver    - "this phrase looks like that cell value", typed (product / customer / staff / category / payment).
AmbiguityResolver - the phrase fits several values (or several kinds of entity): list the options instead of guessing.
"""
from __future__ import annotations

from ..ground import Grounder, Hint
from ..normalize import Normalized, Word, tokens
from .base import Finding, Query, Resolver


_CLAIMING = ("time", "lunar", "money", "shift")


def masked_question(q: Query) -> str:
    """The question with the words already read as dates, amounts or shifts blanked out (same length, same offsets).

    Otherwise "tháng 2/2025" would also be matched against a row called "Tổng tháng 2/2025" in the data.
    """
    chars = list(q.text)
    for f in q.found:
        if f.resolver in _CLAIMING:
            chars[f.start:f.end] = " " * (f.end - f.start)
    return "".join(chars)


def locate(text: str, value: str, ngrams) -> tuple[int, int]:
    """Character range of the run of adjacent question words that matched the cell `value` (exactly or as a typo).

    `text` is the question as given to the grounder. When the words are spread out the longest run wins.
    """
    vtoks = set(tokens(value))
    words = Normalized(text).words()
    runs: list[list[Word]] = []
    prev = -2
    for i, w in enumerate(words):
        if w.text in vtoks or (len(w.text) >= 4 and ngrams.closest(w.text) in vtoks):
            if i == prev + 1 and runs:
                runs[-1].append(w)
            else:
                runs.append([w])
            prev = i
    if not runs:
        return 0, 0
    run = max(runs, key=len)
    return run[0].start, run[-1].end


class GroundResolver(Resolver):
    name = "entity"
    section = "ENTITY HINTS"
    claims = False  # advisory: a product name may legitimately contain digits or words other resolvers read

    def __init__(self, grounder: Grounder | None):
        self.grounder = grounder

    def resolve(self, q: Query) -> list[Finding]:
        if self.grounder is None:
            return []
        text = masked_question(q)
        return [self.finding(q, h.etype, *locate(text, h.value, self.grounder._ngrams), h.render(), h)
                for h in self.grounder.hints(text)]


class AmbiguityResolver(Resolver):
    name = "ambiguous"
    section = "AMBIGUOUS ENTITIES"
    claims = False

    def __init__(self, grounder: Grounder | None):
        self.grounder = grounder

    def resolve(self, q: Query) -> list[Finding]:
        if self.grounder is None:
            return []
        ng = self.grounder._ngrams
        out: list[Finding] = []
        best: list[tuple[Hint, int, int]] = []
        text = masked_question(q)
        for g in self.grounder.groups(text):
            if g.etype == "other":
                continue  # units, statuses, quantities: not something to ask the user to choose between
            start, end = locate(text, g.options[0].value, ng)
            best.append((g.options[0], start, end))
            if g.ambiguous:
                opts = " | ".join(f"'{h.value}'" for h in g.options)
                line = (f'[{g.etype}] "{q.text[start:end]}" fits {len(g.options)} values of {g.table}.{g.column}: {opts} - '
                        f"the question does not say which; use the rest of the question to choose, otherwise cover all of them")
                out.append(self.finding(q, "same_column", start, end, line, g.options))
        # exactly the same words matched cell values of different kinds ("cà phê" = a product and a category)
        seen_pairs: set[tuple[str, str]] = set()  # monthly tables repeat the same column: report each pair of kinds once
        for i, (a, a0, a1) in enumerate(best):
            for b, b0, b1 in best[i + 1:]:
                if a.etype != b.etype and "other" not in (a.etype, b.etype) and (a0, a1) == (b0, b1) and a1 > a0 and min(a.score, b.score) >= 0.6:
                    if (pair := tuple(sorted((a.etype, b.etype)))) in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    s, e = min(a0, b0), max(a1, b1)
                    line = (f'"{q.text[s:e]}" could be a {a.etype} ({a.table}.{a.column} \'{a.value}\') or a {b.etype} '
                            f"({b.table}.{b.column} '{b.value}') - decide from the rest of the question")
                    out.append(self.finding(q, "cross_type", s, e, line, (a, b)))
        return out
