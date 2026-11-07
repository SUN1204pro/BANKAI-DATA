#!/usr/bin/env bash
# Container entrypoint: read the test set from /data, write /result/submission.jsonl and /result/logs/.
# DATA_DIR, RESULT_DIR and PYTHON can be overridden for local checks. Inputs are never modified.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
RESULT_DIR="${RESULT_DIR:-/result}"
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x "$(dirname "${BASH_SOURCE[0]}")/.venv/bin/python" ]; then
  PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.venv/bin/python"
else
  PY=python3
fi

cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p "$RESULT_DIR/logs"

# Databases go in a folder owned by this run, outside the read-only data mount.
WORK_DIR="$(mktemp -d)"
child=""
cleanup() { rm -rf "$WORK_DIR"; }
forward() {
  if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap forward TERM INT

"$PY" run_submission.py \
  --data-dir "$DATA_DIR" \
  --output "$RESULT_DIR/submission.jsonl" \
  --log-dir "$RESULT_DIR/logs" \
  --db-dir "$WORK_DIR/dbs" &
child=$!

# `wait` returns early when a signal arrives, so wait again until the child has really exited.
status=0
while true; do
  wait "$child" && status=0 || status=$?
  if ! kill -0 "$child" 2>/dev/null; then break; fi
done
exit "$status"
