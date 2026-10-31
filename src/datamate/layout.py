"""Find the questions file, business folders, rulebook and calendar inside /data.

The real directory layout is defined by the competition's Data section, which
this skeleton has not seen. These heuristics are deliberately forgiving; every
location can also be forced from the command line (see run.py).
"""
from __future__ import annotations

from pathlib import Path


def find_questions(root: Path) -> Path:
    exact = sorted(root.rglob("questions.jsonl"))
    if exact:
        return exact[0]
    loose = sorted(root.rglob("questions*.jsonl"))
    if loose:
        return loose[0]
    raise FileNotFoundError(f"no questions.jsonl under {root}")


class BusinessIndex:
    """Maps business_id -> folder (first directory with that exact name, depth <= 4)."""

    def __init__(self, root: Path, explicit_root: Path | None = None, max_depth: int = 4):
        self.explicit = explicit_root
        self.dirs: dict[str, Path] = {}
        if explicit_root is None:
            base = len(root.parts)
            for p in sorted(root.rglob("*")):
                if p.is_dir() and len(p.parts) - base <= max_depth:
                    self.dirs.setdefault(p.name, p)

    def find(self, business_id: str) -> Path | None:
        if self.explicit is not None:
            p = self.explicit / business_id
            return p if p.is_dir() else None
        return self.dirs.get(business_id)


def _find_shared(root: Path, words: tuple[str, ...], suffixes: tuple[str, ...]) -> Path | None:
    cands = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in suffixes
             and any(w in p.name.lower() for w in words)]
    cands.sort(key=lambda p: (0 if p.stem.lower() in words else 1, len(p.parts)))
    return cands[0] if cands else None


def find_rulebook(root: Path) -> Path | None:
    return _find_shared(root, ("rulebook", "rules"), (".md", ".txt", ".json"))


def find_calendar(root: Path) -> Path | None:
    return _find_shared(root, ("calendar", "lich"), (".csv", ".json", ".xlsx", ".tsv"))
