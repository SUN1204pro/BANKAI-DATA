# DataMate — Zalo AI Challenge 2026

An agent that answers everyday-Vietnamese questions over CSV / Excel / SQLite files, or says
`unanswerable` / `abstain` when it should. It runs fully offline in a Docker image.

**Scoring (rulebook §9): +1 correct, −1 wrong, 0 abstain.** So the agent answers only when it is more
likely right than wrong (`DATAMATE_MIN_AGREE`, default 0.5, to be calibrated on the dev split).

## How it works

```
question + as_of_date + business folder
   1. ingest.py     clean tables -> in-memory SQLite, schema card (types, ranges, sample values),
                    full README (column meanings, unit conversions), time-of-day kept as <date>_time
   2. timeparse.py  "tuần rồi", "tháng này", "cùng kỳ năm ngoái" -> exact date ranges   (pure code, rulebook §2)
      lunar.py      Tết (23 tháng Chạp .. mùng 10 tháng Giêng), holidays, rằm/mùng -> solar dates from calendar.csv
   3. ground.py     words in the question -> real cell values (accent- and typo-tolerant)
   4. prompts.py    system prompt = instructions + the WHOLE rulebook (identical for every question, so vLLM's
                    prefix cache computes it once); user prompt = README + schema + time ranges + hints + question
   5. agent.py      LLM writes SQL -> execute -> repair on error -> vote over samples -> answer | unanswerable | abstain
   6. fmt.py        exact answer format per type (rulebook §9)
```

The LLM decides intent; code handles everything that has a right answer (dates, matching, rounding, formatting).

## Data layout (train set, confirmed)

```
train/questions.jsonl   {"id","business_id","question","as_of_date"}       3000 questions, 30 businesses
train/answers.jsonl     {"id","type","answer","alias":[...]}               gold; types: number 63%, percent 19%,
                                                                          list 6%, date 5%, string 3%, unanswerable 2%, ...
train/rulebook.md       shared definitions (about 15k characters)
train/calendar.csv      date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday
train/data/biz_101/     README.md + the business's files
```

## Commands

```bash
pip install -e '.[dev]' && pytest                       # no GPU needed

# 1) split train by business: 24 businesses to fit on, 6 held out as dev
python scripts/split_train.py --train train --out splits

# 2) plumbing check without a model (everything abstains; shows ingestion warnings and the gold mix)
DATAMATE_BACKEND=null DATAMATE_LOG_DIR=./logs python run.py --data train \
    --questions splits/dev_questions.jsonl --out splits/dev_null.jsonl
python eval/evaluate.py --pred splits/dev_null.jsonl --gold splits/dev_answers.jsonl --questions splits/dev_questions.jsonl

# 3) real run (GPU, model under /models/main or DATAMATE_MODEL=...)
DATAMATE_LOG_DIR=./logs python run.py --data train --questions splits/dev_questions.jsonl --out splits/dev_sub.jsonl
python eval/evaluate.py --pred splits/dev_sub.jsonl --gold splits/dev_answers.jsonl \
    --questions splits/dev_questions.jsonl --errors splits/dev_errors.json

# competition run (what the Docker image does): one worker per GPU, then fills gaps with abstain
bash scripts/launch_dp.sh
```

Per-question traces (resolved dates, hints, every candidate SQL and its vote) are in `<log dir>/qa_shard*.jsonl`.

Environment knobs: `DATAMATE_MODEL`, `DATAMATE_N_SAMPLES` (3), `DATAMATE_MAX_REPAIRS` (2), `DATAMATE_MIN_AGREE` (0.5),
`DATAMATE_MAX_MODEL_LEN` (20480), `DATAMATE_DEADLINE_S` (18900 = 5h15m), `DATAMATE_CHUNK_SIZE` (64), `DATAMATE_LOG_DIR`.

## Model declaration (required by Rule 2 — fill in before submitting)

| Model | Parameters | Licence | Released | Role |
|---|---|---|---|---|
| _TODO_ | | | | SQL generation |

Total across all models must be ≤ 15B (count exact parameters, MoE total included).

## Rule compliance

| Rule | How it is handled |
|---|---|
| 2 — ≤15B params, declared | table above (**you fill it in**) |
| 3 — offline | weights baked into the image; `HF_HUB_OFFLINE=1`; no network code anywhere |
| 4 — no hard-coding | nothing is keyed on question or business ids; tests use a mock LLM only |
| 5 — code execution | generated SQL runs on an in-memory copy; only `SELECT`/`WITH`, `PRAGMA query_only`, 20 s timeout |
| 6 — no test-data training | none; train data and `scripts/synth_data.py` only |
| 7 — 6 h budget | answers streamed to disk per chunk; soft deadline → 1 sample, no repair; hard deadline → abstain |
| 8 — reproducible | greedy first sample, fixed seed for the others, per-question logs in `/result/logs/` |

## Status

Verified by tests (107, no GPU): time resolver and Tết/holiday rules against the rulebook, number/date parsing, output
formats and rounding, +1/−1/0 scorer, answer/abstain decision, ingestion of CSV/Excel/SQLite, resume after a crash.

**Not yet verified**
1. `llm.py` (vLLM backend) has never run on a GPU.
2. Ingestion has only been tried on synthetic files, not the 30 real businesses. Rulebook §8 rules still to implement once the
   real files are inspected: blank cells below an order's first line inherit its customer/status; two identical lines of
   the same order count once; dates without a year take the sheet's year.
3. `layout.py` heuristics are expected to work with `--data train` (checked by reading the folder listing only).
4. `alias` in gold answers is used by the scorer; the submission never contains it.
5. `min_agree` and `n_samples` are untuned.
6. `/result/logs/` as the log location is a guess; check the Docker guideline.

## Roadmap

dev-split baseline → error buckets (time, entity, rule, unit conversion, unanswerable) → fix the biggest bucket first →
few-shot examples retrieved from fit businesses → tune `min_agree` → optional LoRA fine-tune on SQL that reproduces the gold
answers → full timing run on a 2×5090-class budget.
