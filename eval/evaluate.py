#!/usr/bin/env python
"""Score a submission against gold answers with the rulebook's rules (section 9).

Score per question: +1 correct, -1 wrong or wrong format, 0 abstain / missing line.
`match()` implements the comparison rules written in the rulebook; if the official
Evaluation section says anything different, change it here only.

Usage:
  python eval/evaluate.py --pred submission.jsonl --gold train/answers.jsonl \
      [--questions train/questions.jsonl] [--errors errors.json] [--only-businesses biz_101,biz_102]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

NON_ANSWERS = {"unanswerable", "abstain"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def norm_str(s) -> str:
    """Case, whitespace and punctuation are ignored; Vietnamese diacritics are NOT."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFC", str(s)).lower())


def is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and not (isinstance(x, float) and math.isnan(x))


def num_eq(a, b) -> bool:
    return is_num(a) and is_num(b) and math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)


def cell_eq(a, b) -> bool:
    if is_num(a) or is_num(b):
        return num_eq(a, b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return norm_str(a) == norm_str(b)


def valid_format(pred: dict) -> bool:
    t, a = pred.get("type"), pred.get("answer")
    if t in ("number", "percent"):
        return is_num(a)
    if t == "boolean":
        return isinstance(a, bool)
    if t == "string":
        return isinstance(a, str)
    if t == "date":
        return isinstance(a, str) and bool(_DATE_RE.match(a))
    if t == "list":
        return isinstance(a, list)
    if t == "table":
        return isinstance(a, dict) and isinstance(a.get("columns"), list) and isinstance(a.get("rows"), list)
    if t == "unanswerable":
        return "reason" not in pred or isinstance(pred["reason"], str)
    return False


def _row_key(row) -> str:
    return "|".join(f"n:{float(c):.9g}" if is_num(c) else f"s:{norm_str(c)}" for c in row)


def _table_eq(pa: dict, ga: dict) -> bool:
    pc, gc = [norm_str(c) for c in pa["columns"]], [norm_str(c) for c in ga["columns"]]
    prows = pa["rows"]
    if len(pc) != len(gc):
        return False
    if sorted(pc) == sorted(gc) and len(set(pc)) == len(pc):  # same names -> compare by name
        order = [pc.index(c) for c in gc]
        prows = [[r[i] for i in order] for r in prows]
    # names differ -> compare by position
    if len(prows) != len(ga["rows"]):
        return False
    return sorted(_row_key(r) for r in prows) == sorted(_row_key(r) for r in ga["rows"])


def match(pred: dict, gold: dict) -> bool:
    """True if `pred` earns +1 against `gold` (abstain is handled by `score`)."""
    pt, gt = pred.get("type"), gold.get("type")
    if gt == "unanswerable":
        return pt == "unanswerable" and valid_format(pred)
    if pt in NON_ANSWERS or pt != gt or not valid_format(pred):
        return False
    pa, ga = pred.get("answer"), gold.get("answer")
    if gt in ("number", "percent"):
        return num_eq(pa, ga)
    if gt == "boolean":
        return pa is ga
    if gt == "date":
        return pa == ga
    if gt == "string":
        accepted = [ga] + list(gold.get("alias") or [])
        return norm_str(pa) in {norm_str(x) for x in accepted}
    if gt == "list":
        return isinstance(ga, list) and len(pa) == len(ga) and all(cell_eq(x, y) for x, y in zip(pa, ga))
    if gt == "table":
        return _table_eq(pa, ga)
    return pa == ga


def score(pred: dict | None, gold: dict) -> int:
    if pred is None or pred.get("type") == "abstain":
        return 0
    return 1 if match(pred, gold) else -1


def load(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["id"]] = r
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--questions", help="adds question text and business_id to the error dump and enables --only-businesses")
    ap.add_argument("--only-businesses", help="comma-separated business ids to score (needs --questions)")
    ap.add_argument("--errors", help="write wrong / abstained answers here (json)")
    args = ap.parse_args()

    pred, gold = load(args.pred), load(args.gold)
    qs = load(args.questions) if args.questions else {}
    if args.only_businesses:
        keep = set(args.only_businesses.split(","))
        gold = {k: v for k, v in gold.items() if qs.get(k, {}).get("business_id") in keep}

    total = Counter()
    by_type: dict[str, Counter] = defaultdict(Counter)
    errors = []
    for qid, g in gold.items():
        p = pred.get(qid)
        s = score(p, g)
        label = {1: "right", -1: "wrong", 0: "abstain"}[s]
        total[label] += 1
        by_type[g["type"]][label] += 1
        if s != 1:
            errors.append({
                "id": qid, "result": label,
                "question": qs.get(qid, {}).get("question"),
                "business_id": qs.get(qid, {}).get("business_id"),
                "as_of_date": qs.get(qid, {}).get("as_of_date"),
                "gold": {k: g.get(k) for k in ("type", "answer", "alias")},
                "pred": p,
            })

    n = sum(total.values())
    pts = total["right"] - total["wrong"]
    answered = total["right"] + total["wrong"]
    print(f"score: {pts:+d} of {n} max  (mean {pts / max(n, 1):+.4f} per question)")
    print(f"right {total['right']}  wrong {total['wrong']}  abstain {total['abstain']}  "
          f"| precision when answering {total['right'] / max(answered, 1):.3f}")
    print(f"{'gold type':14s} {'n':>5s} {'right':>6s} {'wrong':>6s} {'abstain':>8s} {'score':>7s}")
    for t, c in sorted(by_type.items()):
        k = sum(c.values())
        print(f"{t:14s} {k:5d} {c['right']:6d} {c['wrong']:6d} {c['abstain']:8d} {c['right'] - c['wrong']:+7d}")
    if args.errors:
        Path(args.errors).write_text(json.dumps(errors, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {len(errors)} non-correct answers to {args.errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
