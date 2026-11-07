# DataMate: two alternatives to the current deterministic pipeline

**Research date:** 2026-11-05. **Decision document, not an implementation or a claim of improved test accuracy.**

## 1. Recommendation

**Keep the data engine. Change the interface between language and computation.**

Choose between:

1. **A - Small-model semantic router:** a local Vietnamese-capable model translates questions into a strict, closed set of calculation plans. Existing deterministic calculators execute them. This is the recommended **first prototype**, conditional on error attribution and an early serving test, not a proven best use of the remaining time.
2. **B - README-grounded query compiler:** a larger local model translates the active rulebook, business metadata and question into a typed relational plan, which a deterministic compiler turns into bounded SQLite queries. This is the higher-ceiling, higher-risk option for unfamiliar metrics and schemas.

Neither means asking a model to read spreadsheets and guess totals. Neither requires a vector database, arbitrary generated Python, multiple database engines, or an always-on model ensemble.

The existing [baseline plan](../BASELINE_PIPELINE_PLAN.md) already contains many sound ideas, including structured plans, deterministic execution and selective verification. These alternatives make two **different implementation commitments** explicit:

- A deliberately restricts what the model may compute, and concentrates on paraphrases, qualifiers, entity grounding and validated metadata repair.
- B deliberately allows new compositions of supported operators and source attributes, rather than requiring one manually written calculator per question family.

They are promising alternatives, **not yet demonstrated to be better**. Promote one only after a controlled comparison against the unchanged baseline.

**First-day decision gate:** classify fresh train/synthetic failures and existing coverage gaps into (i) language/qualifier, (ii) missing calculator/source attribute, and (iii) metadata/ingestion. Use question counts where available; counts of coverage-document rows are not estimates of question prevalence. If recoverable wording failures dominate, prototype A. If supported source information is missing from the calculation layer, prioritize those capabilities or a tightly scoped B pilot. If data integrity or deployment dominates, spend the time there and keep the current answer engine. Neither LLM option is mandatory.

### Decision table

| Aspect | Current implementation | A: semantic router | B: query compiler |
|---|---|---|---|
| Question understanding | Handwritten normalization, patterns and qualifier checks | Model-to-typed-plan, with deterministic fast path | Model-to-typed relational plan |
| Execution | Fixed Python/SQLite calculators | Same calculators, selected through a validated contract | Deterministically compiled SQLite plus existing exact-arithmetic helpers |
| New wording for an existing metric | Needs patterns or abstains | Principal intended gain | Also supported |
| New combination of known metrics/filters | Limited by calculator/parser branches | Only registered combinations | Can compose supported operators |
| Previously unused documented source attributes | Usually require semantic/schema changes | Limited, explicit additions | First-class typed extension views |
| New rulebook wording | Fingerprint guard can block | Can recognize supported semantics; otherwise blocks | Can compile expressible semantics with evidence; otherwise blocks |
| Main danger | Coverage ceiling and overfit pattern acceptance | Semantically wrong plan with valid syntax | Wrong interpretation, join or formula producing plausible SQL |
| Training | None | Optional train-only plan SFT after a zero-shot pilot | Start without fine-tuning; optional later |
| Suggested starting model | None | Qwen3.5-4B versus Qwen3-4B-Instruct-2507; 8B if needed | Qwen3.5-9B versus Qwen3-14B |
| Engineering scope | Preserve as fallback | Smaller incremental change | Larger contract/compiler/validation project |
| Best reason to choose | Safest emergency submission | Best risk-adjusted improvement before Nov 16 | Greater adaptability if implementation and GPU time are available |

The model names are starting candidates, not a universal ranking. See section 7 for eligibility and selection limits.

## 2. What was actually inspected

### 2.1 Current implementation versus historical plans

The repository was clean at the beginning of this research, at commit `d901093`.

- Read the [current README](../README.md), [progress record](../PROGRESS.md), [coverage matrix](../RULEBOOK_COVERAGE.md), [baseline plan](../BASELINE_PIPELINE_PLAN.md), and the relevant sections of the [flow diagram](../doc/end_to_end_data_flow.html).
- Traced the answer path through [agent.py](../src/agent.py), [question_parser.py](../src/question_parser.py), [metrics.py](../src/metrics.py), [rule_guard.py](../src/rule_guard.py), and [run_pipeline.py](../scripts/run_pipeline.py).
- Inspected the semantic schema and existing metadata/rule retrieval and SQL-execution helpers.

**Important distinction:** the implementation uses **zero model parameters**. It does not currently run the LLM-generated SQL path described in the architectural plan. The [SQL execution helper](../src/verifier.py) is available for future use but is not the current question-answer execution route.

The flow diagram describes commit `c5e919a`; its scores, specific abstentions and bug notes are historical. The newer progress document also distinguishes contaminated train results from earlier estimates. Do not combine those documents into a fictitious current benchmark.

### 2.2 Dataset inventory, remeasured

Read all **44 business READMEs**. Inspected all workbook sheet inventories and both SQLite table inventories. Read all train question/label records for structural/type statistics, and examined representative train questions, raw CSV rows, Excel cells and relational schemas.

Public-test inspection was limited to documentation, file/sheet/table inventory, and question record structure/counts. **No public-test answers or inference runs were produced, and no new public-derived parsing/training patterns were added.**

| Remeasured property | Train | Public test |
|---|---:|---:|
| Businesses / business READMEs | 30 | 14 |
| Questions / unique IDs | 3,000 / 3,000 | 1,000 / 1,000 |
| Business CSV files, excluding calendar | 20 | 10 |
| Excel workbooks | 107 | 69 |
| Workbook sheets | 302 | 136 |
| SQLite databases | 1 | 1 |
| Tables in each supplied SQLite database | 17 | 17 |
| Business file bytes, including READMEs | 193,479,600 | 109,129,911 |
| README length, characters | 739-8,539 | 737-5,219 |
| READMEs without `## Cột trong` sections | 4 | 3 |

The two calendars have identical SHA-256:
`c2579905e8b4eb152a4833b05f1f72de3e3d2c517b5d610d7664aeeb2d6a75b3`.

All question records have `id`, `business_id`, `question`, `as_of_date`. Train question lengths range from 18 to 200 characters.

Absence of a column-section heading does **not** mean a README is uninformative: the SQLite READMEs describe tables, joins, amounts and stock semantics in prose. A heading-only metadata extractor is therefore insufficient.

### 2.3 Train labels explain where effort should go

| Answer type | Count | Share |
|---|---:|---:|
| Number | 1,884 | 62.80% |
| Percent | 578 | 19.27% |
| List | 193 | 6.43% |
| Date | 145 | 4.83% |
| String | 88 | 2.93% |
| Unanswerable | 70 | 2.33% |
| Boolean | 23 | 0.77% |
| Table | 19 | 0.63% |

**82.07% are number or percent answers.** Language understanding matters, but most final values should be calculated, not generated.

