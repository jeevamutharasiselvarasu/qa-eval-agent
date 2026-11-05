# Jira-Driven AI Source-to-Target QA Validation Agent — Phase 1 / Flow 1

A working vertical slice of the specification: Jira ticket + manually uploaded source and target datasets →
requirements and gaps → mappings → SQL / Python / Visual transformation → test cases and synthetic data →
human approval → deterministic validation (actual and synthetic tracks) → AI failure analysis → reports.

> **AI proposes; deterministic execution proves.** No AI output sets PASS/FAIL, becomes an approved rule
> without a human decision, or produces expected values. Expected targets are always the output of the
> approved transformation running in the shared engine.

This is a **prototype, not production-ready**. See [Status and gaps](#status-and-gaps).

---

## Quick start (local, no Docker)

```bash
# backend (Python 3.12+; tested on 3.13)
cd backend
pip install -r requirements.txt
QA_ENV=dev uvicorn app.main:app --port 8000          # SQLite + ./var/storage by default

# frontend (Node 20+), in a second terminal
cd frontend
npm install
npm run dev                                           # http://localhost:5173 (proxies /api to :8000)
```

Or build the UI once (`npm run build`); the API then serves it at http://localhost:8000.

### Demo walkthrough (about 3 minutes)

1. **New QA run** → key `DATA-1234` (a clearly labelled mock Jira fixture). Upload `sample_data/customers.csv`
   and `sample_data/transactions.csv` as sources and `sample_data/customer_master_target.csv` as the target.
2. **Jira requirements**: 7 acceptance criteria are turned into typed rules with evidence. There are 2 open gaps
   (from unanswered comment questions about null DISCOUNT and negative NET_AMOUNT). A prompt-injection comment
   is flagged and ignored. Reject the 2 AI-inferred proposals, or accept them.
3. **Transformation studio**: click *AI generate* in SQL, Python or Visual mode, then *Run preview*, then *Approve*.
4. **Test cases**: *Generate actual-track tests*. **Synthetic data**: *Generate all* (needs the approved transformation).
5. **Approval center**: approve the remaining artifacts. **Execution monitor**: run Track A and Track B.
6. **QA report**: generate the Excel, HTML, JSON and Power BI files.

The sample target has four planted defects, and the run finds all of them:

| Defect | Detected by |
|---|---|
| C102 `NET_AMOUNT` 1700 vs expected 1800 (difference −100) | COLUMN_MATCH (AC5) |
| C105 `CUSTOMER_NAME` not trimmed or upper-cased | COLUMN_MATCH (AC2) |
| C108 missing from the target | RECON_MISSING + KEY_COVERAGE (AC7) |
| C999 extra record in the target | RECON_EXTRA |

Synthetic testing also finds a real defect in the AI-drafted transformation. A NULL `CUSTOMER_ID` in the source
flows through to the target, which violates AC1 ("cannot be null"). The NEGATIVE_RESULT and NULL_VALUES scenarios
stay **BLOCKED** until their gaps are clarified.

### Docker Compose (PostgreSQL)

```bash
cp .env.example .env      # edit as needed; never commit .env
docker compose up --build # http://localhost:8000
```
*The Dockerfile and Compose file are provided but were **not** built or run in the development environment
(no Docker daemon). The automated suite ran against SQLite.*

### Tests

```bash
cd backend && python -m pytest -q        # 102 tests on SQLite; Jira and LLM are mocked, no credentials needed
```

**Against PostgreSQL in a separate Docker stack** (`docker-compose.test.yml`): a throwaway `postgres:16` container
(data in tmpfs) plus a test-runner container on an internal network with no internet access. It is fully separate
from the app's `docker-compose.yml`.

```bash
scripts/run-tests-docker.sh              # Linux/macOS
powershell -File scripts/run-tests-docker.ps1   # Windows (Docker Desktop)
```
Results go to `test-results/junit.xml`, and the stack is removed afterwards. Set `QA_TEST_DATABASE_URL` to run the suite
against any other PostgreSQL database; tables are truncated between tests.
If Docker Hub is unreachable, `docker/local/` builds equivalent images from an Ubuntu mirror.

Verified on 2026-11-04: **102 passed** in the Docker stack against PostgreSQL 16.2 (images built via `docker/local/`,
because Docker Hub is blocked in the development environment).

---

## Architecture

```
React UI (Vite)  ──►  FastAPI (/api)  ──►  services (deterministic)          AI (separate)
                                           ├─ JiraConnector (mock | Cloud)   ├─ llm.MockLLM (offline)
                                           ├─ RequirementIntelligence        └─ llm.OpenAILLM (any OpenAI-compatible)
                                           ├─ Dataset upload/ParserRegistry/Profiler (DuckDB, PyArrow)
                                           ├─ MappingService
                                           ├─ TransformationStudio ─► engine.execute()  ◄─ ONE engine for both tracks
                                           │      SQL (DuckDB) · PYTHON (isolated worker) · VISUAL (→ SQL)
                                           ├─ TestCaseGenerator · SyntheticDataGenerator
                                           ├─ ApprovalService (content-hash bound)
                                           ├─ ExecutionManager (idempotent, checkpointed, threaded)
                                           ├─ ValidationEngine (all PASS/FAIL)
                                           ├─ FailureAnalysis (after execution; advisory only)
                                           └─ ReportService (xlsx, html, json, Power BI CSV star schema)
SQLAlchemy metadata DB (SQLite default / PostgreSQL)   +   run-scoped file storage (originals read-only, Parquet)
```

Key files: `backend/app/services/transform/engine.py` (common contract), `validation.py`, `execution.py`,
`approvals.py`, `requirements.py`, `synthetic.py`, `testgen.py`. The API is in `backend/app/api/routes.py`.

### Common transformation contract
`TransformationDefinition(transformation_id, qa_run_id, mode, inputs{alias→parquet}, code | visual_spec, version,
content_hash, …)` → `TransformationResult(execution_id, status, output_path, output_schema, row_count,
duration_ms, logs, warnings, errors[line, kind], code_version, input_fingerprints)`.
Previews, the actual expected target and synthetic expected targets all call `engine.execute()`.

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
Each artifact (REQUIREMENTS, DATASETS, MAPPINGS, TRANSFORMATION, TEST_CASES, SYNTHETIC_DATA, EXECUTION_SETTINGS) is
hashed. An approval is valid only while the current hash equals the approved hash. Any edit, such as a code
change, mapping edit, gap clarification or tolerance tweak, turns the approval **STALE**, and execution is refused
until someone re-approves. Every decision is stored with reviewer, timestamp, version and reason.

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
DATASETS_PROFILED → MAPPING_GENERATED → TRANSFORMATION_DESIGNED → TEST_CASES_GENERATED → SYNTHETIC_DATA_GENERATED →
AWAITING_APPROVAL → APPROVED → ACTUAL_TRANSFORMATION_IN_PROGRESS / SYNTHETIC_EXECUTION_IN_PROGRESS →
VALIDATION_IN_PROGRESS → VALIDATION_COMPLETED → FAILURE_ANALYSIS_COMPLETED → REPORT_GENERATED`.
Exception states (`JIRA_RETRIEVAL_FAILED`, `REQUIREMENT_GAP`, `DATASET_PARSE_FAILED`, `PROFILE_FAILED`,
`MAPPING_REVIEW_REQUIRED`, `APPROVAL_REJECTED`, `TRANSFORMATION_FAILED`, `EXECUTION_FAILED`, `PARTIAL_SUCCESS`,
`CANCELLED`) are tracked separately so they don't overwrite progress.

### API (selection)
`POST /api/runs`, `POST /api/runs/{id}/jira/retrieve`, `GET /api/runs/{id}/requirements`,
`POST /api/runs/{id}/requirements/clarifications`, `POST /api/runs/{id}/datasets/sources|target`, `POST /api/runs/{id}/profile`,
`POST /api/runs/{id}/mappings/generate`, `PATCH /api/runs/{id}/mappings/{mid}`, `POST /api/runs/{id}/transformations[/generate|/validate|/explain|/fix|/restore]`,
`POST /api/runs/{id}/transformations/{vid}/preview|approve`, `POST /api/runs/{id}/synthetic-data/generate`,
`POST /api/runs/{id}/test-cases/generate`, `POST /api/runs/{id}/approvals`, `POST /api/runs/{id}/execute/actual|synthetic`,
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
| Python editor and isolated worker | ✅ / 🟡 | Process isolation, rlimits and an audit hook. **No container or network-namespace isolation per execution.** For production, run the worker in a network-less sandbox (gVisor or Firecracker). |
| Visual builder (joins, pre/post filters, string, arithmetic, aggregate, CASE, cast, round, defaults, dedupe, raw JSON) | ✅ | Lookups are modelled as joins. Date-format helpers exist only in the JSON or advanced mode. |
| SQL / Python / Visual equivalence | ✅ | Tested on the sample scenario. |
| AI generate, explain, fix, modify | 🟡 | The mock provider is a deterministic compiler from accepted mappings, plus sqlglot-based explanations and fuzzy column fixes. Instruction-based edits need `LLM_MODE=openai`, which is **not tested against a live model**. |
| Test case generation and AC coverage report | ✅ | |
| Synthetic data (12 scenario families, seeded, isolated, expected via engine, BLOCKED on gaps) | ✅ | Generic over the requirement types listed above. Domain values come from the real data's categories (values only). |
| Human approval with hash-based invalidation | ✅ | |
| Deterministic validation (schema, count, keys, nulls, dupes, missing/extra, column recon with tolerance, null-safe, case/trim, temporal, composite keys, coverage, defaults, snapshot, predicates) | ✅ | |
| Execution manager (threads, idempotency, checkpoints, retry, restart recovery, cancel) | ✅ | Runs in-process threads. Use a real job queue for multi-instance deployments. |
| AI failure analysis (facts, likely explanations, hypotheses kept separate; cannot change status) | ✅ (mock) | |
| Reports: Excel, HTML, JSON, Power BI CSV star schema, traceability, run compare | ✅ | |
| RBAC and project boundaries | 🟡 | Static bearer-token map with roles. **No SSO/OIDC, user management or token rotation.** In dev with no tokens set, every request runs as an admin. |
| PostgreSQL | ✅ / 🟡 | Full suite passes against PostgreSQL 16 in the Docker test stack. Tables are created with `create_all`. **No Alembic migrations yet.** |
| Encryption at rest, malware scanning, retention jobs | ❌ | `retention_days` is modelled but not enforced. Use encrypted volumes and the platform's scanner. |
| Docker / cloud deployment | 🟡 | Dockerfile and Compose are provided but were **not built here**. No cloud IaC. |
| Frontend automated tests | 🟡 | One scripted Playwright walkthrough of the full flow was run manually. It is not part of the suite. |
| Flow 2 (storage discovery, pipeline triggering) | ❌ | Out of scope by design. The services are adapter-based so they can be reused later. |

### Testing round 2 (second scenario, sandbox attacks, robustness)
A second, independent scenario (`SALES-77`: JSON and XLSX sources, INNER lookup join, base-side filter, composite key,
Python transformation approved and executed) plus 27 sandbox attack probes and about 30 API and robustness probes turned up
these defects, which are now fixed and covered by `tests/test_round2_regressions.py`:

* Composite business keys ("A and B together form the business key") weren't recognised, so every record-level test was blocked.
* A filter on a multi-word dataset ("Only order lines with …") wasn't extracted, so the AC was dropped silently. Unparsed ACs now raise a gap.
* Grouping keys named in a rule ("… per SALE_DATE, PRODUCT_CODE") weren't mapped, so the AI draft aggregated at the wrong grain.
* Synthetic data assumed the business key was also the join key and a string, which broke on date keys and lookup joins.
  Base-side filters and multiplicative precision scenarios are now generated correctly.
* Python sandbox: `operator.attrgetter`, `string.Formatter` and `str.format` field traversal allowed dynamic dunder
  access past the AST policy. The audit hook still blocked any file, network or process access, so no data leaked.
* Ragged CSV files were accepted as a single garbage column; very long filenames lost their extension; unknown `/api/*`
  paths returned the UI page instead of a JSON 404.


### Honest limitations to know
* Synthetic Track B compares a re-execution against the **approved expected snapshot** and applies rule-derived
  checks (unique and not-null keys, join coverage, default values). The expected values are what the approved
  transformation produces, so reviewers must check them by hand in the Synthetic Data Studio (for example the
  DECIMAL_PRECISION scenario: half-up of 10.005).
* A filter on the right side of a LEFT JOIN is placed **before** the join in AI drafts, and this is recorded as an
  explicit assumption in the code comment and version note for review.
