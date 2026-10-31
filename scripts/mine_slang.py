"""List short, rare, non-Vietnamese-looking tokens in the training questions (candidates for slang.json).

A token is a candidate when it is ASCII-only, short, and its accent-free form never appears as the
accent-stripped version of a real accented word in the corpus (so "thang"/"bao" are filtered out,
"dthu"/"nhiu" are not). The output is for manual review; nothing is written automatically.

    python scripts/mine_slang.py train/questions.jsonl [--max-len 6] [--max-freq 400]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from datamate.normalize import unaccent  # noqa: E402

WORD = re.compile(r"[^\W\d_]+", re.UNICODE)  # letters only: digits are handled by the money resolver


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("questions")
    ap.add_argument("--max-len", type=int, default=6)
    ap.add_argument("--max-freq", type=int, default=400)
    a = ap.parse_args()

    qs = [json.loads(l)["question"] for l in Path(a.questions).read_text(encoding="utf-8").splitlines() if l.strip()]
    freq: Counter[str] = Counter()
    accented_forms: set[str] = set()
    examples: dict[str, list[str]] = defaultdict(list)
    for q in qs:
        q = unicodedata.normalize("NFC", q)
        for w in WORD.findall(q.lower()):
            freq[w] += 1
            if unaccent(w) != w:
                accented_forms.add(unaccent(w))
            elif len(examples[w]) < 2:
                examples[w].append(q)
    rows = [(w, n) for w, n in freq.items()
            if w.isascii() and len(w) <= a.max_len and w not in accented_forms and n <= a.max_freq]
    for w, n in sorted(rows, key=lambda x: -x[1]):
        print(f"{n:5d}  {w:8s} | {examples[w][0][:110]}")


if __name__ == "__main__":
    main()