Only 19 labeled tables and 23 booleans make aggregate accuracy a poor diagnostic for those formats. Likewise, the 70 `unanswerable` labels cannot justify a broad missing-data classifier without additional controlled fixtures.

Train examples include missing accents, slang, mixed English/Vietnamese, Telex errors, product packaging numbers, comparisons and named units. For example, the inspected questions include a quantity-weighted purchase price, revenue for one exact ingredient, delivery batch counts, and a price-change comparison with two seven-day windows. A model must recover these distinctions, not merely recognize the topic.

### 2.4 Why READMEs are part of the program, not background reading

Train alone supplies strong evidence for these requirements:

| Evidence | Architectural consequence |
|---|---|
| [Header-only invoices](../train/data/biz_110/README.md) distinguish goods value from total payable and contain no item detail | Answerability depends on table grain, not just having an orders table |
| [No-order-ID ledger](../train/data/biz_107/README.md) records one item entry per row | Do not invent order groups or deduplicate equal event rows |
| [Grouped handwritten ledger](../train/data/biz_105/README.md) has return evidence and snapshot stock | Preserve original events, group inheritance and snapshot dates |
| [Relational CSV](../train/data/biz_111/README.md) separates payments from orders | Order payment method, later receipts and revenue are different concepts |
| [Construction records](../train/data/biz_115/README.md) describe separate delivery dates and batches | Count events at their own grain and time role |
| [Pharmacy records](../train/data/biz_119/README.md) allow a sales line to draw from several lots | Joining allocations to sales without preaggregation can multiply money |
| [SQLite metadata](../train/data/biz_123/README.md) describes balances after stock movements | Historical stock is a last-event state, not a sum of balance cells |
| [Mixed-source shop](../train/data/biz_124/README.md) uses prose instead of column sections | Read prose, headers and validated relationships together |
| [Restaurant data](../train/data/biz_128/README.md) separates tables, customers and order-level amounts | A universal flat sales table loses distinctions needed by some questions |

Representative raw checks also showed:

- A two-row Excel header with sparse coordinates, followed by both Excel serial dates and date strings.
- Money strings such as `55.000đ` and `25k` in the same workbook.
- A title/blank-row preamble before the actual POS header.
- In the train SQLite shop: 33,630 orders, 64,149 sales lines, 33,816 receipts and 65,514 stock movements. These counts illustrate distinct grains, not expected runtime constants.

Public documentation confirms the need for flexibility: monthly file partitions, merged order/line exports, prose-heavy metadata, service visits, warranty events, installment ledgers and explicit alias sheets all appear. The [public rulebook](../public_test/rulebook.md) also adds mother-and-baby, repair and phone-shop rules absent from the train rulebook.

This is **compatibility evidence, not labeled validation**. Do not turn inspected public-specific layouts, constants or phrasing into training examples. General mechanisms and their development fixtures must come from train, the rulebooks, or independently designed synthetic data under the current local policy.

## 3. Diagnosis of the current approach

### Keep these strengths

1. Lossless/raw-first ingestion, provenance and row-accounting concepts.
2. README-derived meanings and one SQLite database per business.
3. Metric-specific eligibility instead of treating revenue, cash, debt and stock identically.
4. Exact monetary formatting and a local rulebook-equivalent scorer.
5. Explicit `abstain` versus source-backed `unanswerable`.
6. Early all-abstain publication, atomic snapshots, and per-question logs.
7. Fast, inspectable deterministic calculators as a baseline and fallback.

### Address these structural limits

**Language coverage is closed.** The parser's allowed vocabulary and branches cannot cover every paraphrase or new combination. Adding vocabulary alone can also make unsupported conditions appear accepted. A structured qualifier contract is needed, not simply a more permissive word list.

**The calculation vocabulary is also closed.** An LLM that only selects existing functions cannot answer a documented question requiring a missing function. This is the principal difference between A and B.

**The semantic schema is useful but selective.** Its tables do not expose every source attribute/event. Examples in the coverage document include unsupported installment schedules, phone warranty events and some specific-entity filters. Preserve typed source extensions rather than forcing every possible business fact into sales lines.

**Rule fingerprints detect change but do not interpret it.** Recognizing that prose changed is not equivalent to adapting its semantics. Conversely, wording can change without changing the rule. Any replacement must retain explicit uncertainty and dependency tracking.

**Evidence is heavily reused.** The reported all-train PA of 0.9970 is a regression result, not evidence of 99.7% performance on unseen businesses. The historical 671-question held-out set was later inspected and tuned against; it cannot become pristine again by renaming it.

**Local errors are often correlated.** Two models, or two SQL queries, using the same wrong order grouping can agree exactly. Verification must sometimes return to source rows and key/cardinality evidence.

## 4. Shared foundation for both alternatives

### 4.1 A versioned business contract

Extend existing metadata rather than create a parallel ingestion engine. For each source/view expose:

- source file, sheet/table, raw columns and source-row references;
- README evidence location and content hash;
- logical role, row grain, candidate keys and **validated** key scope;
- join edges, uniqueness, overlap and expected cardinality;
- types, monetary/quantity scales, selling/base units and conversion evidence;
- date roles: order, payment, delivery, purchase, warranty, expiry, snapshot;
- missing/empty/unknown/conflicting attributes;
- coverage intervals and snapshot dates;
- supported calculations and unresolved dependencies.

Capability states should distinguish:

1. **Available and validated**.
2. **Documented absent**, supporting `unanswerable` for a dependent request.
3. **Present but unresolved/invalid**, requiring `abstain`.
4. **Not yet inspected**, which is not absence.

A model may propose a mapping with citations, but the host validates references, keys and invariants. Never turn a high model confidence score into a data fact.

Keep validated base mappings and already accepted baseline outputs immutable. Apply model-proposed unresolved mappings in a separate candidate version/overlay; do not let a metadata experiment silently alter every previously supported question for that business.

### 4.2 Runtime document compilation

1. Read the actual mounted rulebook, including prose outside Markdown tables.
2. Read every business README, including definitions in paragraphs and bullets.
3. Build deterministic mappings first.
4. Ask the local model only for unresolved semantic mappings, once per affected business/source.
5. Validate each proposed mapping and retain rejected proposals as diagnostics.
6. Cache only within an input/rule/code/model-hash namespace.

Unknown data shapes remain unsupported; no runtime human review, external API, or schema map keyed on a supplied business ID is allowed.

Use lexical retrieval and source-key lookup first. The READMEs are modest in length, but full schemas, catalogs and rule dependencies still need selective retrieval. Do not promise that an entire business fits a fixed tiny token budget.

### 4.3 Question obligation ledger

Every candidate plan must account for:

- every measure and requested output position;
- entities, exclusions, thresholds, comparisons and groups;
- period, relative date anchor, time role and snapshot/flow distinction;
- unit, denominator, ranking criterion and tie order;
- relevant defaults and explicit question overrides.

