# Architecture, safety model and status

This document covers how the application works internally and what is and isn't implemented.
For installation and running, see the [README](../README.md).

> **AI proposes; deterministic execution proves.** No AI output sets PASS/FAIL, becomes an approved rule
> without a human decision, or produces expected values. Expected targets are always the output of the
> approved transformation running in the shared engine.

## Architecture

```
React UI (Vite)  ──►  FastAPI (/api)  ──►  services (deterministic)          AI (separate)
                                           ├─ JiraConnector (mock | Cloud)   ├─ llm.MockLLM (offline)
                                           ├─ RequirementIntelligence        └─ llm.OpenAILLM (any OpenAI-compatible)
                                           ├─ Dataset upload/ParserRegistry/Profiler (DuckDB, PyArrow)
                                           ├─ MappingService
                                           ├─ TransformationStudio ─► engine.execute()  ◄─ ONE engine for both tracks
                                           │      SQL (DuckDB) · PYTHON (isolated worker) · VISUAL (→ SQL)
                                           ├─ TestCaseGenerator · logic tests
                                           ├─ ApprovalService (content-hash bound)
                                           ├─ ExecutionManager (idempotent, checkpointed, threaded)
                                           ├─ ValidationEngine (all PASS/FAIL)
                                           ├─ FailureAnalysis (after execution; advisory only)
                                           └─ ReportService (xlsx, html, json, Power BI CSV star schema)
SQLAlchemy metadata DB (SQLite default / PostgreSQL)   +   run-scoped file storage (originals read-only, Parquet)
```

Key files: `backend/app/services/transform/engine.py` (common contract), `validation.py`, `execution.py`,
`approvals.py`, `requirements.py`, `rule_model.py`, `testgen.py`, `logic_tests.py`. The API is split into one router per area in `backend/app/api/routers/`
(overview, runs, requirements, datasets, transformations, testing, executions, reports); `api/deps.py` holds the shared
dependencies (`DB`, role aliases), the error mapping and the row serialiser. Sources and targets can be read from Azure Blob / ADLS Gen2
paths (`services/blob.py`): the path is validated (Azure hosts only, https/abfss/wasbs, no SAS tokens, optional account
allow-list), read with `DefaultAzureCredential`, a folder of part files is combined into one Parquet file, and the
result goes through the same upload pipeline (fingerprint, parsing, profiling); `Dataset.source_uri` records the path so
the dataset can be re-read. The client report is rendered by
`services/report_render.py`: HTML from the Jinja template `app/templates/report.html.j2` (autoescaped, print-ready, with
"Print / save as PDF") and a formatted Excel workbook (summary sheet, one Excel Table per section, fit-to-width printing).

### Common transformation contract
`TransformationDefinition(transformation_id, qa_run_id, mode, inputs{alias→parquet}, code | visual_spec, version,
content_hash, …)` → `TransformationResult(execution_id, status, output_path, output_schema, row_count,
duration_ms, logs, warnings, errors[line, kind], code_version, input_fingerprints)`.
Previews and the expected target both call `engine.execute()`.

**Python contract:** `def transform(sources: dict[str, pandas.DataFrame]) -> pandas.DataFrame`. Each source alias is a key.
Allowed imports are `pandas, numpy, math, datetime, decimal, re, statistics, collections, itertools, functools`
(`operator` and `string` are excluded because they allow dynamic attribute access; `str.format` is limited to literal
templates without attribute or index fields). `print()` output appears in the logs.

### Safety model for user-written and AI-written code
* **SQL:** parsed with sqlglot. Exactly one read-only SELECT or set operation (CTEs allowed). DDL, DML, COPY, ATTACH,
  PRAGMA, SET, INSTALL, LOAD and SELECT INTO are rejected, as are table functions (`read_csv`, `glob`, …) and
  file, environment or system functions. Only the run's dataset aliases can be referenced. At runtime a fresh
  in-memory DuckDB loads the inputs, then sets `enable_external_access=false` and `lock_configuration=true`. Memory
  and thread limits apply, and a timeout calls `interrupt()`.
