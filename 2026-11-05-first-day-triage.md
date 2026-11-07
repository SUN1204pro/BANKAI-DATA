# First-Day Failure Triage Plan

**Requested by the user on 2026-11-05:** create a new branch and do the first-day decision gate from [the two-alternatives plan](2026-11-05-datamate-two-alternatives.md). This is measurement only. It does not change `src/` or `scripts/`.

## Problem and Why

| Problem | Why it matters |
|---|---|
| We don't know what kind of failure dominates | The local-model option A only helps with wording. It can't fix a missing calculator or a data-reading error, so building it first could waste most of the 11 days left |
| Current train results can't answer this | Only 9 train questions still abstain, and the rules were tuned on all of them |
| The 134 wrong answers can't be attributed | Public test has no labels and the source of that number is unconfirmed, so it can't be used as evidence here |

## Failure Classes

1. **Language or qualifier:** the data and calculator exist, but the wording wasn't understood.
2. **Missing calculator or source attribute:** the wording was understood, or the data exists, but nothing computes it.
3. **Metadata or ingestion:** a column, unit or table wasn't mapped or read correctly.
4. **Not a failure of the system:** for example, a correct abstain.

## Steps

1. **Least-tuned code.** Export commit `2e5128d` to the scratchpad, without touching this checkout, and run it on the held-out businesses. Its 62 failures come from before question-level tuning on those businesses, so they best approximate unseen shops. Classify each by hand.
2. **Weak READMEs.** Count abstain reasons on the current code with `no-columns` and `empty` READMEs, by class.
3. **Unfamiliar wording.** Take a seeded sample of correctly answered train questions, rewrite each in different wording that keeps its meaning, and score against the original labels. This is the most direct test of what A could recover. Abstains here are A's possible gain. Wrong answers are a guard leak.
4. **Hardware.** Check whether this machine can run the plan's model and container smoke tests.
5. **Decision.** Write the counts and the gate result to `artifacts/triage/` and to this file.

## Limits

- I would write the paraphrases myself and I know the parser's vocabulary, so they may be easier than real wording. Report that as author correlation.
- Held-out was already opened, so step 1 can't be a clean reading. It only shows failure kinds.
- No public-test inference and no use of public-test questions.
- Coverage-table rows aren't question counts, so they won't be used as prevalence.

## Result (measured 2026-11-05 on commit `d901093`)

**Gate result: wording and parsing dominate every measurement that can see them, so the evidence points to A. A can't start yet, because this machine has no usable GPU and no model has been downloaded.**

### 1. Least-tuned code, held-out businesses (commit `2e5128d`, 609 correct, 6 wrong, 56 abstained)

| Class | Failures | Of which wrong | Examples |
|---|---:|---:|---|
| Wording | 36 | 5 | `bỏ ra`, `hai số`, `sp`, `bằng bao nhiêu phần trăm`, Telex typos |
| Parser defect on wording it recognized | 13 | 0 | `3.200đ` not read as a price, `≥ 4 đơn` read as money, Telex damage to `Plusssz Max` |
| Metadata or ingestion | 8 | 0 | `Hạng` column unmapped, names stored with phones |
| Missing calculator | 5 | 1 | Nearest-expiry lot, out of stock, claim chains |
| **Total** | **62** | **6** | |

79% are language-side. All of these are now fixed, so this shows the kind of failure a new business causes, not a current defect. The classification was done by hand.

### 2. Fresh wording on the current code

80 correctly answered train questions, seeded and stratified by answer type, rewritten by me and scored against their original labels.

| Correct | Wrong | Abstain | PA | Coverage | Precision |
|---:|---:|---:|---:|---:|---:|
| 51 | 2 | 27 | **0.6125** | 66.25% | 96.2% |

- All 27 abstains are wording over a calculator that exists: `doanh số` with `cả`, `đem lại`, `tiêu thụ`, `trị giá`, `biến động`, `đạt đỉnh`, `liên tiếp`.
- Both wrong answers are real guard gaps:
  - "nợ mới bao nhiêu và đã trả nợ bao nhiêu" asks for two numbers and got one.
  - "tính tới hôm nay" was parsed as a second period, giving 922.49% instead of 22.11%.
- With 80 questions, the abstain rate is only known to about 24% to 45%.

### 3. Weak READMEs on the current code, all train

| README | Correct | Wrong | Abstain |
|---|---:|---:|---:|
| No column definitions | 2,784 | 0 | 216 |
| Empty | 2,752 | 0 | 248 |

These are metadata failures by construction, since only the README changed. Top causes with empty READMEs:

- Order identity unresolved in three shops: 108 questions
- A total column that could be goods or payable: 24 questions
- Goods amount unreadable in one shop: 23 questions

A model might help, but some of this information may be gone from an empty README, so these labels can't prove it. Row-ledger shops may be recoverable with deterministic rules.

### 4. Hardware

| Item | Status |
|---|---|
| Machine | Apple M1 Pro, 16 GB |
| NVIDIA GPU | None, so the target smoke test can't run here |
| Docker | Installed; daemon not running |
| Local model | None downloaded |

### What This Can't Tell Us

- **It can't forecast the private score.** Train questions look templated, and contest questions may stay closer to those templates than my rewrites do. PA 0.6125 is a stress result, and the clean held-out 0.9508 is the familiar-wording result.
- **Author correlation:** I wrote the rewrites and know the parser's vocabulary. That may make them easier than real wording, or the deliberate rewording may make them harder.
- **New industries are invisible.** Train has no mother-and-baby, repair or phone shop, so missing or misread calculators there are undercounted. Those may be behind the 134 wrong answers, and A wouldn't fix that.
- **The 134 wrong answers remain unattributed.**

Evidence, local only because `artifacts/` is not in git: `artifacts/triage/heldout_old_code_classified.json`, `paraphrases.json`, `paraphrase_ids.json`, `paraphrase_results.json`, `weak_readme_failures.json`.

## What To Do Next

**Decisions for the user**
1. Approve a download of one small model (Qwen3-4B-Instruct-2507, quantized, a few GB) for a local pilot on this machine, or provide GPU access. The pilot would give the model the 27 abstained and 2 wrong rewrites and check whether its plans are correct. It wouldn't prove anything about speed or fit on the target hardware.
2. Allow public-test inference, logging only abstain reason counts and no answers. That is the only available evidence on the new industries, so it needs your decision under Rule 6.
3. Find the Docker guideline and start the Docker daemon, so the zero-model container can be tested.

**Possible without a model**
4. Fix the two guard gaps found here, test-first: a question asking for two values must not return one, and a second time phrase must not create a second period silently.
5. Look at recovering row-ledger shops without README help.

**Not recommended**
- Adding the 27 rewrites' words to the vocabulary and calling coverage fixed. That only tunes on this test and would make the number meaningless.
