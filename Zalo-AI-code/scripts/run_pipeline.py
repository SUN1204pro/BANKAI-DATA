"""Offline end-to-end pipeline entrypoint with budget management, checkpointing, and atomic finalization.

- Reads `/data` (or any split folder such as `train/` or `public_test/`) strictly read-only.
- Builds per-business SQLite databases under `--db-dir`, in parallel, reusing one only when both its
  input files and the ingestion code are unchanged.
- Loads `calendar.csv` and `rulebook.md` from the active split at runtime.
- Answers `questions.jsonl` with incremental checkpointing (`--checkpoint`) and a global wall-clock
  deadline plus safety reserve. A failed business or question abstains instead of ending the run.
- Atomically writes `/result/submission.jsonl` (or `--output`) guaranteeing 1 valid record
  per input question ID with zero duplicates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from concurrent.futures import ProcessPoolExecutor
import os
import signal

from src.agent import BusinessContext, answer_question
from src.evaluate import evaluate_files
from src.grounder import CalendarIndex, RulebookIndex
from src.ingest import ingest_business_folder, is_db_current
from src.postprocess import format_submission_record, validate_submission_record, write_submission_jsonl
from src.rule_guard import RuleGuard
from src.semantic import build_semantic_tables


def _build_business(job: tuple[Path, Path]) -> tuple[str, str | None]:
    """Build one business in a worker. A failure is reported, never allowed to end the whole run."""
    biz_folder, db_file = job
    try:
        ingest_business_folder(biz_folder, db_file)
        build_semantic_tables(db_file)
        return biz_folder.name, None
    except Exception as exc:  # noqa: BLE001
        return biz_folder.name, f"{type(exc).__name__}: {exc}"


def load_questions_jsonl(path: str | Path) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            questions.append(json.loads(s))
    return questions


def load_checkpoint(checkpoint_path: Path | None) -> dict[str, dict[str, Any]]:
    if checkpoint_path is None or not checkpoint_path.exists():
        return {}
    completed: dict[str, dict[str, Any]] = {}
    with checkpoint_path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
                if isinstance(rec, dict) and "id" in rec:
                    completed[str(rec["id"])] = rec
            except json.JSONDecodeError:
                continue
    return completed


def append_checkpoint(checkpoint_path: Path | None, record: dict[str, Any]) -> None:
    if checkpoint_path is None:
        return
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with checkpoint_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class GracefulStop(SystemExit):
    """Raised on SIGTERM so the current answers are published before exiting."""


def _on_sigterm(signum: int, frame: Any) -> None:
    raise GracefulStop(143)


def _usable_checkpoint(records: dict[str, dict[str, Any]], input_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Resume only from valid records for this input. A bad checkpoint must not stop the first publication."""
    wanted = set(input_ids)
    return {qid: rec for qid, rec in records.items() if qid in wanted and validate_submission_record(rec)[0]}


