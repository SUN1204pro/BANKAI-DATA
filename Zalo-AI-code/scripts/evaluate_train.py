"""Score the pipeline on train, reporting dev and held-out businesses separately.

Held-out businesses (tests/fixtures/train_split.json) were fixed by hash before tuning. Looking only at
their aggregate here keeps them useful; do not tune rules on their individual questions.

Two modes:
- Default: score an existing submission file.
- `--readme-variants`: rebuild and answer on temporary copies whose READMEs are weakened, and write a
  report carrying its provenance. Comparing to a baseline fails loudly if the two runs are not comparable.
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_pipeline import run_pipeline
from src.evaluate import evaluate_predictions, load_jsonl_tolerant
from src.ingest import input_fingerprint

REPORT_SCHEMA_VERSION = 1
# The definition is hashed into every report: changing a transformation makes old baselines incomparable.
README_TRANSFORMS = {
    "original": "README bytes unchanged",
    "no-columns": "Remove each '## Cột trong ...' section up to the next '## ' heading; keep everything else",
    "empty": "Replace the README with an empty file",
}
SCOPE_TO_SPLIT_KEY = {"dev": "dev", "heldout": "holdout"}
_COLUMN_SECTION_RE = re.compile(r"^## Cột trong.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
_COMPARED_FIELDS = ("split_hash", "input_hash", "labels_hash", "scorer_hash", "transform_hash", "schema_version", "scope")


class IncompatibleReports(Exception):
    """Two reports measured different things, so comparing them would be meaningless."""


def transform_readme(text: str, variant: str) -> str:
    if variant == "original":
        return text
    if variant == "no-columns":
        return _COLUMN_SECTION_RE.sub("", text)
    if variant == "empty":
        return ""
    raise ValueError(f"Unknown README variant: {variant}")


def compute_metrics(correct: int, wrong: int, abstain: int) -> dict[str, Any]:
    """Counts and rates. Precision is null when nothing was answered, never a passing value."""
    total = correct + wrong + abstain
    if total == 0:
        raise ValueError("Report has zero questions")
    answered = correct + wrong
    return {
        "C": correct,
        "W": wrong,
        "A": abstain,
        "N": total,
        "PA": (correct - wrong) / total,
        "coverage": answered / total,
        "precision": (correct / answered) if answered else None,
    }


def precision_gate_passes(metrics: dict[str, Any], percent: int) -> bool:
    """Compare exact counts against the threshold, not a rounded percentage."""
    answered = metrics["C"] + metrics["W"]
    return answered > 0 and Fraction(metrics["C"], answered) >= Fraction(percent, 100)


def _pa_fraction(metrics: dict[str, Any]) -> Fraction:
    return Fraction(metrics["C"] - metrics["W"], metrics["N"])


def gate_failures(report: dict[str, Any], baseline: dict[str, Any], min_precision: int = 98) -> list[str]:
    """Plan gates: no variant loses PA against its own baseline; weakened READMEs meet the precision target."""
    failures = []
    for variant, metrics in report["results"].items():
        base = baseline["results"].get(variant)
        if base is None:
            failures.append(f"{variant}: no matching baseline")
            continue
        if _pa_fraction(metrics) < _pa_fraction(base):
            failures.append(f"{variant}: PA {metrics['PA']:.4f} below baseline {base['PA']:.4f}")
        if variant != "original" and not precision_gate_passes(metrics, min_precision):
            failures.append(f"{variant}: precision {metrics['precision']} below {min_precision}%")
    return failures


def compare_reports(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Require the same questions, inputs, labels, scorer and variants. Implementation may differ."""
    cur, base = current["provenance"], baseline["provenance"]
    mismatched = [f for f in _COMPARED_FIELDS if cur.get(f) != base.get(f)]
    if mismatched:
        raise IncompatibleReports("Reports are not comparable: " + ", ".join(mismatched))
    return {"implementation_changed": cur.get("implementation_hash") != base.get("implementation_hash")}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths, key=str):
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()