* **Python:** an AST policy blocks non-allowlisted imports, eval/exec/open/getattr/…, dunder and private attribute
  access, and pandas/numpy IO methods. Code runs in a separate `python -I -S` process with an empty environment,
  rlimits (memory, CPU, file size, open files), a wall-clock kill, restricted builtins and a guarded `__import__`.
  A **PEP 578 audit hook** blocks sockets, subprocesses, os.system/exec/fork, ctypes and file opens outside
  library paths while user code runs. A test confirms it catches `pd.io.common.get_handle('/etc/hostname')`, which
  gets past the AST policy.
* **Visual:** a JSON definition is compiled with an allowlist of functions, operators and cast types, quoted
  identifiers and escaped literals, then passed through the same SQL validator.
* Code that fails validation cannot be previewed or approved. Execution requires a current approval.

### Approvals and invalidation
Each artifact (REQUIREMENTS, DATASETS, MAPPINGS, TRANSFORMATION, TEST_CASES, EXECUTION_SETTINGS) is
hashed. An approval is valid only while the current hash equals the approved hash. Any edit, such as a code
change, mapping edit, gap clarification or tolerance tweak, turns the approval **STALE**, and execution is refused
until someone re-approves. Every decision is stored with reviewer, timestamp, version and reason.

In the UI these are grouped into **two decisions**:
* **Approval 1** is the TRANSFORMATION.
* **Approval 2**, the *test pack*, approves the other six artifacts in one step. It is accepted only when the
  transformation is approved and the pack is current: the pack's basis hash covers requirements, datasets, mappings
  and the transformation version.

AI-suggested requirements and proposed mappings no longer block approval. Anything nobody accepted is simply left
out of the pack, and the review screen lists it.

### Requirements per target column, and the SQL gate
`services/target_spec.py` presents the requirements as a target specification:
* **Per target column:** the source columns, the rule (the default for unmatched records is shown separately), its
  origin (Jira, edited or suggested) and its status: CLEAR, MISSING, SUGGESTED, QUESTION or INVALID.
* **Table rules:** key, joins, filters and defaults.

**Open items** are anything that is not CLEAR, a missing key, several sources with no join, or an unanswered question.

**SQL gate.** `sync_sql` generates the SQL only when there are no open items. It runs after every edit, Jira
re-retrieval, profiling or dataset change, and the test pack is built after it.
* `/transformations/generate` and the test-pack build refuse to run while items are open (an instruction-based edit
  of existing code is still allowed).
* Generated versions store `requirements_basis`. A requirements-generated version is regenerated when the rules
  change.
* A hand-written or hand-edited version is never overwritten; it is flagged `MANUAL_OUTDATED` and can be regenerated
  on request.

**In-app edits.**
* Every edit is stored as a USER_CONFIRMED requirement. `apply_overrides` marks the Jira rule for the same column or
  table rule as SUPERSEDED, and re-applies this after re-analysis.
* Questions that an edit answers structurally (no key, no join, precision, conflict) are closed automatically.
* An edit keeps the AC id of the rule it replaces, so tests stay grouped by acceptance criterion.

**ACCEPTED_LIMITATION** unblocks SQL generation, but dependent tests stay BLOCKED. Only CLARIFIED makes
them run against the rule.

**UI changes.** The Mappings tab was merged into Requirements, and the Data tab now lists only the files (column
profiling still runs in the background for the null checks).