def run_pipeline(
    data_dir: str | Path,
    output_path: str | Path,
    *,
    db_dir: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    wall_clock_limit_sec: float = 21600.0,
    reserve_sec: float = 120.0,
    workers: int | None = None,
    evaluate_if_labels_present: bool = False,
    log_dir: str | Path | None = None,
    flush_every: int = 200,
) -> dict[str, Any]:
    """Run the offline QA pipeline over `data_dir`, keeping a complete valid submission at `output_path` throughout.

    Official Rule 8 scores only answers already in the file when a run stops, so the file is published before any
    other work and replaced atomically every `flush_every` answers. A hard kill keeps the last published snapshot:
    up to `flush_every` computed answers can be lost, and those questions remain abstain.
    """
    if flush_every <= 0:
        raise ValueError("flush_every must be positive")
    t_start = time.monotonic()
    deadline = t_start + wall_clock_limit_sec - reserve_sec

    split_path = Path(data_dir).resolve()
    out_file = Path(output_path).resolve()
    ckpt_file = Path(checkpoint_path).resolve() if checkpoint_path else None

    # 1. Read the input IDs and publish before calendar, rulebook, ingestion or any model startup.
    questions_file = split_path / "questions.jsonl"
    questions = load_questions_jsonl(questions_file)
    input_ids = [str(q["id"]) for q in questions]
    completed = _usable_checkpoint(load_checkpoint(ckpt_file), input_ids)

    def publish() -> None:
        write_submission_jsonl(
            out_file,
            [completed[qid] for qid in input_ids if qid in completed],
            expected_ids=input_ids,
            fill_missing_with_abstain=True,
        )

    publish()

    log_root = Path(log_dir).resolve() if log_dir is not None else out_file.parent / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    log_file = (log_root / "questions.jsonl").open("a", encoding="utf-8")

    def log_event(event: dict[str, Any]) -> None:
        log_file.write(json.dumps(event, ensure_ascii=False) + "\n")
        log_file.flush()

    def log_question(
        qid: str, record: dict[str, Any], status: str, elapsed: float, parsed_text: str | None = None, diagnostics: list[str] | None = None
    ) -> None:
        log_event({
            "event": "question", "id": qid, "status": status, "type": record.get("type"), "answer": record.get("answer"),
            "elapsed_sec": elapsed, "parsed_text": parsed_text, "diagnostics": diagnostics or [],
        })

    previous_handler = None
    handler_installed = False
    try:
        previous_handler = signal.signal(signal.SIGTERM, _on_sigterm)
        handler_installed = True
    except ValueError:
        pass  # Not the main thread: the caller owns signal handling.

    build_errors: dict[str, str] = {}
    counts_by_status: dict[str, int] = {"accepted": 0, "unanswerable": 0, "abstain": 0, "resumed": 0}
    contexts: dict[str, BusinessContext | None] = {}
    logged: set[str] = set()
    computed_since_publish = 0
    generation = 0

    def publish_snapshot() -> None:
        nonlocal computed_since_publish, generation
        publish()
        generation += 1
        computed_since_publish = 0
        log_event({"event": "snapshot", "generation": generation, "answers_published": len(completed)})

    try:
        if db_dir is not None:
            db_cache_root = Path(db_dir).resolve()
        else:
            default_local = REPO_ROOT / "artifacts" / "dbs" / split_path.name
            db_cache_root = default_local if default_local.exists() else Path(tempfile.gettempdir()) / "datamate_dbs" / split_path.name
        db_cache_root.mkdir(parents=True, exist_ok=True)

        # 2. Load runtime calendar.csv and rulebook.md from split_path
        calendar = CalendarIndex.from_csv(split_path / "calendar.csv")
        rulebook = RulebookIndex.from_markdown(split_path / "rulebook.md")

        # 3. Build each referenced business unless a DB from this exact input and code already exists.
        needed_businesses = sorted({str(q["business_id"]) for q in questions if str(q["id"]) not in completed})
        jobs = []
        for biz_id in needed_businesses:
            biz_folder = split_path / "data" / biz_id
            db_file = db_cache_root / f"{biz_id}.sqlite"
            if not biz_folder.is_dir():
                build_errors[biz_id] = f"Missing business folder: {biz_folder}"
            elif not is_db_current(db_file, biz_folder):
                jobs.append((biz_folder, db_file))
        if jobs:
            pool = ProcessPoolExecutor(max_workers=workers or os.cpu_count() or 1)
            try:
                for biz_id, err in pool.map(_build_business, jobs):
                    if err:
                        build_errors[biz_id] = err
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            pool.shutdown()

        guard = RuleGuard.from_rulebook(rulebook)
        for line in guard.report():
            print(f"[RULEBOOK] {line}", file=sys.stderr)

        # 4. Answer each question within budget. Questions on a business that failed to build abstain.
        def context_for(biz_id: str) -> BusinessContext | None:
            if biz_id not in contexts:
                db_file = db_cache_root / f"{biz_id}.sqlite"
                if biz_id in build_errors or not db_file.exists():
                    contexts[biz_id] = None
                else:
                    try:
                        contexts[biz_id] = BusinessContext(db_file)
                    except Exception as exc:  # noqa: BLE001
                        build_errors[biz_id] = f"{type(exc).__name__}: {exc}"
                        contexts[biz_id] = None
            return contexts[biz_id]

        for q in questions:
            qid = str(q["id"])
            if qid in completed:
                counts_by_status["resumed"] += 1
                log_question(qid, completed[qid], "resumed", 0.0)
                logged.add(qid)
                continue

            asked_at = time.monotonic()
            ctx = context_for(str(q["business_id"]))
            if asked_at >= deadline or ctx is None:
                reason = "Global deadline reached" if asked_at >= deadline else "Business could not be built"
                completed[qid] = format_submission_record(qid, "abstain")
                counts_by_status["abstain"] += 1
                log_question(qid, completed[qid], "abstain", 0.0, diagnostics=[reason])
            else:
                outcome = answer_question(q, ctx, calendar=calendar, rulebook=rulebook, guard=guard)
                completed[qid] = outcome.submission_record
                counts_by_status[outcome.status] = counts_by_status.get(outcome.status, 0) + 1
                append_checkpoint(ckpt_file, outcome.submission_record)
                log_question(
                    qid, outcome.submission_record, outcome.status, time.monotonic() - asked_at,
                    outcome.parsed_text, outcome.diagnostics,
                )
            logged.add(qid)
            computed_since_publish += 1
            if computed_since_publish >= flush_every:
                publish_snapshot()
    finally:
        # Normal completion and catchable stops. SIGKILL and crashes rely on the snapshots already published.
        for qid in input_ids:
            if qid not in logged:
                log_question(qid, completed.get(qid, {"type": "abstain"}), "not_attempted", 0)
        publish_snapshot()
        for ctx in contexts.values():
            if ctx is not None:
                ctx.close()
        log_file.close()
        if handler_installed:
            signal.signal(signal.SIGTERM, previous_handler)

    elapsed_sec = time.monotonic() - t_start
    summary: dict[str, Any] = {
        "split": split_path.name,
        "total_questions": len(questions),
        "counts_by_status": counts_by_status,
        "build_errors": build_errors,
        "elapsed_sec": round(elapsed_sec, 3),
        "output_path": str(out_file),
    }

    # Optional development-time evaluation when answers.jsonl exists and --evaluate is passed
    answers_file = split_path / "answers.jsonl"
    if evaluate_if_labels_present and answers_file.exists():
        eval_report = evaluate_files(answers_file, out_file, questions_path=questions_file)
        summary["evaluation"] = {
            "penalized_accuracy": round(eval_report.overall.penalized_accuracy, 6),
            "correct": eval_report.overall.correct,
            "wrong": eval_report.overall.wrong,
            "abstain": eval_report.overall.abstain,
            "coverage": round(eval_report.overall.coverage, 6),
            "answered_accuracy": round(eval_report.overall.accuracy_on_answered, 6),
        }

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the offline DataMate QA pipeline.")
    parser.add_argument(
        "--data-dir",
        default="/data",
        help="Input split directory containing rulebook.md, calendar.csv, questions.jsonl, and data/",
    )
    parser.add_argument(
        "--output",
        default="/result/submission.jsonl",
        help="Output JSONL submission path",
    )
    parser.add_argument(
        "--db-dir",
        default=None,
        help="Directory for built/cached per-business SQLite databases",
    )
    parser.add_argument(
        "--checkpoint",
        default=None,
        help="Optional JSONL checkpoint file for incremental resume",
    )
    parser.add_argument(
        "--wall-clock-limit-sec",
        type=float,
        default=21600.0,
        help="Total wall-clock time budget in seconds (default: 21600 = 6 hours)",
    )
    parser.add_argument(
        "--reserve-sec",
        type=float,
        default=120.0,
        help="Safety reserve in seconds before deadline to finalize submission",
    )
    parser.add_argument("--workers", type=int, default=None, help="Parallel build workers (default: all CPUs)")
    parser.add_argument("--log-dir", default=None, help="Per-question logs (default: <output folder>/logs)")
    parser.add_argument("--flush-every", type=int, default=200, help="Publish the submission every N answers")
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Evaluate generated submission against <data-dir>/answers.jsonl if present",
    )
    args = parser.parse_args()

    summary = run_pipeline(
        args.data_dir,
        args.output,
        db_dir=args.db_dir,
        checkpoint_path=args.checkpoint,
        wall_clock_limit_sec=args.wall_clock_limit_sec,
        reserve_sec=args.reserve_sec,
        workers=args.workers,
        log_dir=args.log_dir,
        flush_every=args.flush_every,
        evaluate_if_labels_present=args.evaluate,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
