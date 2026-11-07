#!/usr/bin/env bash
# Create a new timestamped submission folder under artifacts/submissions/.
#   ./run.sh              public test (default)
#   ./run.sh train        train, which also prints its score
#   ./run.sh PATH         any folder with questions.jsonl, rulebook.md, calendar.csv and data/
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
exec "$PY" scripts/make_submission.py "$@"