Attach original-text spans to plan fields. Unresolved obligations prevent acceptance. Span coverage is a useful check, **not proof that the model understood the words**; validate it with contrastive tests where a single qualifier changes the answer.

For override forms recognized by the existing deterministic parser, require the corresponding typed field to agree. A model cannot erase an explicit exclusion, comparator or time override by attaching its words to a generic span. A detected override that the executor cannot express must block the plan.

## 5. Alternative A: small-model semantic router

### 5.1 Flow

```text
Runtime documents + existing ingestion
                    |
       Validated business contract
                    |
Question -> deterministic parser -> typed candidate plan
                    |
     no match / ambiguity / selected risk
                    v
      local 4B-8B semantic planner
                    |
   schema + capability + obligation validation
                    |
    registered calculator, with bound arguments
                    |
 exact result + evidence checks + acceptance gate
                    |
        current formatter/publication path
```

The deterministic path remains first for established cases. The model proposes **plans, not final numeric answers**, and cannot issue arbitrary SQL or Python.

### 5.2 Model output contract

A plan needs at least:

```json
{
  "metric": "revenue",
  "date_role": "order_date",
  "window": {"kind": "month_to_date"},
  "filters": [],
  "group_by": [],
  "comparison": null,
  "output": {"type": "number", "measure_kind": "money_vnd"},
  "rule_refs": ["runtime-rule-reference"],
  "obligations": [],
  "unresolved": []
}
```

This is an illustrative skeleton, not an executable example. Real requests require source-backed rule references, bound entity keys and complete obligations. The host resolves relative dates using `as_of_date` and the current calendar/rules; the model cannot invent dates or identifiers.

Use a registry declaring each calculator's supported arguments and capabilities. Unsupported filters are rejected **before** calling a calculator. Multi-measure answers are a bounded composition of registered calculations with explicit output order.

Do not convert the model's plan back into a Vietnamese sentence and run the old regex parser again. That would preserve the language bottleneck and introduce a second interpretation step.

### 5.3 Training plan, if the initial pilot warrants it

The train labels contain final answers, **not gold SQL or gold plans**.

1. Freeze grouped training/calibration/evaluation assignments before extracting traces or selecting demonstrations.
2. Instrument the deterministic system to expose candidate plans for known train questions.
3. Audit approximately 300-500 diverse plans, prioritizing qualifiers, money versus quantity, debt timing, ranking, missing data and multiple outputs.
4. Retain a trace only if its plan is semantically supported and its execution matches the label. Answer matching alone is insufficient.
5. Test plans on counterfactual synthetic databases: change order status, payment date, entity code, duplicate placement or conversion factor. Wrong plans that accidentally matched one answer should fail.
6. Add a small number of meaning-preserving train-only paraphrases per accepted plan; split by source question/business **before** augmentation. Add explicit unresolved-obligation/abstention targets, source-backed `unanswerable` targets, and contrastive valid-versus-unsupported qualifiers. Include positive zero/false/empty answers so "unusual result" does not become a rejection heuristic.
7. If useful, train one LoRA adapter with early stopping on plan correctness and PA, not training loss alone. Merge into one final checkpoint for deployment.
8. Compare the fine-tuned model with its untuned checkpoint on the same frozen suite. Do not assume SFT improves out-of-domain behavior.

A locally run eligible larger teacher may assist training-time trace generation; it must not be included or used in the final inference path if doing so exceeds the cap. Do not send repository data to external teacher APIs.

No reinforcement-learning project is needed for the first version. With only 3,000 final-answer labels, a complicated reward-training loop can amplify spurious programs and consume the submission schedule.

Reuse the repository's existing qualifier-test cases and the stress methodology documented in the progress report rather than inventing all negatives anew. **Do not label all 6,000 historical stress attempts as abstentions:** that report explicitly identifies 66 non-abstentions as supported customer-group filters, with one unchanged result not fully investigated. Audit negative labels and hold out new qualifier families. Model-generated paraphrases are useful augmentation, not evidence of real-world paraphrase coverage.

### 5.4 Verification and baseline preservation

- For model-only recovery, execute only when all required capabilities and argument bindings are validated.
- For disagreement with a previously accepted deterministic answer, record both plans and check their differing assumptions. Do not blindly replace or majority-vote.
- For the first release candidate, allow only abstention recovery; model disagreement with an accepted baseline answer is logged in shadow mode and cannot veto or replace it. Treat veto/replacement as a separate experiment, not part of A's initial scope.
- A shared ingestion failure is not repaired by model agreement. Send the affected request to source-level checks or abstain.
- Retain current output behavior for unaffected baseline cases, including correct zero, false and empty answers.

### 5.5 Scope and likely trade-off

This option should help with **new wording over already supported calculations**. It will not create missing installment, warranty or arbitrary business calculators by itself.

Estimated implementation scope: **4-6 focused engineering days plus GPU validation**, assuming the existing data layer can be reused. This is a planning estimate, not a commitment or benchmark.

Stop and ship the existing baseline if the model adds wrong answers faster than it recovers useful abstentions.

## 6. Alternative B: README-grounded query compiler

### 6.1 Flow

```text
All source data ----------------------> raw and typed source tables
Active rulebook + every README -------> validated semantic contract
                                                    |
Question -> grounded entities + rule dependencies -> local planner
                                                    |
                              typed relational operator plan
                                                    |
                           deterministic plan/type/grain checker
                                                    |
                       compile SQLite + exact arithmetic steps
                                                    |
                        bounded read-only execution worker
                                                    |
                 selective source reconciliation / alternate plan
                                                    |
                            calibrated answer / abstain
```

The model selects semantic operations. The compiler owns SQL construction and critical business invariants.

B is best understood as **implementing the previously planned LLM route under a stricter compiler contract**, not inventing an entirely new architecture. Its specific additions are a closed operator vocabulary without a free-SQL escape hatch, typed source-extension views, and enforceable capability/obligation contracts.

### 6.2 What makes this different from ordinary Text-to-SQL

Allow a bounded operator vocabulary:

- scan a validated source/view;
- filter with typed comparisons and resolved entity keys;
- join over an approved key edge;
- aggregate and count distinct at an explicit grain;
- project permitted arithmetic expressions;
- rank/top-k with deterministic ties;
- compare two windows or measures;
- select a last event at a cutoff;
- test existence or return an ordered set/table.

A ratio must declare its numerator, denominator and unit. A stock balance must declare its event/snapshot semantics. A sum of order money after a one-to-many join is rejected unless the plan explicitly restores the correct grain.

Reuse existing calculators as trusted subplans where possible. Start with **no free-form SQL escape hatch**. If later evidence justifies one, give it a separate, stricter acceptance route; never quietly bypass the plan checker.

### 6.3 Preserve typed source extensions

Keep canonical orders/lines/payments for common questions, but retain a typed view of every relevant source table with original keys and attributes.

