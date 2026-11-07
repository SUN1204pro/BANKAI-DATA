"""Build normalized per-business SQLite databases for one or more dataset splits.

Discovers business folders dynamically under `<split_dir>/data/` and invokes
`src.ingest.ingest_business_folder`, reporting row accounting, subtotal
reconciliation, and diagnostic summaries.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

# Ensure repo root is importable when executed as a script
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from concurrent.futures import ProcessPoolExecutor
import os

from src.ingest import IngestionReport, ingest_business_folder, is_db_current
from src.semantic import build_semantic_tables


def _build_one(job: tuple[Path, Path]) -> IngestionReport:
    b_dir, db_file = job
    report = ingest_business_folder(b_dir, db_file)
    build_semantic_tables(db_file)
    return report


def build_split_databases(
    split_dir: str | Path,
    output_dir: str | Path,
    *,
    workers: int | None = None,
    force: bool = True,
) -> list[IngestionReport]:
    """Ingest all business folders under `<split_dir>/data/` into `<output_dir>/*.sqlite`."""
    s_path = Path(split_dir)
    data_dir = s_path / "data" if (s_path / "data").is_dir() else s_path
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    biz_dirs = sorted(
        [p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith(".")],
        key=lambda x: x.name,
    )

    jobs = [
        (b_dir, out_path / f"{b_dir.name}.sqlite")
        for b_dir in biz_dirs
        if force or not is_db_current(out_path / f"{b_dir.name}.sqlite", b_dir)
    ]
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count() or 1) as pool:
        return list(pool.map(_build_one, jobs))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build normalized SQLite databases for DataMate splits."
    )
    parser.add_argument(
        "--splits",
        nargs="+",
        default=["train", "public_test"],
        help="Dataset split directories to ingest (default: train public_test).",
    )
    parser.add_argument(
        "--output-root",
        default="artifacts/dbs",
        help="Output root directory for SQLite databases (default: artifacts/dbs).",
    )
    args = parser.parse_args()

    t0 = time.perf_counter()
    total_businesses = 0
    total_sheets = 0
    accounting_mismatches = 0
    subtotal_total = 0
    subtotal_mismatches = 0
    total_diagnostics = 0

    for split_arg in args.splits:
        split_path = Path(split_arg)
        if not split_path.exists():
            print(f"[SKIP] Split path does not exist: {split_path}")
            continue
        out_dir = Path(args.output_root) / split_path.name
        print(f"=== Ingesting split: {split_path} -> {out_dir} ===")
        reports = build_split_databases(split_path, out_dir)
        for rep in reports:
            total_businesses += 1
            for sh in rep.sheet_summaries:
                total_sheets += 1
                if sh.raw_row_count != sh.accounted_total:
                    accounting_mismatches += 1
                    print(
                        f"  [ACCOUNTING MISMATCH] {rep.business_id} {sh.source_file}:{sh.source_sheet} "
                        f"raw={sh.raw_row_count} accounted={sh.accounted_total}"
                    )
            for chk in rep.subtotal_checks:
                subtotal_total += 1
                if chk.delta != 0:
                    subtotal_mismatches += 1
                    print(
                        f"  [SUBTOTAL DELTA] {rep.business_id} {chk.source_file}:{chk.source_sheet} "
                        f"row={chk.source_row} ({chk.subtotal_label!r}, {chk.target_col}): "
                        f"expected={chk.expected_amount} computed={chk.computed_amount} delta={chk.delta}"
                    )
            for d in rep.diagnostics:
                total_diagnostics += 1
                print(
                    f"  [DIAG {d.severity.upper()}] {rep.business_id} {d.code}: {d.message} ({d.context})"
                )

    elapsed = time.perf_counter() - t0
    print(
        f"\nCompleted {total_businesses} businesses ({total_sheets} sheets/tables) in {elapsed:.2f}s.\n"
        f"  Row accounting mismatches : {accounting_mismatches}\n"
        f"  Subtotal checks           : {subtotal_total - subtotal_mismatches}/{subtotal_total} matched (mismatches={subtotal_mismatches})\n"
        f"  Ingestion diagnostics     : {total_diagnostics}"
    )
    return 1 if (accounting_mismatches > 0 or subtotal_mismatches > 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