def _code_provenance() -> dict[str, Any]:
    """Implementation identity: every code file except the scorer and this report tool."""
    code = [
        p for folder in ("src", "scripts") for p in (REPO_ROOT / folder).glob("*.py")
        if p.name not in {"evaluate.py", "evaluate_train.py"}
    ]
    info: dict[str, Any] = {"implementation_hash": _hash_files(code), "git_revision": None, "dirty_files": {}}
    try:
        info["git_revision"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "scripts", "tests"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout
        for line in status.splitlines():
            rel = line[3:].strip().strip('"')
            path = REPO_ROOT / rel
            if path.is_file():
                info["dirty_files"][rel] = _sha256(path.read_bytes())
    except (OSError, subprocess.CalledProcessError):
        pass  # Source trees without git still produce a report; the implementation hash identifies the code.
    return info


def _build_provenance(
    scope: str, variants: list[str], members: list[str], train_dir: Path, split_path: Path
) -> dict[str, Any]:
    fingerprints = {b: input_fingerprint(train_dir / "data" / b) for b in members}
    provenance = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "scope": scope,
        "variants": variants,
        "split_hash": _sha256(json.dumps(sorted(members)).encode("utf-8")),
        "split_manifest_hash": _sha256(split_path.read_bytes()),
        "input_hash": _sha256(json.dumps(fingerprints, sort_keys=True).encode("utf-8")),
        "questions_hash": _sha256((train_dir / "questions.jsonl").read_bytes()),
        "labels_hash": _sha256((train_dir / "answers.jsonl").read_bytes()),
        "scorer_hash": _sha256((REPO_ROOT / "src" / "evaluate.py").read_bytes()),
        "transform_hash": _sha256(json.dumps(README_TRANSFORMS, sort_keys=True).encode("utf-8")),
        "transforms": {v: README_TRANSFORMS[v] for v in variants},
    }
    provenance.update(_code_provenance())
    return provenance


def _prepare_variant(variant: str, members: list[str], questions: list[dict[str, Any]], train_dir: Path, root: Path) -> Path:
    """A temporary split: data files are linked read-only by path; only the copied README is transformed."""
    if root.exists():
        shutil.rmtree(root)
    (root / "data").mkdir(parents=True)
    for name in ("calendar.csv", "rulebook.md"):
        shutil.copy(train_dir / name, root / name)
    (root / "questions.jsonl").write_text(
        "\n".join(json.dumps(q, ensure_ascii=False) for q in questions) + "\n", encoding="utf-8"
    )
    for biz in members:
        dest = root / "data" / biz
        dest.mkdir()
        for f in (train_dir / "data" / biz).iterdir():
            if f.name.startswith("."):
                continue
            if f.name == "README.md":
                (dest / f.name).write_text(transform_readme(f.read_text(encoding="utf-8"), variant), encoding="utf-8")
            else:
                os.symlink(f.resolve(), dest / f.name)
    return root