This avoids the bottleneck of projecting every business into a fixed sales schema before knowing the question. It also supports a documented calculation over attributes for which the team did not write a dedicated metric function.

Do not materialize every possible metric or generate new Python modules for each shop. Compile only grounded plans over validated data.

### 6.4 Reading new rules without silently inventing rules

Represent supported rule semantics as data: eligible states, row roles, event-date choice, comparator, grouping, denominator, amount definition and rounding.

- Equivalent wording can resolve to the same supported rule representation.
- A changed threshold/default can resolve to a different supported representation, with current-source evidence.
- An unfamiliar concept or ambiguity that cannot be represented and validated stays blocked.
- The absence of a rule from train is not itself a license to use a train default on private test.
- Missing information must be established from the sources, not from the compiler failing to express a calculation.

This is bounded runtime interpretation, **not a promise to correctly compile arbitrary natural-language policy**.

For a first contest release, narrow automatic rule changes further: support only already validated clause structures and enumerated parameter slots, such as a threshold, comparator, date role or state set. Match the clause structure and extract/validate the slot from the active text; merely finding the same number somewhere in the document is insufficient. Structural rule changes stay blocked.

Measure the impact radius of each new rule/mapping: how many questions and capabilities depend on it. A worst-case switch from correct to wrong across M questions costs `2M/N`; abstention recovery that is wrong costs `M/N`. These are different baselines, not a universal cost for every change. Do not enable a high-impact mapping solely because two model prompts agree. Restrict initial use to validated recovery routes and retain the existing safe path.

### 6.5 Execution and selective verification

Use one eligible checkpoint for planning, unresolved metadata interpretation, targeted repair and optional review. Different prompts over the same weights do not provide statistically independent evidence.

For high-risk plans, verify the suspected failure mode:

| Risk | Useful independent evidence |
|---|---|
| Order/line/event fan-out | Key counts and totals before/after joins |
| Return eligibility | Trace the selected orders to all documented return sources |
| Wrong amount definition | Reconcile line goods, order payable and receipt amounts separately |
| Historical snapshot misuse | Check observation date and availability of intervening events |
| Unit errors | Reconcile documented preconverted quantity with conversion graph |
| Ranking or denominator | Recompute candidate set and denominator at the correct grain |
| README mapping uncertainty | Inspect cited header/prose and counterexamples, not just model agreement |

Initially allow one targeted repair after the first plan; retain the existing plan's maximum of two repairs only if measurements justify the extra budget. A valid zero is never a reason to retry until a nonzero answer appears.

The existing SQLite helper is a starting point, not a complete certification for this broader route. Validate its allowed tables/functions, process-level time/memory termination, output limits and rejection of mutation/attachment/extension loading. Exact fractional arithmetic may need fixed-scale integers or deterministic Decimal helpers; SQLite `REAL` or `NUMERIC` affinity is not a monetary guarantee.

### 6.6 Scope and likely trade-off

This option can address both language and **compositional/schema coverage**, but it creates a larger space of plausible wrong answers.

Estimated implementation scope: **7-10 focused engineering days plus substantial verification**, with overlapping work or unusually strong existing infrastructure needed to fit the current deadline comfortably.

Choose it only if source-grounded compiler correctness can be demonstrated quickly. Otherwise narrow it to a few validated operator families and keep A/current calculators as the production path.

## 7. Models, parameter budget and evidence

### 7.1 Starting shortlist

