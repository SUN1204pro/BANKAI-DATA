"""Text helpers: accent stripping, tokenizing, and position-preserving normalization."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def unaccent(text) -> str:
    """Remove Vietnamese diacritics ("Xi măng Hà Tiên" -> "Xi mang Ha Tien")."""
    text = unicodedata.normalize("NFD", str(text))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.replace("đ", "d").replace("Đ", "D")


def norm(text) -> str:
    """Lower-case, accent-free, single-spaced. Used for every fuzzy comparison."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", unaccent(text).lower()).strip()


def tokens(text) -> list[str]:
    return re.findall(r"[a-z0-9]+", norm(text))


@dataclass(frozen=True)
class Word:
    text: str    # accent-free, lower-case token ([a-z0-9]+)
    start: int   # character offsets into Normalized.raw, end exclusive
    end: int


class Normalized:
    """`norm()` that remembers where every character came from.

    `text` is identical to `norm(question)`; `raw` is the NFC question the offsets refer to. Resolvers match
    regexes on `text` and turn the match back into an offset in `raw` with `to_raw`, so findings from
    different resolvers can be compared by position even though accents and extra spaces were removed.
    """

    def __init__(self, question):
        self.raw = unicodedata.normalize("NFC", "" if question is None else str(question))
        chars: list[str] = []
        origin: list[int] = []  # origin[i] = index in raw of text[i]
        prev_space = True       # True at the start: drops leading whitespace
        for i, ch in enumerate(self.raw):
            if ch.isspace():
                if not prev_space:
                    chars.append(" ")
                    origin.append(i)
                prev_space = True
                continue
            prev_space = False
            for c in unaccent(ch).lower():
                chars.append(c)
                origin.append(i)
        if chars and chars[-1] == " ":
            chars.pop()
            origin.pop()
        self.text = "".join(chars)
        self._origin = origin

    def to_raw(self, start: int, end: int) -> tuple[int, int]:
        """Map a [start, end) slice of `text` to the matching [start, end) slice of `raw`."""
        if not self._origin or start >= end:
            return 0, 0
        return self._origin[start], self._origin[end - 1] + 1

    def words(self) -> list[Word]:
        return [Word(m.group(0), *self.to_raw(m.start(), m.end())) for m in re.finditer(r"[a-z0-9]+", self.text)]
