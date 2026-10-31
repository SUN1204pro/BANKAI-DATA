#!/usr/bin/env bash
# Data-parallel launcher: one worker process per GPU, then fill any gaps with abstain.
# The clock starts here so the 6-hour budget includes model loading.
set -uo pipefail
cd "$(dirname "$0")/.."

export DATAMATE_START_TS="${DATAMATE_START_TS:-$(date +%s)}"
export VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

N="${NUM_SHARDS:-$(nvidia-smi -L 2>/dev/null | wc -l)}"
[ "$N" -ge 1 ] || N=1

pids=()
for ((i = 0; i < N; i++)); do
  CUDA_VISIBLE_DEVICES=$i python run.py --shard "$i" --num-shards "$N" "$@" &
  pids+=($!)
done

rc=0
for pid in "${pids[@]}"; do wait "$pid" || rc=1; done

# Even if a worker crashed, the submission must be complete and valid.
python run.py --finalize-only "$@"
exit $rc