def run_variants(
    variants: list[str],
    scope: str,
    report_path: str | Path,
    *,
    baseline_report: str | Path | None = None,
    train_dir: str | Path = REPO_ROOT / "train",
    split_path: str | Path = REPO_ROOT / "tests" / "fixtures" / "train_split.json",
    cache_root: str | Path = REPO_ROOT / "artifacts" / "variant_cache",
    workers: int | None = None,
) -> dict[str, Any]:
    """Run README variants on one scope and write a report. Held-out output is aggregate only."""
    report_file, cache = Path(report_path), Path(cache_root)
    train, split_file = Path(train_dir), Path(split_path)
    baseline_file = Path(baseline_report) if baseline_report is not None else None
    if baseline_file is not None and baseline_file.resolve() == report_file.resolve():
        raise ValueError("The output report must not be the baseline report")
    if baseline_file is None and report_file.exists():
        raise FileExistsError("Refusing to overwrite an existing baseline report")
    baseline = json.loads(baseline_file.read_text(encoding="utf-8")) if baseline_file is not None else None
    if scope not in SCOPE_TO_SPLIT_KEY:
        raise ValueError(f"Unknown scope: {scope}")

    members = sorted(json.loads(split_file.read_text(encoding="utf-8"))[SCOPE_TO_SPLIT_KEY[scope]])
    member_set = set(members)
    questions = [q for q in load_jsonl_tolerant(train / "questions.jsonl")[0] if q["business_id"] in member_set]
    wanted_ids = {q["id"] for q in questions}
    gold = [g for g in load_jsonl_tolerant(train / "answers.jsonl")[0] if g["id"] in wanted_ids]

    report: dict[str, Any] = {
        "provenance": _build_provenance(scope, variants, members, train, split_file),
        "results": {},
        "timing_sec": {},
        "build_failures": {},
    }
    for variant in variants:
        started = time.monotonic()
        variant_root = cache / scope / variant
        split_dir = _prepare_variant(variant, members, questions, train, variant_root / "split")
        output = variant_root / "submission.jsonl"
        logs = variant_root / "logs"
        if logs.exists():
            shutil.rmtree(logs)
        summary = run_pipeline(split_dir, output, db_dir=variant_root / "dbs", workers=workers, log_dir=logs)
        preds = load_jsonl_tolerant(output)[0]
        overall = evaluate_predictions(gold, preds).overall
        report["results"][variant] = compute_metrics(overall.correct, overall.wrong, overall.abstain)
        report["timing_sec"][variant] = round(time.monotonic() - started, 3)
        # Counts only: an error message can name a business.
        report["build_failures"][variant] = len(summary["build_errors"])
        shutil.rmtree(split_dir)
        if scope == "heldout":
            # Question-level answers and logs are not retained for held-out businesses.
            output.unlink()
            shutil.rmtree(logs)
        m = report["results"][variant]
        print(f"{scope} {variant}: C={m['C']} W={m['W']} A={m['A']} N={m['N']} PA={m['PA']:.4f} "
              f"precision={m['precision']} coverage={m['coverage']:.4f} sec={report['timing_sec'][variant]}", flush=True)

    comparison_error: IncompatibleReports | None = None
    if baseline is not None:
        report["baseline_report_hash"] = _sha256(baseline_file.read_bytes())
        try:
            report["comparison"] = compare_reports(report, baseline)
            report["comparison"]["gate_failures"] = gate_failures(report, baseline)
        except IncompatibleReports as exc:
            comparison_error = exc
            report["comparison"] = {"status": "incompatible", "error": str(exc)}

    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if comparison_error is not None:
        raise comparison_error
    return report


def _score_existing_submission(pred_path: str, output_path: str) -> int:
    split = json.loads((REPO_ROOT / "tests" / "fixtures" / "train_split.json").read_text(encoding="utf-8"))
    questions, _ = load_jsonl_tolerant(REPO_ROOT / "train" / "questions.jsonl")
    gold, _ = load_jsonl_tolerant(REPO_ROOT / "train" / "answers.jsonl")
    preds, _ = load_jsonl_tolerant(pred_path)
    business_of = {q["id"]: q["business_id"] for q in questions}
    by_id = {q["id"]: q for q in questions}

    report = {}
    for name in ("dev", "holdout"):
        ids = set(split[name])
        subset = [g for g in gold if business_of[g["id"]] in ids]
        rep = evaluate_predictions(subset, preds, questions_by_id=by_id)
        report[name] = rep.to_dict()
        o = rep.overall
        print(f"{name:8} N={o.total} correct={o.correct} wrong={o.wrong} abstain={o.abstain} "
              f"PA={o.penalized_accuracy:.4f} precision={o.accuracy_on_answered:.4f} coverage={o.coverage:.4f}")
    rep = evaluate_predictions(gold, preds, questions_by_id=by_id)
    report["all_train"] = rep.to_dict()
    o = rep.overall
    print(f"all      N={o.total} correct={o.correct} wrong={o.wrong} abstain={o.abstain} "
          f"PA={o.penalized_accuracy:.4f} precision={o.accuracy_on_answered:.4f} coverage={o.coverage:.4f}")
    Path(output_path).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pred", default="artifacts/train_baseline_submission.jsonl")
    parser.add_argument("--output", default="artifacts/train_baseline_submission.eval.json")
    parser.add_argument("--readme-variants", nargs="+", choices=sorted(README_TRANSFORMS))
    parser.add_argument("--scope", choices=sorted(SCOPE_TO_SPLIT_KEY))
    parser.add_argument("--report", help="Variant report to write")
    parser.add_argument("--baseline-report", help="Saved report to compare against; never overwritten")
    args = parser.parse_args()

    if not args.readme_variants:
        return _score_existing_submission(args.pred, args.output)
    if not args.scope or not args.report:
        parser.error("--readme-variants requires --scope and --report")
    try:
        report = run_variants(args.readme_variants, args.scope, args.report, baseline_report=args.baseline_report)
    except (IncompatibleReports, FileExistsError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    failures = report.get("comparison", {}).get("gate_failures", [])
    if failures:
        print("GATE FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
