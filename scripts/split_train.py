#!/usr/bin/env python
"""Split the training questions by BUSINESS into a fit part and a dev part.

Test businesses are new to your system, so the dev score only means something if
the dev businesses were never used for few-shot examples, fine-tuning or rule tuning.

  python scripts/split_train.py --train train --out splits [--dev-every 5]

Writes  <out>/{fit,dev}_questions.jsonl  and  <out>/{fit,dev}_answers.jsonl  and prints the dev businesses.
Then:
  python run.py --data train --questions splits/dev_questions.jsonl --out splits/dev_submission.jsonl
  python eval/evaluate.py --pred splits/dev_submission.jsonl --gold splits/dev_answers.jsonl --questions splits/dev_questions.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="train")
    ap.add_argument("--out", default="splits")
    ap.add_argument("--dev-every", type=int, default=5, help="every N-th business (sorted) goes to dev")
    args = ap.parse_args()

    train, out = Path(args.train), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    questions = read(train / "questions.jsonl")
    answers = {a["id"]: a for a in read(train / "answers.jsonl")}
    businesses = sorted({q["business_id"] for q in questions})
    dev_biz = {b for i, b in enumerate(businesses) if i % args.dev_every == args.dev_every - 1}

    parts = {"fit": [], "dev": []}
    for q in questions:
        parts["dev" if q["business_id"] in dev_biz else "fit"].append(q)
    for name, qs in parts.items():
        write(out / f"{name}_questions.jsonl", qs)
        write(out / f"{name}_answers.jsonl", [answers[q["id"]] for q in qs if q["id"] in answers])
        print(f"{name}: {len(qs)} questions, {len({q['business_id'] for q in qs})} businesses")
    print("dev businesses:", ", ".join(sorted(dev_biz)))


if __name__ == "__main__":
    main()