### Test pack (positive / negative / edge)
`services/testpack.py` builds the pack: SQL from the requirements (if none) → `testgen.generate_test_pack`.
There is no synthetic data. Every test checks the **uploaded target** (the developer's output) against the expected
target the approved reference SQL builds from the uploaded sources.
* Tests are grouped by acceptance criterion (`TC-AC<n>-NN`), transformation logic (`TC-TL-NN`) and general
  checks (`TC-GEN-NN`). Each is typed POSITIVE, NEGATIVE, EDGE or GENERAL.
* POSITIVE cases check the whole target.
* NEGATIVE and EDGE cases carry a **scope**: a query over the `src_<alias>` views that returns the keys of the uploaded
  records hitting an edge case. Scopes cover no join match, several matches, rows failing or all failing the filter,
  null, zero or negative inputs, inputs with more decimals than the result, unusual text, and duplicate or null source
  keys (`rule_model.py` describes base, join and filter).
* `ValidationContext.apply_scope` redefines the listed views to those keys before the check runs. An empty scope
  gives SKIPPED ("not in this data"); SKIPPED results do not lower an acceptance criterion's coverage.
* Scopes need one business key shared by the base source and the target. With a composite key, only the
  whole-target cases are generated.
* A test whose expectation depends on an unanswered question is BLOCKED with the gap ID.
* An acceptance criterion that cannot be parsed into tests gets a BLOCKED manual test.
* `POST /runs/{id}/run-validation` runs the selected tests and generates the report.

### Transformation-logic tests and test selection
`services/logic_tests.py` parses the current transformation SQL with sqlglot. Visual mode is parsed from its compiled
SQL; Python mode gets no logic tests, and the test-pack status says why. It generates group TL tests
(basis TRANSFORMATION_LOGIC), each with a rationale quoting the SQL fragment:
* **VALUE_PROPERTY checks**, from the guarantees of the outermost functions of each output column: UPPER, LOWER,
  TRIM, ROUND(n), COALESCE with a literal fallback, COUNT, ABS, LEFT/SUBSTRING length, and CASE allowed values.
* **COLUMN_MATCH** for output columns that no acceptance criterion covers.
* **Joins:** KEY_COVERAGE for a LEFT JOIN; KEY_SUBSET for an INNER JOIN or a base-table filter.
* **GROUP BY:** KEY_UNIQUE (one row per group).

**Selection.** `TestCase.selected` is not part of the TEST_CASES approval hash, so choosing tests needs no
re-approval.
* Selection is preserved across pack rebuilds, because test IDs are stable.
* `PUT /runs/{id}/test-cases/selection` takes `only`, `select` and `deselect`.
* Each execution snapshots `selected_test_ids` and runs only those; the idempotency key includes the selection.
* `run-validation` starts only the tracks that have selected tests, and the report covers exactly those results
  (`summary.selection`).

### Jira connection settings
The `AppSetting` table stores the Jira mode, URL and e-mail address. The API token is Fernet-encrypted (`core/secrets.py`).
* The key comes from `QA_SECRET_KEY`, otherwise from `<storage>/.secret.key` (mode 0600).
* The API never returns the token, only `token_set`.
* The URL must be https with no path. Loopback, link-local, multicast and unspecified hosts are refused.
* `POST /settings/jira/test` calls `/rest/api/3/myself`. Saved settings take priority over the environment variables.
* Tested against simulated Jira responses only, not a live site.

### Execution, recovery and audit
* An idempotency key is built from the code hash, dataset fingerprints, approval hashes and settings. An identical
  completed execution is reused unless *force* is set. A second concurrent execution on the same track is refused.
* Results are committed one at a time as checkpoints. On restart, RUNNING executions become **INTERRUPTED**.
  *Retry* re-runs only the ERROR and NOT_EXECUTED results, and only if the inputs are unchanged.
* Technical problems are recorded as **ERROR**, never as FAIL. Unknown behaviour is **BLOCKED**. Runtime guards
  block a test only when an open gap actually affects the data (for example, nulls are present).
* The audit log records state changes, uploads (with fingerprints), versions, approvals, executions and AI calls
  (including whether data samples were sent).

### Workflow states
`RUN_CREATED → JIRA_RETRIEVAL_IN_PROGRESS → JIRA_RETRIEVED → REQUIREMENTS_ANALYZED → DATASETS_UPLOADED →
DATASETS_PROFILED → MAPPING_GENERATED → TRANSFORMATION_DESIGNED → TEST_CASES_GENERATED →
AWAITING_APPROVAL → APPROVED → ACTUAL_TRANSFORMATION_IN_PROGRESS →
VALIDATION_IN_PROGRESS → VALIDATION_COMPLETED → FAILURE_ANALYSIS_COMPLETED → REPORT_GENERATED`.
Exception states (`JIRA_RETRIEVAL_FAILED`, `REQUIREMENT_GAP`, `DATASET_PARSE_FAILED`, `PROFILE_FAILED`,
`MAPPING_REVIEW_REQUIRED`, `APPROVAL_REJECTED`, `TRANSFORMATION_FAILED`, `EXECUTION_FAILED`, `PARTIAL_SUCCESS`,
`CANCELLED`) are tracked separately so they don't overwrite progress.

### API (selection)
`POST /api/runs`, `POST /api/runs/{id}/jira/retrieve`, `GET /api/runs/{id}/requirements`,
`POST /api/runs/{id}/requirements/clarifications`, `POST /api/runs/{id}/datasets/sources|target`, `POST /api/runs/{id}/profile`,
`POST /api/runs/{id}/mappings/generate`, `PATCH /api/runs/{id}/mappings/{mid}`, `POST /api/runs/{id}/transformations[/generate|/validate|/explain|/fix|/restore]`,
`POST /api/runs/{id}/transformations/{vid}/preview|approve`, `POST /api/runs/{id}/test-pack[/approve]`, `GET /api/runs/{id}/test-pack`, `POST /api/runs/{id}/run-validation`,
`GET|PUT /api/settings/jira`, `POST /api/settings/jira/test`, `POST /api/jira/preview`, `POST /api/runs/{id}/approvals`, `POST /api/runs/{id}/execute/actual`,
`GET /api/executions/{eid}`, `POST /api/executions/{eid}/retry|cancel`, `GET /api/compare?a=&b=`,
`POST /api/runs/{id}/reports`, `GET /api/reports/{rid}/download/{json|html|xlsx|powerbi}`, `GET /api/runs/{id}/traceability|audit`.
Interactive docs are at `/docs`.

---

## Status and gaps

Legend: ✅ implemented and covered by automated tests · 🟡 implemented, partially tested or simplified · ❌ not implemented

| Area | Status | Notes |
|---|---|---|
| Jira Cloud retrieval (issue, ADF description, comments, text attachments, links, custom AC fields) | 🟡 | Tested against mocked HTTP (success, 401/403/404/429/500, timeout). **Not tested against a live Jira instance.** Binary attachments (PDF, DOCX, images) are skipped with a reason. |
| Mock Jira fixtures | ✅ | `DATA-1234` (full scenario) and `DATA-9999` (permission error), clearly labelled MOCK in the UI and data. |
| Requirement extraction with provenance and evidence | ✅ | Pattern-based extraction of typed rules (single and composite keys, unique, not-null, derived, direct, filter, join, defaults, rounding) plus attachment mapping sheets. An acceptance criterion that yields no typed rule now raises an `UNPARSED_RULE` gap, so it can't be dropped silently, and dependent column checks are blocked until it's resolved. |
| Gap detection (questions, missing key, precision, conflicts, join type, profile nulls) | ✅ | |
| Prompt-injection defence | 🟡 | Pattern detector plus "data, not instructions" prompting plus schema-validated LLM output. Heuristic, not exhaustive. |
| Upload of multiple sources and one target, fingerprints, read-only originals | ✅ | |
| Parsers: CSV, TXT/TSV, XLSX (sheet selection), JSON/NDJSON, Parquet | ✅ | Ragged CSVs (inconsistent field counts) are rejected with line numbers instead of being read as a single column. |
| Profiling (nulls, blanks, distinct, duplicates, keys, min/max, numeric stats, format issues, masking) | ✅ | |
| Large-file handling | 🟡 | DuckDB/Parquet streaming, with no pandas full-load in the API path. **No large-file performance tests.** Python mode loads inputs into pandas by design. |
| Mapping proposals and editing (accept, edit, reject, regenerate, add) | ✅ | |
| SQL editor (highlighting, schema autocomplete, line-numbered errors, preview limits, versions, restore, diff) | ✅ | DuckDB is the only dialect. Formatting is not implemented. |
| Python editor and isolated worker | ✅ / 🟡 | Process isolation, rlimits and an audit hook. **No container or network-namespace isolation per execution.** For production, run the worker in a network-less sandbox (for example gVisor or Firecracker). |
| Visual builder (joins, pre/post filters, string, arithmetic, aggregate, CASE, cast, round, defaults, dedupe, raw JSON) | ✅ | Lookups are modelled as joins. Date-format helpers exist only in the JSON or advanced mode. |
| SQL / Python / Visual equivalence | ✅ | Tested on the sample scenario. |
| AI generate, explain, fix, modify | 🟡 | The mock provider is a deterministic compiler from accepted mappings, plus sqlglot-based explanations and fuzzy column fixes. Instruction-based edits need `LLM_MODE=openai`, which is **not tested against a live model**. |
| Test case generation and AC coverage report | ✅ | |
| Edge-case checks on uploaded records (scoped, SKIPPED when absent) | ✅ | Single business key required for scoping. |
| Human approval with hash-based invalidation | ✅ | |
| Deterministic validation (schema, count, keys, nulls, dupes, missing/extra, column recon with tolerance, null-safe, case/trim, temporal, composite keys, coverage, defaults, snapshot, predicates) | ✅ | |
| Execution manager (threads, idempotency, checkpoints, retry, restart recovery, cancel) | ✅ | Runs in-process threads. Use a real job queue for multi-instance deployments. |
| AI failure analysis (facts, likely explanations, hypotheses kept separate; cannot change status) | ✅ (mock) | |
| Reports: Excel, HTML, JSON, Power BI CSV star schema, traceability, run compare | ✅ | |
| RBAC and project boundaries | 🟡 | Static bearer-token map with roles. **No SSO/OIDC, user management or token rotation.** In dev with no tokens set, every request runs as an admin. |
| PostgreSQL | ✅ / 🟡 | Optional via `DATABASE_URL`. The full suite passed against PostgreSQL 16 on 2026-11-04; run it yourself with `QA_TEST_DATABASE_URL`. Tables are created with `create_all`. **No Alembic migrations yet.** |
| Encryption at rest, malware scanning, retention jobs | ❌ | `retention_days` is modelled but not enforced. Use encrypted volumes and the platform's scanner. |
| Deployment packaging | ❌ | Runs locally only (Python + Node). No container images or cloud deployment configuration are included. |
| Frontend automated tests | 🟡 | One scripted Playwright walkthrough of the full flow was run manually. It is not part of the suite. |
| Flow 2 (storage discovery, pipeline triggering) | ❌ | Out of scope by design. The services are adapter-based so they can be reused later. |

### Testing round 2 (second scenario, sandbox attacks, robustness)
A second, independent scenario (`SALES-77`: JSON and XLSX sources, INNER lookup join, base-side filter, composite key,
Python transformation approved and executed) plus 27 sandbox attack probes and about 30 API and robustness probes turned up
these defects, which are now fixed and covered by `tests/test_round2_regressions.py`:

* Composite business keys ("A and B together form the business key") weren't recognised, so every record-level test was blocked.
* A filter on a multi-word dataset ("Only order lines with …") wasn't extracted, so the AC was dropped silently. Unparsed ACs now raise a gap.
* Grouping keys named in a rule ("… per SALE_DATE, PRODUCT_CODE") weren't mapped, so the AI draft aggregated at the wrong grain.
* Python sandbox: `operator.attrgetter`, `string.Formatter` and `str.format` field traversal allowed dynamic dunder
  access past the AST policy. The audit hook still blocked any file, network or process access, so no data leaked.
* Ragged CSV files were accepted as a single garbage column; very long filenames lost their extension; unknown `/api/*`
  paths returned the UI page instead of a JSON 404.


### Honest limitations to know
* Edge cases are only tested when the uploaded data contains them; otherwise they are SKIPPED. The expected values
  come from the approved reference SQL, so they are only as correct as the requirements it was generated from.
* A filter on the right side of a LEFT JOIN is placed **before** the join in AI drafts, and this is recorded as an
  explicit assumption in the code comment and version note for review.
