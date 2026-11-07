"""Container-ready entrypoint for Zalo AI Challenge 2026 DataMate submission.

Reads `/data` read-only and writes `/result/submission.jsonl` (or CLI overrides).
Delegates to `scripts/run_pipeline.py`.
"""

from __future__ import annotations

from scripts.run_pipeline import main

if __name__ == "__main__":
    raise SystemExit(main())