Primary references: [Qwen3 release and language coverage](https://qwenlm.github.io/blog/qwen3/), [Qwen3-14B](https://huggingface.co/Qwen/Qwen3-14B), [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B), [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507).

| Candidate | Published rounded total | License | Why benchmark it |
|---|---:|---|---|
| Qwen3.5-4B | About 4.66B including additional components | Apache-2.0 | Newer compact challenger for A; hybrid-attention serving needs validation |
| Qwen3.5-9B | About 9.65B including additional components | Apache-2.0 | Newer larger challenger for B, with substantially more budget headroom than 14B |
| Qwen3-4B-Instruct-2507 | 4.0B | Apache-2.0 | Compact, non-thinking instruction model for a narrow typed-plan task |
| Qwen3-8B | 8.2B | Apache-2.0 | More capacity for qualifiers and semantic mappings |
| Qwen3-14B | 14.8B | Apache-2.0 | Larger single model for B's broader interpretation task |
| Qwen2.5-Coder-14B-Instruct | 14.7B | Apache-2.0 | Code-oriented comparator; Vietnamese contest performance unverified |
| Phi-4 | About 14B | MIT | Reasoning comparator, but English-centric documentation makes it a lower-priority choice |
| Phi-4-mini-instruct | About 3.8B | MIT | Compact comparator; Vietnamese is not in its documented supported-language list |

The Qwen3 family explicitly lists Vietnamese among its supported languages. That is more relevant than an English coding leaderboard, but **language support is not a DataMate accuracy guarantee**.

The Qwen3/Phi/Qwen2.5 candidates were released in 2024-2025. The publisher's [dated News entry](https://github.com/QwenLM/Qwen3.5#news) announces public availability of Qwen3.5-9B and Qwen3.5-4B on **2026-03-02**, also before the contest's **2026-10-26** cutoff. The repository currently presents newer Qwen releases but retains that announcement. For Qwen2.5-Coder-14B, use the actual checkpoint's release history rather than treating the first technical-report date as the release of every size. Model-card numbers are rounded; exact artifact accounting is a release gate.

Native context is not the deployment context budget. Start around 8K-16K for grounded tasks and expand only when measured retrieval omissions require it. Long-context support does not make filling a context with raw ledgers a good design.

Exact totals returned by the Hugging Face checkpoint API's `safetensors` metadata:

| Checkpoint | Stored tensor parameter total |
|---|---:|
| Qwen3.5-9B | 9,653,104,368 |
| Qwen3.5-4B | 4,659,865,088 |
| Qwen3-14B | 14,768,307,200 |
| Qwen3-8B | 8,190,735,360 |
| Qwen3-4B-Instruct-2507 | 4,022,468,096 |

Sources: [Qwen3.5-9B metadata](https://huggingface.co/api/models/Qwen/Qwen3.5-9B?expand=safetensors), [Qwen3.5-4B metadata](https://huggingface.co/api/models/Qwen/Qwen3.5-4B?expand=safetensors), [14B metadata](https://huggingface.co/api/models/Qwen/Qwen3-14B?expand=safetensors), [8B metadata](https://huggingface.co/api/models/Qwen/Qwen3-8B?expand=safetensors), [4B metadata](https://huggingface.co/api/models/Qwen/Qwen3-4B-Instruct-2507?expand=safetensors). Pin revisions and revalidate the packaged artifact; these counts are not a license to add uncounted heads or adapters.

**Newer candidates must not be ignored:** official [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B) and [Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B) checkpoints exist, with Apache-2.0 metadata and stored totals of **9,653,104,368** and **4,659,865,088**, respectively. These totals include more than their marketed text-size labels; count the complete checkpoint, including vision components, even for text-only use.

The inspected Qwen3.5 cards advertise broader multilingual coverage, but an explicit Vietnamese-specific capability list was not verified. That is a reason to benchmark Vietnamese tasks, not a reason to reject them. The [9B config](https://huggingface.co/Qwen/Qwen3.5-9B/raw/main/config.json) and [4B config](https://huggingface.co/Qwen/Qwen3.5-4B/raw/main/config.json) specify dense text MLPs with hybrid linear/full attention and a vision encoder, not expert-routed MLPs. Generic family-level MoE marketing does not describe every size. Count all stored parameters regardless of which modalities are used.

Use the March 2 public-availability announcement as release evidence, not the earlier repository-creation timestamp. Pin an eligible revision and archive its license/release record.

### 7.2 Recommended selection procedure

**For A:** start with Qwen3.5-4B versus Qwen3-4B-Instruct-2507, then test Qwen3-8B only if the compact candidates fail on interpretation quality. Prefer the smaller/faster model if their PA and critical error rates are indistinguishable. Serving reliability can outweigh model recency.

**For B:** compare Qwen3.5-9B with Qwen3-14B, using the same selected weights for every neural role. A newer 9.65B checkpoint could beat an older 14.77B checkpoint; no local benchmark in this research settles that. Include the compact winner as a cost/quality comparator if time permits.

Do not add an embedding model initially: lexical search over headers, names and explicit aliases costs zero model parameters.

An optional Qwen3-8B + Qwen3-4B-Instruct-2507 pair totals **12,213,203,456** stored parameters, below the cap, but should be deployed only if a separate verifier produces measured gains over reusing one model. Same-family agreement is not independence.

Reject models such as Qwen3-30B-A3B or gpt-oss-20b under this contest cap: their **total** parameters exceed 15B even though their activated count is much smaller. The newer Qwen3.6-27B/35B and Qwen3.8-27B/2.4T releases listed in the same official News section also exceed the cap; newer does not mean eligible.

### 7.3 Compliance ledger

Before downloading/packaging a final candidate, record:

- publisher, checkpoint revision and public release evidence;
- license and any additional use restrictions;
- exact parameter count, including embeddings, output heads, vision modules and auxiliary models where present;
- adapter treatment; preferably ship one merged checkpoint, not several independently adapted variants;
- quantization format and tensor/kernel support;
- tokenizer/chat template, decoding configuration and serving versions;
- local weight/dependency hashes and a software/data provenance manifest.

Quantization changes storage and memory, **not** parameter count. Sequentially loading another model still counts. Reusing one model in multiple prompt roles avoids adding distinct weights. If replica/adapter accounting is ambiguous, obtain organizer clarification; the conservative default is one model instance, optionally sharded across GPUs.

## 8. Evaluation that can actually choose a winner

### 8.1 Treat existing results honestly

**Freshly rerun during this research:** the unchanged pipeline built all 30 train businesses and processed all 3,000 train questions with its local scorer:

| Result | Fresh measurement |
|---|---:|
| Correct / wrong / abstain | 2,991 / 0 / 9 |
| PA / coverage | 0.9970 / 99.70% |
| Precision among answered | 100% |
| Accepted ordinary answers / `unanswerable` outputs | 2,924 / 67 |
| Reported business-build errors | 0 |
| Wall time reported by pipeline | 315.902 seconds |

This run used two ingestion workers in the local macOS environment. It was not a target-GPU benchmark and is not directly comparable with earlier runs using different concurrency. It confirms regression behavior, not universal ingestion correctness, a pristine holdout result or an official score.

The [progress report](../PROGRESS.md) separately records:

- all-train: 2,991 correct, 0 wrong, 9 abstain, PA 0.9970;
- an earlier, less-contaminated 671-question reading: 639 correct, 1 wrong, 31 abstain, PA 0.9508;
- later held-out tuning, after which that set is regression evidence.

Neither number predicts private-test PA. Do not optimize the nine remaining train abstentions as if they were the full remaining problem.

Executed evidence commands used the project interpreter and a session-local read-only inventory/runner:

```text
.venv/bin/python <session-files>/research_evidence.py
.venv/bin/python <session-files>/research_evidence.py --baseline
```

The second called the existing `run_pipeline` with `data_dir=train`, `workers=2`, a fresh temporary database directory, session-local output, and `evaluate_if_labels_present=True`. Both commands exited 0. Temporary databases were removed by the runner's scoped temporary-directory cleanup. The inventories and baseline summary remain with the session for provenance.

The complete unit suite, all-subtotal reconciliation, public-test inference, LLM inference/training and target-hardware Docker runs were **NOT RUN** for this planning task.

### 8.2 Freeze a prospective comparison

1. Keep the baseline code/model-free outputs fixed.
2. Create grouped train/calibration/evaluation splits for **new model training**; keep all paraphrases of one original together.
3. Also evaluate leave-one-business/layout/industry-out where feasible. Existing hand-built rules have seen train, so label these as comparative stress results, not pristine end-to-end holdouts.
4. Reuse the existing original/no-column/empty-README ablation harness and frozen historical reports as an immediate paired **robustness regression**, after verifying compatible source/code versions. These have more headroom than the original train input, but have already been used in development and are not a new untouched benchmark.
5. Before tuning, freeze a modest additional train-derived/synthetic stress suite with separate development and final template families. Cover layouts and rulebook semantics without copying public-test examples. Changing only random numbers does not create a genuinely new reasoning distribution. An independently authored final subset is preferable; without one, explicitly report author-correlation risk.
6. Include licensed public Text-to-SQL evaluation only as auxiliary evidence; its SQL accuracy does not substitute for Vietnamese business-rule accuracy.
7. Keep public-test inference and leaderboard use as a separate user-authorized, frozen release check. Do not train on it or collect its answers.

No new split can undo prior exposure. The goal is a transparent prospective comparison, not a new claim of uncontaminated train accuracy.

**Ablation oracle caveat:** removing a README can remove information genuinely needed for the answer, such as an otherwise undocumented conversion. Do not force original labels onto such altered inputs and reward hallucinated recovery. Use original gold values only on cases where sufficient evidence remains; separately audit/relabel source-insufficient cases or mark their oracle unresolved. A lower empty-README score is not automatically a parser defect, nor evidence that B can recover it.

### 8.3 Required stress cases

- New paraphrases, accents removed, Telex errors and polite filler.
- Same familiar words in a different logical arrangement.
- One extra exclusion, entity, threshold, branch or time qualifier.
- Two equal customer names with different codes.
- Dates at month/week boundaries and all-history balances.
- Future return evidence versus future payment/stock events.
- Repeated order totals, many-to-many joins and sales/event duplicates.
- Missing README sections, renamed columns, reordered/split sheets.
- Empty field versus unsupported interpretation versus genuine zero.
- Changed rule text and separately changed rule semantics.
- Ordered multi-number lists, ties, tables and fractional quantities.
- Unseen synthetic schema combinations with independently computed answers.

Renaming and row shuffling must preserve the documented semantics. Do not shuffle grouped ledgers arbitrarily and then call changed results a pipeline defect.

### 8.4 Metrics and promotion gates

Report:

- PA, C/W/A, coverage and precision among answered;
- `unanswerable` correctness separately;
- results by business/layout/metric/output type;
- recovered baseline abstentions, new wrong answers, and degraded correct answers;
- plan and mapping validity, join/qualifier failures;
- latency p50/p95/p99, startup/ingestion times, prompt/generated tokens, peak GPU/CPU memory;
- full rerun answer differences and deadline behavior.

For a fixed N:

```text
PA = coverage * (2 * answered_precision - 1)

Gain from recovering R correct abstentions
and introducing W wrong abstentions = (R - W) / N.

Changing a baseline-correct answer to wrong costs 2 / N.
Changing a baseline-correct answer to abstain costs 1 / N.
```

Thus a larger model can have more correct answers yet a worse PA.

Proposed gates, to freeze before comparison:

1. No regressions in output contracts, exact arithmetic, input isolation or deterministic calculator tests.
2. Account for every changed baseline answer; do not accept a worse all-train regression result without an explicit, evidence-backed trade-off.
3. Positive paired PA improvement on applicable existing robustness cases and the frozen prospective suite, with uncertainty reported using business-cluster bootstrap intervals. If there are few businesses, report that limitation. With only nine original-train abstentions, there is insufficient evidence to promote A solely from recovery on the original input. If the evaluation sample remains too weak, keep shadow mode rather than manufacture a statistical win.
4. No hidden critical slice regressions; rare output types need dedicated tests rather than aggregate averages.
5. Early target-hardware serving/container smoke test before architecture commitment, then a complete cold run with useful safety margin below six hours.
6. Repeat-run stability and deadline/interruption tests.

A 0.01 PA improvement can be a useful project target on the **new stress suite**, not a requirement on the already saturated 0.997 train score. Its value is a policy choice, not a measured improvement.

### 8.5 Acceptance calibration

The theoretical break-even correctness probability is 0.5 because answering yields expected reward `2p - 1`.

Do not use the model's self-reported confidence as that probability. Estimate route-level correctness using frozen calibration data and features such as mapping validation, unsupported obligations, ambiguity, rule novelty and deterministic checks. Keep the estimator simple and account for its parameters if it is an additional inference model.

Select thresholds for PA and risk; use conservative uncertainty bounds for sparse routes. The baseline's tiny residual abstention set is insufficient to calibrate a recovery model. Use prospective paraphrase/structural cases as well, while acknowledging distribution shift.

An `unanswerable` output is a scored answer, not a safe substitute for abstention. Unsupported code or ambiguous mapping means `abstain`, not proof that the source lacks the information.

## 9. Runtime, offline packaging and scheduling

### 9.1 Hardware is a gate, not an assumption

The [live track rules](https://challenge.zalo.ai/portal/datamate) specify two RTX 5090 GPUs with 32 GB each, 512 GB RAM, and a six-hour wall-clock limit including startup.

Two GPUs do not behave like one unpartitioned 64 GB allocation. A 14.8B model has about **29.6 GB of BF16 weights alone**, before KV cache, activations and framework overhead. This arithmetic is not a measured fit or throughput claim.

At 9.653B parameters, Qwen3.5-9B has about 19.3 GB of BF16 weights alone, which makes a single-GPU benchmark attractive. Its vision and hybrid-attention components still require explicit loader/kernel testing even when requests contain only text.

Benchmark:

- one compact BF16 model on one GPU;
- a validated quantized large model where kernels support it;
- one larger model sharded over both GPUs if required.

Do not assume tensor parallelism is faster than a single-GPU quantized deployment. Communication, batch size, prompt length and memory pressure decide.

[PyTorch's Blackwell support](https://pytorch.org/blog/pytorch-2-7/) and [vLLM GPU installation documentation](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/) are relevant starting points. Consumer RTX 5090 `sm_120` must be tested explicitly; documentation for datacenter Blackwell does not certify every consumer quantization kernel.

Obtain the organizers' Docker guideline before choosing the final base image. Package all weights, tokenizers, wheels and required kernels; test startup from cold caches with network disabled. Do not depend on a runtime JIT step downloading a compiler/kernel.

### 9.2 Bounded inference

Measure the budget as:

```text
startup + ingestion + document interpretation + prompt prefill
+ generation + SQL/calculation + repairs/verification
+ publication + safety reserve <= 21,600 seconds
```

Start with short structured outputs, a bounded context, one candidate and one targeted repair. Increase candidates or thinking budget only for routes where measured PA gain justifies the cost.

Use per-business reusable contracts/prompt prefixes, source-hash caches and deterministic batching. Benchmark reasoning versus non-reasoning modes instead of using long reasoning on every question.

Budget against the actual input N; do not assume private test has 1,000 questions.

### 9.3 Preserve useful answers under a deadline

1. Publish one valid abstain record per input ID **before** loading models.
2. Run validated deterministic cases first where feasible.
3. Prioritize additional work by estimated marginal PA benefit per second, not merely easiest-first or shortest-output-first.
4. Publish by both answer count and elapsed time; a count-only threshold of 200 can lose many minutes of LLM work.
5. Stop launching work before the deadline and publish the latest safe results.
6. Use one coordinator as output writer, stable ID ordering and atomic replacement.
7. Resume only with matching input, rulebook, model and code fingerprints.

A model startup failure should be explicit in logs; the system may still finish with the validated deterministic baseline rather than lose the whole submission.

Seeded/greedy decoding does not guarantee GPU bitwise determinism. Pin the environment and test reruns, including batching and concurrent scheduling. The contest requires PA agreement within 0.03, while byte-identical output remains a stronger local target.

## 10. File-level implementation plan

These are proposed changes after choosing A or B, not edits performed by this research.

| File / surface | A | Additional B work |
|---|---|---|
| [metadata_compiler.py](../src/metadata_compiler.py) | Evidence references, capability states, validated local-model proposals | Typed source extensions and rule representations |
| [ingest.py](../src/ingest.py), [normalizers.py](../src/normalizers.py) | Preserve existing pipeline; touch only demonstrated support gaps | Exact typed source attributes needed by the compiler |
| [semantic.py](../src/semantic.py) | Expose existing keys, grain, capability/health dependencies | Source extension views without lossy forced canonicalization |
| New `src/query_plan.py` | Typed plan, obligation ledger, calculator registry and validation | Typed relational operator plan and compiler |
| [question_parser.py](../src/question_parser.py) | Expose deterministic plan candidates; retain normalizers and grounding | Remains fast-path proposer |
| [grounder.py](../src/grounder.py), [temporal.py](../src/temporal.py) | Reuse entity/calendar/rule evidence; preserve question overrides | Dependency-complete rule retrieval and new supported compositions |
| [rule_guard.py](../src/rule_guard.py) | Route changed clauses to supported semantic interpretation or block | Validate expressible changed rules; never disable guard globally |
| New `src/local_model.py` | One pinned offline model interface, structured output, caps and errors | Same interface reused for planner/mapping/repair roles |
| [agent.py](../src/agent.py) | Route candidates, validate, execute, gate | Compile and execute relational plans |
| [metrics.py](../src/metrics.py) | Existing exact calculators with explicit argument support | Reusable trusted primitives and exact-arithmetic steps |
| [verifier.py](../src/verifier.py) | Deterministic evidence checks where appropriate | Wire bounded generated-query execution and harden its tested contract |
| [postprocess.py](../src/postprocess.py) | Preserve types, ordering and rounding contracts | Same |
| [run_pipeline.py](../scripts/run_pipeline.py), [predict.sh](../predict.sh) | Deadline budget, timed publication, model-load failure handling | Same, with compiler-worker resources |
| [tests](../tests/) | Typed-plan, qualifier, regression, calibration and fallback tests | Operator/SQL/grain/source-extension and adversarial execution tests |
| New training script, only if SFT wins | Audited train-only plan examples and reproducible tuning | Optional later |
| New Dockerfile / dependency lock | After receiving official base-image guidance | Same |
| [README.md](../README.md), [RULEBOOK_COVERAGE.md](../RULEBOOK_COVERAGE.md) | Declare actual weights, data provenance and measured limits | Distinguish closed calculators from compiled coverage |

Suggested interfaces:

```text
validate_plan(plan, business_contract, question_context) -> validated_plan | diagnostics
propose_plan(question_context, retrieved_evidence) -> candidate_plan
execute_registered_plan(validated_plan, business_context) -> answer_and_evidence
compile_relational_plan(validated_plan) -> bounded_query_and_exact_postprocessing  # B only
```

Use existing repository test tooling. The current regression command documented by the project is:

```bash
.venv/bin/python -W ignore -m unittest \
  tests.test_evaluate tests.test_ingestion tests.test_query_contracts \
  tests.test_column_inference tests.test_weak_readme \
  tests.test_make_submission tests.test_qualifier_guard tests.test_rule_wording
```

Add focused tests for the new contracts without weakening existing assertions. Public-fixture tests remain opt-in and skipped tests remain NOT RUN.

## 11. Schedule to the November 16 freeze

Planning assumption: one principal implementer, development GPU access obtained promptly. There are approximately **11 days and 15 hours** from the request time to the stated deadline. The estimates below are intentionally front-loaded with stop/go gates.

| Date, GMT+7 | Deliverable / decision |
|---|---|
| Nov 5 | Freeze current outputs; attribute failures by language/capability/metadata; request Docker guideline and Rule 6 clarification; book target hardware |
| Nov 6-7 | First run the zero-model pipeline offline in a container, then a candidate-model loading/structured-output/prefill/decode smoke test on target GPUs; build the typed-plan pilot and reuse applicable robustness tests |
| Nov 8 | **Architecture gate:** choose A only with useful recovery evidence; B requires a passed serving smoke test and correct compositional pilot. Otherwise retain the baseline and prioritize capabilities/packaging |
| Nov 8-10 | A integration and optional narrow SFT, or B's bounded operator compiler; train/synthetic tests only |
| Nov 11 | Freeze candidate behavior; evaluate baseline/A/B ablations and acceptance thresholds |
| Nov 12 | Full cold offline Docker run on target hardware, including model load and input isolation |
| Nov 13 | Repeatability, deadline, hard-stop, malformed-input and memory tests; fix only release blockers |
| Nov 14 | Freeze weights, dependencies, code, provenance, coverage and solution README |
| Nov 15 | Upload/download/checksum rehearsal; verify official submission instructions and archive deliverable |
| Nov 16 | Submission buffer; no speculative architecture or model changes |

If Docker guidance or target GPUs are delayed, reduce modeling ambition rather than postpone packaging. A strong model that cannot rerun offline is not a valid solution.

If the mandated base image is still unknown, a provisional development container can test feasibility but is not a compliant final image. Do not commit to B without the target-hardware smoke test by the architecture gate. A also cannot be promoted without serving evidence. The Nov 12 run is an end-to-end release rehearsal, not the first discovery that the selected model fails to load.

## 12. Research grounding and limits

### 12.1 Contest sources

- [Official DataMate portal](https://challenge.zalo.ai/portal/datamate).
- [Archived exact Rule JSON](../doc/zalo-ai-2026-datamate-rule.json): total model cap, release cutoff, offline runtime, no hardcoding, Rule 6 and rerun conditions.
- [Archived submission guidance](../doc/zalo-ai-2026-datamate-submissionGuidance.json): Nov 16 solution deadline, two solution submissions, private phase dates.
- [Archived evaluation](../doc/zalo-ai-2026-datamate-evaluationMetric.json): PA and final ties broken by rerun inference time, then earlier submission.
- [Source provenance](../doc/zalo-ai-2026-official-sources.md).
- [Train rulebook](../train/rulebook.md) and [public rulebook](../public_test/rulebook.md), plus all business READMEs in Appendix A.

The live Rule and Submission Guidance endpoints were successfully rechecked with public portal headers during this research; their reported update times match the archived snapshots. A plain web fetch first returned `Invalid Request`; that was not treated as valid rule content.

The Docker guideline and Data-tab-only logging specification were not independently obtained. Required logging fields remain grounded in the user's previously supplied contest instructions. Public leaderboard scores were not rechecked. The progress document's mention of "134 wrong answers" is explicitly unverified and is **not** used to choose an architecture. A verified aggregate score can inform a later release decision only when tied to the exact submission version and its answer/abstention count; PA alone does not identify C, W and A.

### 12.2 Relevant research, without benchmark transfer claims

- [PICARD](https://arxiv.org/abs/2109.05093): constrained decoding/incremental parsing supports rejecting invalid output forms. Syntax validity is not semantic correctness.
- [DIN-SQL](https://arxiv.org/abs/2304.11015): decomposition into schema linking and query-generation tasks motivates explicit intermediate steps. It does not establish that mandatory multi-pass generation is best under this contest budget.
- [DTS-SQL](https://arxiv.org/abs/2402.01117): decomposed training for smaller models motivates testing narrow plan learning rather than assuming only a large general agent can work.

Those studies concern other datasets. Their execution-accuracy numbers are not estimates of DataMate PA. No candidate LLM was downloaded, fine-tuned or benchmarked in this research session.

### 12.3 What would change the recommendation

Prefer **A** if:

- the main observed prospective errors are paraphrases/qualifiers over existing metrics;
- one compact planner recovers them with few new false acceptances;
- only several implementation days remain.

Prefer **B** if:

- prospective errors are primarily valid questions outside the metric registry;
- relevant source data is present but discarded/unexposed by canonical views;
- a bounded compiler passes source-grounded tests and remains comfortably within runtime.

Keep the **current implementation** if neither alternative shows net PA improvement or the offline release gate cannot be met.

**Bottom line:** A is the recommended first prototype after triage, not a guaranteed score improvement. B is the more ambitious path for schema/rule composition and a poor deadline choice unless its early pilot clears the gates. Build only one production route first; retain a viable zero-model release.

### 12.4 Architecture challenge and resolutions

A separate read-only challenge was performed after drafting the plan. No runtime changes or test answers resulted from that review.

| Challenge | Resolution |
|---|---|
| A may target wording when the real failures are missing capabilities | Accepted: make recommendation conditional on first-day attribution; do not infer question frequencies from coverage-table rows |
| Saturated train and a newly invented synthetic suite cannot establish a winner quickly | Accepted in part: reuse existing ablations and negative-test methods, but do not relabel contaminated ablations as prospective or assume removed documentation leaves the gold oracle valid |
| GPU/Docker feasibility was scheduled too late | Accepted: container and target-model smoke tests move before architecture selection |
| New runtime rules/mappings can create correlated failures | Accepted: immutable baseline, restricted rule slots/structures, override checks and explicit impact-radius analysis |
| Training only successful plans encourages unsafe over-acceptance | Accepted: add audited abstention/missing-data/contrastive targets; reject the suggestion that all historical qualifier mutations are valid negatives |
| B substantially overlaps the original plan | Accepted and clarified: identify the compiler contract/source-extension differences rather than claim a new conceptual invention |

Two proposed inferences were not adopted: the unverified public-error count is not current evidence, and sparse documentation does not prove that B can recover answers. Likewise, the 315.902-second run and historical roughly 110-second runs were not treated as a controlled performance comparison. Concurrency differs, and no hardware-normalized/per-stage attribution experiment was run.

## Appendix A. All business READMEs reviewed

These identifiers are evidence references only. They must never become inference routing rules. Public entries describe compatibility observations, not development recipes.

| Train business | Documentation observation |
|---|---|
| [biz_101](../train/data/biz_101/README.md) | Flat CSV lines, preconverted quantity, README unit conversions |
| [biz_102](../train/data/biz_102/README.md) | Monthly sheets, name joins, conversion workbook |
| [biz_103](../train/data/biz_103/README.md) | Monthly sales sheets and conversion workbook |
| [biz_104](../train/data/biz_104/README.md) | Name-based line export and conversion workbook |
| [biz_105](../train/data/biz_105/README.md) | Handwritten partitions, negative return lines, stock snapshot, purchases and prices |
| [biz_106](../train/data/biz_106/README.md) | Prose-only POS documentation, split detail files, payments and units |
| [biz_107](../train/data/biz_107/README.md) | Row-based ledger without order/customer IDs |
| [biz_108](../train/data/biz_108/README.md) | Relational exports, empty attributes, repeated/order-level measures |
| [biz_109](../train/data/biz_109/README.md) | Several logical tables packed into one workbook |
| [biz_110](../train/data/biz_110/README.md) | Order headers only, goods versus payable versus debt snapshot |
| [biz_111](../train/data/biz_111/README.md) | Relational CSV with payment events and methods |
| [biz_112](../train/data/biz_112/README.md) | Partitioned monthly line ledger and product catalog |
| [biz_113](../train/data/biz_113/README.md) | Row ledger without orders, construction units |
| [biz_114](../train/data/biz_114/README.md) | Flat construction sales with base quantities |
| [biz_115](../train/data/biz_115/README.md) | Separate delivery batches and due-date metadata |
| [biz_116](../train/data/biz_116/README.md) | Sales/catalog/delivery/due-date tables in one workbook |
| [biz_117](../train/data/biz_117/README.md) | Handwritten sales/returns, deliveries, imports and price history |
| [biz_118](../train/data/biz_118/README.md) | Monthly sales plus distinct delivery/date roles |
| [biz_119](../train/data/biz_119/README.md) | Pharmacy lots, allocations, positive return quantities and ingredients |
| [biz_120](../train/data/biz_120/README.md) | Pharmacy relational exports, catalog cost and empty fields |
| [biz_121](../train/data/biz_121/README.md) | Pharmacy sales with supporting tables packed as sheets |
| [biz_122](../train/data/biz_122/README.md) | Sales and lot-allocation partitions |
| [biz_123](../train/data/biz_123/README.md) | Seventeen-table SQLite POS with event stock and payment history |
| [biz_124](../train/data/biz_124/README.md) | Mixed POS, notebook and CSV sources, prose semantics |
| [biz_125](../train/data/biz_125/README.md) | Restaurant flat CSV; service-fee row is not a product |
| [biz_126](../train/data/biz_126/README.md) | Restaurant row ledger without order/customer IDs |
| [biz_127](../train/data/biz_127/README.md) | Restaurant relational CSV and independent receipts |
| [biz_128](../train/data/biz_128/README.md) | Restaurant relational exports plus tables/guest counts |
| [biz_129](../train/data/biz_129/README.md) | Renamed restaurant CSV fields and payment events |
| [biz_130](../train/data/biz_130/README.md) | Split restaurant sales ledger and product catalog |

| Public business | Documentation-only compatibility observation |
|---|---|
| [dev_001](../public_test/data/dev_001/README.md) | Sales ledger and catalog with README conversions |
| [dev_002](../public_test/data/dev_002/README.md) | Monthly files and inline product components |
| [dev_003](../public_test/data/dev_003/README.md) | Merged sales export, prose metadata, separate shift cash |
| [dev_004](../public_test/data/dev_004/README.md) | Handwritten sales and multiple auxiliary event sources |
| [dev_005](../public_test/data/dev_005/README.md) | Monthly pharmacy files and multi-step conversions |
| [dev_006](../public_test/data/dev_006/README.md) | Seventeen-table SQLite POS |
| [dev_007](../public_test/data/dev_007/README.md) | Denormalized CSV with repeated order/receipt amounts |
| [dev_008](../public_test/data/dev_008/README.md) | Split wide exports and prose debt evidence |
| [dev_009](../public_test/data/dev_009/README.md) | Header-only invoices |
| [dev_010](../public_test/data/dev_010/README.md) | Mother-and-baby relational CSV |
| [dev_011](../public_test/data/dev_011/README.md) | Phone sales, aliases, IMEIs and order adjustments |
| [dev_012](../public_test/data/dev_012/README.md) | Service visits, multi-action text and worker metadata |
| [dev_013](../public_test/data/dev_013/README.md) | Grouped headers, adjustments, member/catalog metadata |
| [dev_014](../public_test/data/dev_014/README.md) | Phone sales plus trade-in, debt, installment and warranty ledgers |
