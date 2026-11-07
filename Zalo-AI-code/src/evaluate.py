"""Rulebook-equivalent local scorer and slice evaluator for DataMate (Rulebook §9)."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable
import unicodedata

from src.postprocess import (
    VALID_SUBMISSION_TYPES,
    is_valid_date_string,
    is_valid_json_number,
)

FLOAT_TOLERANCE = 1e-9


def official_string_key(val: str) -> str:
    """Official string rule: NFC, lowercase, non letter/digit/space to a space, collapse. Diacritics stay."""
    lowered = unicodedata.normalize("NFC", val).lower()
    kept = "".join(ch if (ch.isalpha() or ch.isdecimal() or ch.isspace()) else " " for ch in lowered)
    return " ".join(kept.split())


def strings_match(pred: Any, gold: Any, aliases: Iterable[str] | None = None) -> bool:
    """Compare a predicted string against a gold string and its accepted forms (official Evaluation table)."""
    if not isinstance(pred, str) or not isinstance(gold, str):
        return False
    key = official_string_key(pred)
    candidates = [gold, *(a for a in (aliases or ()) if isinstance(a, str))]
    return any(key == official_string_key(cand) for cand in candidates)


def numbers_match(pred: Any, gold: Any) -> bool:
    """Compare two JSON numbers after Rulebook §9 rounding, ignoring only IEEE-754 float noise."""
    if not is_valid_json_number(pred) or not is_valid_json_number(gold):
        return False
    return abs(float(pred) - float(gold)) <= FLOAT_TOLERANCE


def cells_match(
    pred_val: Any,
    gold_val: Any,
    aliases: Iterable[str] | None = None,
) -> bool:
    """Compare an individual list element or table cell against gold."""
    if isinstance(gold_val, bool):
        return isinstance(pred_val, bool) and (pred_val is gold_val)
    if is_valid_json_number(gold_val):
        return numbers_match(pred_val, gold_val)
    if isinstance(gold_val, str):
        if is_valid_date_string(gold_val) and not aliases:
            if isinstance(pred_val, str) and pred_val.strip() == gold_val.strip():
                return True
        return strings_match(pred_val, gold_val, aliases=aliases)
    return False


def _resolve_table_column_map(pred_cols: list[str], gold_cols: list[str]) -> list[int]:
    """Map each gold column index -> predicted column index.

    Rulebook §9: match by normalized column name first; if names differ,
    fall back to positional order.
    """
    k = len(gold_cols)
    pred_norm = [official_string_key(c) for c in pred_cols]
    gold_norm = [official_string_key(c) for c in gold_cols]

    if len(set(pred_norm)) == k and len(set(gold_norm)) == k and set(pred_norm) == set(gold_norm):
        return [pred_norm.index(g) for g in gold_norm]

    # Also check if measure columns 1..k-1 uniquely match by name while keeping entity at index 0.
    if k > 2:
        pred_rest = pred_norm[1:]
        gold_rest = gold_norm[1:]
        if (
            len(set(pred_rest)) == k - 1
            and len(set(gold_rest)) == k - 1
            and set(pred_rest) == set(gold_rest)
        ):
            return [0] + [1 + pred_rest.index(g) for g in gold_rest]

    return list(range(k))


def tables_match(
    pred_table: Any,
    gold_table: dict[str, Any],
    gold_aliases: list[Any] | None = None,
) -> bool:
    """Compare a predicted table against a gold table (order-invariant rows, entity aliases)."""
    if not isinstance(pred_table, dict) or not isinstance(gold_table, dict):
        return False

    pred_cols = pred_table.get("columns")
    pred_rows = pred_table.get("rows")
    gold_cols = gold_table.get("columns")
    gold_rows = gold_table.get("rows")

    if (
        not isinstance(pred_cols, list)
        or not isinstance(pred_rows, list)
        or not isinstance(gold_cols, list)
        or not isinstance(gold_rows, list)
    ):
        return False

    if len(pred_cols) != len(gold_cols) or len(pred_rows) != len(gold_rows):
        return False

    num_cols = len(gold_cols)
    num_rows = len(gold_rows)
    if num_cols == 0:
        return False

    for r in pred_rows:
        if not isinstance(r, list) or len(r) != num_cols:
            return False

    col_map = _resolve_table_column_map(pred_cols, gold_cols)

    # Build bipartite adjacency: gold row g -> matching predicted rows p
    adj: list[list[int]] = [[] for _ in range(num_rows)]
    for g_idx, g_row in enumerate(gold_rows):
        row_aliases: list[str] = []
        if (
            isinstance(gold_aliases, list)
            and g_idx < len(gold_aliases)
            and isinstance(gold_aliases[g_idx], list)
        ):
            row_aliases = [a for a in gold_aliases[g_idx] if isinstance(a, str)]

        for p_idx, p_row in enumerate(pred_rows):
            # Column 0 is the entity column (supports row_aliases)
            if not cells_match(p_row[col_map[0]], g_row[0], aliases=row_aliases):
                continue
            all_measures_ok = True
            for c_idx in range(1, num_cols):
                if not cells_match(p_row[col_map[c_idx]], g_row[c_idx]):
                    all_measures_ok = False
                    break
            if all_measures_ok:
                adj[g_idx].append(p_idx)

    # Find maximum bipartite matching via Kuhn's DFS algorithm
    match_pred_to_gold = [-1] * num_rows

    def _dfs(g_idx: int, visited: list[bool]) -> bool:
        for p_idx in adj[g_idx]:
            if visited[p_idx]:
                continue
            visited[p_idx] = True
            if match_pred_to_gold[p_idx] == -1 or _dfs(match_pred_to_gold[p_idx], visited):
                match_pred_to_gold[p_idx] = g_idx
                return True
        return False

    matched_count = 0
    for g_idx in range(num_rows):
        visited = [False] * num_rows
        if _dfs(g_idx, visited):
            matched_count += 1

    return matched_count == num_rows


def _classify_prediction_line(rec: Any) -> str:
    """Classify a raw JSON submission object into 'ignore', 'abstain', or 'Attempt'.

    Per Rulebook §9:
    - Unreadable objects, missing required fields ('id', 'type', or 'answer' for answered types),
      invalid 'type', or 'unanswerable' with a non-string 'reason' are ignored (treated as abstain
      if no valid line for that id exists).
    - Valid 'type' with malformed 'answer' for that type is a readable attempt (and will score -1).
    """
    if not isinstance(rec, dict):
        return "ignore"
    qid = rec.get("id")
    if not isinstance(qid, str) or not qid.strip():
        return "ignore"
    ans_type = rec.get("type")
    if not isinstance(ans_type, str) or ans_type not in VALID_SUBMISSION_TYPES:
        return "ignore"

    if ans_type == "abstain":
        return "abstain"

    if ans_type == "unanswerable":
        if "reason" in rec and rec["reason"] is not None and not isinstance(rec["reason"], str):
            return "ignore"
        return "attempt"

    if "answer" not in rec:
        return "ignore"

    return "attempt"


def score_single_prediction(gold: dict[str, Any], pred: dict[str, Any] | None) -> tuple[str, int, str]:
    """Score a single question prediction against its gold label.

    Returns:
        (outcome, points, detail)
        where outcome is one of {'correct', 'wrong', 'abstain'} and points in {1, -1, 0}.
    """
    if pred is None:
        return "abstain", 0, "missing_or_ignored"

    line_kind = _classify_prediction_line(pred)
    if line_kind in {"ignore", "abstain"}:
        return "abstain", 0, line_kind

    gold_type = gold["type"]
    pred_type = pred["type"]
    gold_ans = gold.get("answer")
    gold_aliases = gold.get("alias") or []

    if gold_type == "unanswerable":
        if pred_type == "unanswerable":
            return "correct", 1, "unanswerable_match"
        return "wrong", -1, f"expected_unanswerable_got_{pred_type}"

    if pred_type != gold_type:
        return "wrong", -1, f"type_mismatch:expected_{gold_type}_got_{pred_type}"

    pred_ans = pred.get("answer")

    if gold_type in {"number", "percent"}:
        if not is_valid_json_number(pred_ans):
            return "wrong", -1, f"invalid_{gold_type}_format"
        if numbers_match(pred_ans, gold_ans):
            return "correct", 1, "exact_or_rounded_match"
        return "wrong", -1, f"value_mismatch:pred={pred_ans!r}_gold={gold_ans!r}"

    if gold_type == "string":
        if not isinstance(pred_ans, str):
            return "wrong", -1, "invalid_string_format"
        str_aliases = [a for a in gold_aliases if isinstance(a, str)]
        if strings_match(pred_ans, gold_ans, aliases=str_aliases):
            return "correct", 1, "string_match"
        return "wrong", -1, f"string_mismatch:pred={pred_ans!r}_gold={gold_ans!r}"

    if gold_type == "date":
        if not is_valid_date_string(pred_ans):
            return "wrong", -1, "invalid_date_format"
        if pred_ans.strip() == str(gold_ans).strip():
            return "correct", 1, "date_match"
        return "wrong", -1, f"date_mismatch:pred={pred_ans!r}_gold={gold_ans!r}"

    if gold_type == "boolean":
        if not isinstance(pred_ans, bool):
            return "wrong", -1, "invalid_boolean_format"
        if pred_ans is gold_ans:
            return "correct", 1, "boolean_match"
        return "wrong", -1, f"boolean_mismatch:pred={pred_ans!r}_gold={gold_ans!r}"

    if gold_type == "list":
        if not isinstance(pred_ans, list) or not isinstance(gold_ans, list):
            return "wrong", -1, "invalid_list_format"
        if len(pred_ans) != len(gold_ans):
            return "wrong", -1, f"list_length_mismatch:pred={len(pred_ans)}_gold={len(gold_ans)}"
        for idx, (p_el, g_el) in enumerate(zip(pred_ans, gold_ans)):
            el_aliases: list[str] = []
            if (
                isinstance(gold_aliases, list)
                and idx < len(gold_aliases)
                and isinstance(gold_aliases[idx], list)
            ):
                el_aliases = [a for a in gold_aliases[idx] if isinstance(a, str)]
            if not cells_match(p_el, g_el, aliases=el_aliases):
                return "wrong", -1, f"list_element_mismatch_at_{idx}:pred={p_el!r}_gold={g_el!r}"
        return "correct", 1, "list_match"

    if gold_type == "table":
        if tables_match(pred_ans, gold_ans, gold_aliases=gold_aliases):
            return "correct", 1, "table_match"
        return "wrong", -1, "table_mismatch"

    return "wrong", -1, f"unsupported_gold_type:{gold_type}"


@dataclass
class SliceMetrics:
    """Aggregated evaluation metrics for a dataset or slice."""

    total: int = 0
    correct: int = 0
    wrong: int = 0
    abstain: int = 0

    @property
    def answered(self) -> int:
        return self.correct + self.wrong

    @property
    def penalized_accuracy(self) -> float:
        return (self.correct - self.wrong) / self.total if self.total else 0.0

    @property
    def coverage(self) -> float:
        return self.answered / self.total if self.total else 0.0

    @property
    def accuracy_on_answered(self) -> float:
        return self.correct / self.answered if self.answered else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "correct": self.correct,
            "wrong": self.wrong,
            "abstain": self.abstain,
            "answered": self.answered,
            "penalized_accuracy": round(self.penalized_accuracy, 6),
            "coverage": round(self.coverage, 6),
            "accuracy_on_answered": round(self.accuracy_on_answered, 6),
        }


@dataclass
class EvaluationReport:
    """Complete evaluation report across all questions and slices."""

    overall: SliceMetrics = field(default_factory=SliceMetrics)
    by_type: dict[str, SliceMetrics] = field(default_factory=lambda: defaultdict(SliceMetrics))
    by_business: dict[str, SliceMetrics] = field(default_factory=lambda: defaultdict(SliceMetrics))
    unanswerable_tp: int = 0
    unanswerable_fp: int = 0
    unanswerable_fn: int = 0
    ignored_lines: int = 0
    duplicate_ids: int = 0
    item_results: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def unanswerable_precision(self) -> float:
        denom = self.unanswerable_tp + self.unanswerable_fp
        return self.unanswerable_tp / denom if denom else 0.0

    @property
    def unanswerable_recall(self) -> float:
        denom = self.unanswerable_tp + self.unanswerable_fn
        return self.unanswerable_tp / denom if denom else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.to_dict(),
            "unanswerable": {
                "tp": self.unanswerable_tp,
                "fp": self.unanswerable_fp,
                "fn": self.unanswerable_fn,
                "precision": round(self.unanswerable_precision, 6),
                "recall": round(self.unanswerable_recall, 6),
            },
            "diagnostics": {
                "ignored_lines": self.ignored_lines,
                "duplicate_ids": self.duplicate_ids,
            },
            "by_type": {k: v.to_dict() for k, v in sorted(self.by_type.items())},
            "by_business": {k: v.to_dict() for k, v in sorted(self.by_business.items())},
        }


def evaluate_predictions(
    gold_records: Iterable[dict[str, Any]],
    pred_records: Iterable[Any],
    questions_by_id: dict[str, dict[str, Any]] | None = None,
) -> EvaluationReport:
    """Evaluate predicted records against gold records per Rulebook §9."""
    report = EvaluationReport()

    # Deduplicate predictions: keep the first valid/readable line per question id.
    first_valid_pred: dict[str, dict[str, Any]] = {}
    for raw_rec in pred_records:
        kind = _classify_prediction_line(raw_rec)
        if kind == "ignore":
            report.ignored_lines += 1
            continue
        qid = raw_rec["id"].strip()
        if qid in first_valid_pred:
            report.duplicate_ids += 1
            continue
        first_valid_pred[qid] = raw_rec

    for gold in gold_records:
        qid = gold["id"]
        gold_type = gold["type"]
        pred = first_valid_pred.get(qid)
        outcome, points, detail = score_single_prediction(gold, pred)

        biz_id = None
        if questions_by_id and qid in questions_by_id:
            biz_id = questions_by_id[qid].get("business_id")

        slices = [report.overall, report.by_type[gold_type]]
        if biz_id:
            slices.append(report.by_business[biz_id])

        for s in slices:
            s.total += 1
            if outcome == "correct":
                s.correct += 1
            elif outcome == "wrong":
                s.wrong += 1
            else:
                s.abstain += 1

        pred_type = pred.get("type") if (pred and outcome != "abstain") else None
        if gold_type == "unanswerable":
            if pred_type == "unanswerable":
                report.unanswerable_tp += 1
            else:
                report.unanswerable_fn += 1
        elif pred_type == "unanswerable":
            report.unanswerable_fp += 1

        report.item_results[qid] = {
            "id": qid,
            "gold_type": gold_type,
            "pred_type": pred.get("type") if pred else None,
            "outcome": outcome,
            "points": points,
            "detail": detail,
        }

    return report


def load_jsonl_tolerant(path: str | Path) -> tuple[list[Any], int]:
    """Load JSONL file tolerantly, counting unparseable lines."""
    records: list[Any] = []
    unparseable = 0
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError:
                unparseable += 1
    return records, unparseable


def evaluate_files(
    gold_path: str | Path,
    pred_path: str | Path,
    questions_path: str | Path | None = None,
) -> EvaluationReport:
    """Evaluate a submission JSONL file against a gold answers JSONL file."""
    gold_records, gold_bad = load_jsonl_tolerant(gold_path)
    if gold_bad > 0:
        raise ValueError(f"Gold file {gold_path} contains {gold_bad} invalid JSON lines")

    pred_records, pred_bad = load_jsonl_tolerant(pred_path)
    questions_by_id: dict[str, dict[str, Any]] | None = None
    if questions_path is not None and Path(questions_path).exists():
        q_records, _ = load_jsonl_tolerant(questions_path)
        questions_by_id = {q["id"]: q for q in q_records if isinstance(q, dict) and "id" in q}

    report = evaluate_predictions(gold_records, pred_records, questions_by_id=questions_by_id)
    report.ignored_lines += pred_bad
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate DataMate predictions (Rulebook §9).")
    parser.add_argument("--gold", required=True, help="Path to gold answers.jsonl")
    parser.add_argument("--pred", required=True, help="Path to predicted submission.jsonl")
    parser.add_argument("--questions", default=None, help="Optional path to questions.jsonl")
    parser.add_argument("--output", default=None, help="Optional path to write JSON report")
    args = parser.parse_args()

    report = evaluate_files(args.gold, args.pred, questions_path=args.questions)
    summary = report.to_dict()
    print(json.dumps(summary["overall"], indent=2, ensure_ascii=False))
    if args.output:
        Path(args.output).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
