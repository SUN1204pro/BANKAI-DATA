# DataMate Solution

Answers everyday Vietnamese business questions over messy CSV, Excel and SQLite files, or declines when the data cannot support an answer. Built for the DataMate track of Zalo AI Challenge 2026, using data from `challenge.zalo.ai`.

## Models Used

**None. 0 parameters.** No language model, embedding model, reranker or classifier runs at inference, and there is no training step or weights. It needs only the Python 3 standard library, and makes no network calls.

## Method

1. **Ingestion.** Reads every file, sheet and table, finds headers, removes subtotal and repeated-header rows with an audit trail, recovers order groups, and parses Vietnamese money, quantity and date formats.
2. **Column meaning.** Comes from the business README first, then known header names, then values, which are accepted only with independent evidence such as exact `quantity × price = amount` with a unit or currency mark.
3. **Clean tables.** Orders, lines, payments, purchases, stock, lots, expenses and catalogs, plus a record per business of what it records and what is unresolved.
4. **Question parsing.** Handles missing diacritics, Telex typing and slang, resolves time against `as_of_date`, and matches products, customers and groups. A question with a word or number the chosen calculation does not account for abstains, so it never answers a broader question.
5. **Calculation.** One fixed calculator per rulebook clause, with exact Decimal rounding. Rule coverage is listed in [RULEBOOK_COVERAGE.md](RULEBOOK_COVERAGE.md).
6. **Answer or decline.** Returns `unanswerable` only when the data provably lacks the information, and `abstain` when a mapping is unresolved or data is incomplete.
7. **Changed rulebooks.** Reads the mounted rulebook. A clause not seen in development makes affected questions abstain, and is reported at startup.

## How to Run

Entry point, reading `/data` and writing `/result`:

```bash
./predict.sh
```

For local use, override the paths:

```bash
DATA_DIR=public_test RESULT_DIR=/tmp/result ./predict.sh
```

To create a new timestamped submission folder under `artifacts/submissions/`:

```bash
./run.sh
```

Input is read only. Temporary databases go in a folder created for the run and removed afterward.

## Output

- `/result/submission.jsonl`: every input ID once. An all-abstain file is published before other work starts, then replaced atomically every 200 answers and at the end.
- `/result/logs/questions.jsonl`: one event per question, with `id`, `status`, `type`, `answer`, `elapsed_sec`, `parsed_text` and `diagnostics`, plus snapshot events. It is appended as answers are computed, so it can hold answers newer than the last published snapshot.

On a hard kill, the last published snapshot survives. Up to 200 computed answers can be lost, and those questions stay abstain. `SIGTERM` publishes current answers before exiting with code 143.

Answers contain no randomness. Two runs on the same input produced byte-identical submissions in local tests; timing fields in the logs differ.

## Data Used

- **Rules and vocabulary:** the supplied `rulebook.md` files and train data. Train labels are used for development only, never at inference.
- **Public-test data:** earlier development read samples of public-test questions and data and added parsing rules based on them. No answers were created or collected. Clarification on this has not yet been received from the organizers (Rule 6).
- **External data:** none.

## Tests

```bash
.venv/bin/python -W ignore -m unittest tests.test_evaluate tests.test_ingestion tests.test_query_contracts tests.test_column_inference tests.test_weak_readme tests.test_make_submission tests.test_qualifier_guard tests.test_rule_wording
```

Tests that need public-test databases are skipped unless `DATAMATE_ALLOW_PUBLIC_FIXTURES=1` is set. Skipped tests count as not run.

## Known Limits

- Question understanding is hand-written patterns, so unfamiliar wording abstains. That limits coverage on unseen phrasing.
- Some rulebook clauses still abstain, and others have no labeled data to check them against. Both are listed in [RULEBOOK_COVERAGE.md](RULEBOOK_COVERAGE.md).
- Runtime on the official hardware has not been measured.
- The Dockerfile is not included yet, because the organizers' Docker guideline, which sets the base image, has not been obtained.
