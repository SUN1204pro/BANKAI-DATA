"""Stage 1 of the hybrid run: the rule-based Zalo engine (no LLM, CPU only).

It builds a clean per-business SQLite database (subtotal rows removed, order groups recovered), then answers
every question it can. Each question ends as `accepted` (a value), `unanswerable` (the data provably lacks it) or
`abstain` (it does not understand or trust the data). The caller keeps the first two and hands the abstentions to
the LLM.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from zalo_engine.agent import BusinessContext, answer_question
from zalo_engine.grounder import CalendarIndex, RulebookIndex
from zalo_engine.ingest import ingest_business_folder, is_db_current
from zalo_engine.rule_guard import RuleGuard
from zalo_engine.semantic import build_semantic_tables

log = logging.getLogger("datamate.zalo")

KEEP = ("accepted", "unanswerable")  # statuses whose answer is final; everything else goes to the LLM


@dataclass
class ZaloResult:
    line: dict                      # the submission record (already validated by the Zalo formatter)
    status: str                     # accepted | unanswerable | abstain
    diagnostics: list[str] = field(default_factory=list)
    parsed_text: str | None = None

    @property
    def final(self) -> bool:
        return self.status in KEEP


def _build(job: tuple[Path, Path]) -> tuple[str, str | None]:
    """Build one business database in a worker process. A failure is returned, never raised."""
    folder, db_file = job
    try:
        ingest_business_folder(folder, db_file)
        build_semantic_tables(db_file)
        return folder.name, None
    except Exception as exc:  # noqa: BLE001
        return folder.name, f"{type(exc).__name__}: {exc}"


def build_databases(
    business_ids: list[str], find_biz: Callable[[str], Path | None], db_dir: Path, workers: int
) -> dict[str, str]:
    """Build (or reuse) one database per business. Returns {business_id: error} for the ones that failed."""
    db_dir.mkdir(parents=True, exist_ok=True)
    errors: dict[str, str] = {}
    jobs: list[tuple[Path, Path]] = []
    for biz_id in business_ids:
        folder = find_biz(biz_id)
        if folder is None:
            errors[biz_id] = "no data folder"
            continue
        db_file = db_dir / f"{biz_id}.sqlite"
        if not is_db_current(db_file, folder):
            jobs.append((folder, db_file))
    if not jobs:
        return errors
    if workers <= 1 or len(jobs) == 1:
        results = [_build(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            results = list(pool.map(_build, jobs))
    for name, err in results:
        if err:
            errors[name] = err
    return errors


def run_zalo(
    questions: list[dict],
    find_biz: Callable[[str], Path | None],
    rulebook_path: Path | None,
    calendar_path: Path | None,
    db_dir: Path,
    workers: int | None = None,
    budget_s: float | None = None,
) -> dict[str, ZaloResult]:
    """Answer `questions` with the rule-based engine. Questions not reached within `budget_s` are simply absent
    from the result (the caller treats them like abstentions)."""
    t0 = time.monotonic()
    out: dict[str, ZaloResult] = {}
    if not questions:
        return out

    rulebook = RulebookIndex.from_markdown(rulebook_path) if rulebook_path else RulebookIndex("", [])
    calendar = None
    if calendar_path is not None and calendar_path.suffix.lower() == ".csv":
        try:
            calendar = CalendarIndex.from_csv(calendar_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("zalo calendar disabled (%s): %s", calendar_path, exc)
    guard = RuleGuard.from_rulebook(rulebook)
    for line in guard.report():
        log.info("[RULEBOOK] %s", line)

    biz_ids = sorted({str(q["business_id"]) for q in questions})
    errors = build_databases(biz_ids, find_biz, db_dir, workers or os.cpu_count() or 1)
    for biz_id, err in errors.items():
        log.warning("zalo could not build %s: %s", biz_id, err)
    log.info("zalo databases ready after %.1fs (%d businesses, %d failed)", time.monotonic() - t0, len(biz_ids), len(errors))

    contexts: dict[str, BusinessContext | None] = {}

    def context_for(biz_id: str) -> BusinessContext | None:
        if biz_id not in contexts:
            db_file = db_dir / f"{biz_id}.sqlite"
            ctx = None
            if biz_id not in errors and db_file.exists():
                try:
                    ctx = BusinessContext(db_file)
                except Exception as exc:  # noqa: BLE001
                    log.warning("zalo cannot open %s: %s", biz_id, exc)
            contexts[biz_id] = ctx
        return contexts[biz_id]

    try:
        for q in questions:
            if budget_s is not None and time.monotonic() - t0 > budget_s:
                log.warning("zalo budget (%.0fs) used up; %d questions left for the LLM", budget_s, len(questions) - len(out))
                break
            ctx = context_for(str(q["business_id"]))
            if ctx is None:
                continue
            outcome = answer_question(q, ctx, calendar=calendar, rulebook=rulebook, guard=guard)
            out[str(q["id"])] = ZaloResult(outcome.submission_record, outcome.status, outcome.diagnostics, outcome.parsed_text)
    finally:
        for ctx in contexts.values():
            if ctx is not None:
                ctx.close()

    counts: dict[str, int] = {}
    for r in out.values():
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info("zalo stage done in %.1fs: %s", time.monotonic() - t0, counts)
    return out
