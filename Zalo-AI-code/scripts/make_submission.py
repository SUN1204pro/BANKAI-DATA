"""Create a new timestamped folder holding a validated submission and a record of what produced it.

Usage: ./run.sh [public_test|train|PATH]   (default: public_test)

Each run writes artifacts/submissions/<YYYYMMDD-HHMMSS>_<split>/ with submission.jsonl and manifest.json, and
points artifacts/submissions/latest-<split> at it, so a train run can never be mistaken for the file to upload.
Earlier folders are never changed. Databases are reused only when their input files and code are unchanged, so
a run after a code change rebuilds what it needs.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_pipeline import load_questions_jsonl, run_pipeline
from src.grounder import RulebookIndex
from src.postprocess import validate_submission_record
from src.rule_guard import RuleGuard

DEFAULT_OUTPUT_ROOT = REPO_ROOT / "artifacts" / "submissions"


def validate_submission_file(path: str | Path, expected_ids: list[str]) -> list[str]:
    """Problems that would make the file unsafe to upload. An empty list means it is valid."""
    problems: list[str] = []
    seen: list[str] = []
    with Path(path).open(encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"Line {number} is not valid JSON")
                continue
            ok, reason = validate_submission_record(record)
            if not ok:
                problems.append(f"Line {number}: {reason}")
            if isinstance(record, dict):
                seen.append(str(record.get("id")))
    if seen != expected_ids:
        missing = len(set(expected_ids) - set(seen))
        extra = len(set(seen) - set(expected_ids))
        repeated = len(seen) - len(set(seen))
        problems.append(f"IDs differ from the input: {missing} missing, {extra} extra, {repeated} repeated, or out of order")
    return problems


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _new_run_folder(root: Path, now: datetime, split_name: str) -> Path:
    """A folder that did not exist before this run. Runs in the same second get a numeric suffix."""
    root.mkdir(parents=True, exist_ok=True)
    stamp = f"{now.strftime('%Y%m%d-%H%M%S')}_{split_name}"
    attempt = 1
    while True:
        folder = root / (stamp if attempt == 1 else f"{stamp}-{attempt}")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            attempt += 1


def _point_latest(root: Path, folder: Path, split_name: str) -> None:
    temp = root / f".latest-{split_name}.tmp"
    if temp.is_symlink() or temp.exists():
        temp.unlink()
    temp.symlink_to(folder.name)
    os.replace(temp, root / f"latest-{split_name}")


def create_run(
    split_dir: str | Path,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    *,
    now: datetime | None = None,
    db_dir: str | Path | None = None,
    workers: int | None = None,
) -> Path:
    """Run the pipeline into a new folder, validate the result, and write its manifest."""
    split = Path(split_dir).resolve()
    root = Path(output_root).resolve()
    started = now or datetime.now()
    folder = _new_run_folder(root, started, split.name)
    output = folder / "submission.jsonl"

    summary = run_pipeline(split, output, db_dir=db_dir, workers=workers, evaluate_if_labels_present=True)

    expected_ids = [str(q["id"]) for q in load_questions_jsonl(split / "questions.jsonl")]
    problems = validate_submission_file(output, expected_ids)
    types = Counter(json.loads(line).get("type") for line in output.read_text(encoding="utf-8").splitlines() if line.strip())
    rulebook = RulebookIndex.from_markdown(split / "rulebook.md")
    rulebook_changes = RuleGuard.from_rulebook(rulebook).report()
    if problems:
        status = "invalid"
    elif summary["build_errors"] or rulebook_changes:
        status = "valid_with_warnings"
    else:
        status = "valid"

    changed = _git("status", "--porcelain", "--", "src", "scripts", "tests", "run.sh", "run_submission.py")
    manifest: dict[str, Any] = {
        "status": status,
        "problems": problems,
        "split": split.name,
        "created_at": started.isoformat(timespec="seconds"),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_changed_files": changed.splitlines() if changed else [],
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "total_questions": len(expected_ids),
        "counts_by_type": dict(sorted(types.items())),
        "elapsed_sec": summary["elapsed_sec"],
        "build_errors": summary["build_errors"],
        "rulebook_changes": rulebook_changes,
        "evaluation": summary.get("evaluation"),
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if status != "invalid":
        _point_latest(root, folder, split.name)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("split", nargs="?", default="public_test", help="public_test, train, or a split folder path")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--db-dir", default=None, help="Database cache (default: artifacts/dbs/<split>)")
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args()

    split = Path(args.split)
    if not split.is_absolute():
        split = REPO_ROOT / split
    if not (split / "questions.jsonl").is_file():
        print(f"ERROR: no questions.jsonl in {split}", file=sys.stderr)
        return 2
    db_dir = args.db_dir or REPO_ROOT / "artifacts" / "dbs" / split.name

    folder = create_run(split, args.output_root, db_dir=db_dir, workers=args.workers)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    print(f"Status:  {manifest['status']}")
    print(f"File:    {folder / 'submission.jsonl'}")
    print(f"Commit:  {(manifest['git_commit'] or 'unknown')[:7]}" + ("  (with uncommitted changes)" if manifest["git_changed_files"] else ""))
    print(f"Answers: {manifest['counts_by_type']}")
    print(f"Time:    {manifest['elapsed_sec']} s")
    print(f"SHA-256: {manifest['sha256']}")
    if manifest["evaluation"]:
        e = manifest["evaluation"]
        print(f"Score:   PA={e['penalized_accuracy']} correct={e['correct']} wrong={e['wrong']} abstain={e['abstain']}")
    for warning in manifest["rulebook_changes"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    for business, error in manifest["build_errors"].items():
        print(f"WARNING: {business} failed to build and its questions abstain: {error}", file=sys.stderr)
    for problem in manifest["problems"]:
        print(f"INVALID: {problem}", file=sys.stderr)
    return 1 if manifest["status"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())
