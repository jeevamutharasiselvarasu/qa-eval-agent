# S2T QA Agent — Jira-driven Source-to-Target QA Validation

This web app validates data pipelines. It reads requirements from a Jira ticket and lets you upload source and
target datasets. It then builds the expected target from approved transformation logic (SQL, Python or a visual
builder) and runs deterministic checks. It also generates synthetic test data and produces Excel, HTML, JSON and
Power BI reports.

> This is a working **prototype**, not production software. For how it works internally and what is and isn't
> implemented, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Everything runs locally with **Python** and **Node.js**, and no Docker is needed. By default it uses a local SQLite
database plus built-in sample Jira tickets and an offline AI helper, so **no accounts or credentials are needed** to try it.

---

## 1. Install the prerequisites (once)

| Tool | Version | Download | Check it works |
|---|---|---|---|
| Python | **3.11, 3.12 or 3.13** (3.13 recommended) | python.org/downloads | `python --version` (Windows: `py --version`) |
| Node.js | **20 or newer** (LTS) | nodejs.org | `node --version` |
| VS Code (optional) | any recent | code.visualstudio.com | — |

**Windows:** in the Python installer, tick **"Add python.exe to PATH"**. Python 3.14 may not have ready-made
packages for every library yet, so stick to 3.11–3.13.

---

## 2. Run it — choose one option

The examples use **port 18080** for the backend. If a port is already in use, see [Change ports](#change-ports).

### Option A — VS Code (easiest)

1. Open VS Code and use **File → Open Folder…** to open the `s2t-qa-agent` folder.
   Install the recommended **Python** extension if VS Code asks.
2. Use **Terminal → Run Task… → Setup: install everything**. This is needed only once and takes a few minutes.
   It creates `backend\.venv` and installs the Python and Node packages.
   If VS Code asks for a Python interpreter, choose the one in `backend\.venv`.
3. Open **Run and Debug** (`Ctrl+Shift+D`), choose **Full stack (backend + UI)** and press **F5**.
4. Open **http://localhost:5173** in your browser.

Stop with the red ⏹ button. Breakpoints in the Python code work while running this way.

### Option B — Command line, two terminals (for development)

**Windows (Command Prompt or PowerShell)**

Terminal 1, the backend:
```bat
cd path\to\s2t-qa-agent\backend
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn app.main:app --port 18080
```

Terminal 2, the frontend (use the line for your shell):
```bat
cd path\to\s2t-qa-agent\frontend
npm install
```
```bat
:: Command Prompt
set "API_PORT=18080"
npm run dev
```
```powershell
# PowerShell
$env:API_PORT="18080"; npm run dev
```

**macOS / Linux**
```bash
# Terminal 1 – backend
cd s2t-qa-agent/backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --port 18080

# Terminal 2 – frontend
cd s2t-qa-agent/frontend
npm install
API_PORT=18080 npm run dev
```

Open **http://localhost:5173**. Stop each terminal with `Ctrl+C`.
You only need `venv`, `pip install` and `npm install` the first time.

### Option C — One process, no dev server (for just using the app)

Build the UI once and the backend serves it:
```bat
cd frontend
npm install
npm run build
cd ..\backend
.\.venv\Scripts\python -m uvicorn app.main:app --port 18080
```
(On macOS/Linux, use `cd ../backend` and `.venv/bin/python`.) Open **http://localhost:18080**.
Run `npm run build` again after you change any frontend code.

---

## 3. Try the demo (about 3 minutes)

1. Click **New QA run**. Enter Jira key **`DATA-1234`** (a built-in sample ticket) and click **Check**.
   Upload `sample_data/customers.csv` and `sample_data/transactions.csv` as sources and
   `sample_data/customer_master_target.csv` as the target, then click **Create run**.
2. The run opens on **Requirements**. This tab lists every **target column**: which source column(s) it comes from
   and the rule that builds it. It also shows the **table rules** (business key, join, row filter, value when a record
   has no match) and any **questions** found in the ticket.
   * If everything is clear, the SQL query and the test pack are generated straight away.
   * If something is missing or unclear, a banner lists what needs your input and no SQL is generated yet. In
     DATA-1234, Jira doesn't say what happens when DISCOUNT is null or when NET_AMOUNT would be negative.
   * To fix an item, click **Edit** on the column (or on the table rules) and write the rule as a SQL expression,
     for example `ROUND(SUM(transactions.AMOUNT - COALESCE(transactions.DISCOUNT, 0)), 2)`. Then mark each question as
     handled by your rule or accept it as a limitation.
   * **Accept as limitation** lets the SQL be generated, but tests that depend on that question stay BLOCKED.
   * Once nothing is open, the SQL is generated from the requirements and the test pack is built.
   * Your edits are kept if you re-retrieve the ticket from Jira.
3. **Transformation (SQL)** shows the generated query; you can still edit it by hand.
   **Test cases** shows the test pack in three groups:
   * **Acceptance criteria:** one group per criterion, with positive, negative and edge cases.
   * **Transformation logic:** checks derived from the SQL itself. UPPER/TRIM mean the target must be upper-case
     with no surrounding spaces, ROUND(x, 2) means at most 2 decimals, COALESCE means never null, and CASE means only
     the values the CASE can return. A LEFT JOIN must keep every base record, an INNER JOIN must keep only matched
     records, and GROUP BY means one row per group.
   * **General:** reconciliation and data-quality checks.

   Each test runs on either the uploaded data or a synthetic edge-case dataset (click its name to see the rows).
4. **Review & approve** has just two decisions: **Approval 1, the SQL transformation** and **Approval 2, the test pack**.
   The test pack covers requirements, data, target column rules, tests, synthetic data and settings. Changing any of these
   later marks the approval out of date.
5. Choose which tests to run with the checkboxes, per test or per group, or with **Select all** and **Select none**.
   Then click **Run selected and create report**. Only the selected tests run, and the report covers exactly those
   tests, stating "N of M test cases run". Choosing tests does not need a new approval, and a rebuilt pack keeps your
   choice. Results and report downloads (Excel, HTML, JSON, Power BI) are on the **Results** tab.

The sample target contains four deliberate errors, and the run should find all of them: C102 net amount −100,
C105 name not trimmed or upper-cased, C108 missing, and C999 extra.

**How the synthetic edge cases catch bugs.** For each rule, the app generates small datasets: null keys,
duplicates, one-to-many joins, rows with no match, an empty lookup, filter boundaries, zero values, rounding and
special characters. It then checks the approved transformation against an independent "rule oracle" built from the
mapping rules. As a result, a transformation that forgets a TRIM, ignores a discount or filters after a LEFT JOIN
fails its synthetic tests even if the uploaded target happens to look right.

A second sample uses Jira key **`SALES-77`** with the files in `sample_data/sales/`. Upload `order_lines.json`
(alias `order_lines`) and `products.xlsx` (alias `products`) as sources and `daily_sales.csv` as the target.

---

## 4. Connect your Jira (Settings page)

Open **Settings** in the left menu (admin role; in `dev` mode everyone is admin).

1. Choose **Jira Cloud (API)** and enter your site URL (e.g. `https://yourcompany.atlassian.net`), your Atlassian
   e-mail address and an **API token**. Create the token at id.atlassian.com → Security → API tokens.
2. Click **Test connection**. The app calls `/rest/api/3/myself` and shows the account it connected as.
3. Click **Save**. From then on, new runs read the ticket directly from Jira, and **Check ticket** on *New QA run*
   previews it first.

The token is **encrypted** (Fernet) in the app database and never sent back to the browser. The encryption key comes
from `QA_SECRET_KEY`, or else is generated once in `<storage dir>/.secret.key`. Keep that file, because without it a
saved token can't be decrypted and must be re-entered. Only `https` site URLs are accepted. Switch back to
**Sample tickets** at any time. Settings saved here take priority over the `JIRA_*` values in `.env`.

---

## 5. Configuration (optional)

Settings come from a **`.env` file in the project root** (the `s2t-qa-agent` folder), which the backend reads at start-up.
Without it, the defaults below apply.

Create it from the template:
```bat
:: Command Prompt
copy .env.example .env
```
```powershell
# PowerShell
Copy-Item .env.example .env
```
```bash
# macOS / Linux
cp .env.example .env
```

| Setting | Default | What it does |
|---|---|---|
| `JIRA_MODE` | `mock` | Fallback when nothing is saved on the Settings page. `cloud` reads from your Jira Cloud site. Also set `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` (create a token at id.atlassian.com → Security → API tokens). |
| `LLM_MODE` | `mock` | `openai` uses a real model through any OpenAI-compatible API. Also set `LLM_API_KEY`, and optionally `LLM_BASE_URL` and `LLM_MODEL`. |
| `LLM_ALLOW_DATA_SAMPLES` | `false` | Keep `false` so record samples are never sent to an external model. |
| `DATABASE_URL` | *(empty → SQLite)* | Optional PostgreSQL, e.g. `postgresql+psycopg://user:password@localhost:5432/qa_agent`. Create the database first; tables are created automatically. |
| `QA_SECRET_KEY` | *(auto-generated)* | Fernet key used to encrypt the Jira token saved on the Settings page. Set it to keep the same key across machines. |
| `QA_STORAGE_DIR` | `backend/var/storage` | Where uploads, generated datasets, evidence and reports are stored. |
| `QA_ENV` / `QA_AUTH_TOKENS` | `dev` / empty | In `dev` there is no login and everyone is an admin. See `.env.example` for setting up tokens and roles. |

Restart the backend after you change `.env`. Never commit `.env`: it can contain credentials.

### Change ports

| What | Default | How to change |
|---|---|---|
| Backend | 18080 in these examples | Use a different `--port` in the uvicorn command. In VS Code, change `s2tqa.apiPort` in `.vscode/settings.json`. |
| Frontend dev server | 5173 | Set `UI_PORT` (same way as `API_PORT`). In VS Code, change `s2tqa.uiPort`. |
| Frontend → backend link | 8000 | `API_PORT` must match the backend port. VS Code sets it for you. |

To check whether a port is free on Windows, run `netstat -ano | findstr :18080`. No output means it's free.

---

## 6. Run the tests

```bat
cd backend
.\.venv\Scripts\python -m pytest -q
```
(On macOS/Linux, use `.venv/bin/python -m pytest -q`.) The result should be **119 passed**. Jira and the AI are mocked,
so no credentials or internet access are needed. In VS Code, you can also use the **Testing** panel (beaker icon) or
**Run Task… → Run tests**.

To run the suite against PostgreSQL instead of SQLite, point it at an **empty test database**. Its tables are cleared
between tests.
```bat
set "QA_TEST_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/qa_test"
.\.venv\Scripts\python -m pytest -q
```

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `'Copy-Item' is not recognized…` | You're in Command Prompt. Use `copy .env.example .env` (Copy-Item is PowerShell). |
| `'py' is not recognized` | Use `python` instead of `py`, or reinstall Python with "Add to PATH" ticked. |
| `'uvicorn' is not recognized` | Run it through the venv's Python: `.\.venv\Scripts\python -m uvicorn …`. |
| pip: *No matching distribution* for pandas, numpy or duckdb | Your Python is too old (needs 3.11+) or too new (3.14). Install 3.13 and recreate `.venv`. |
| pip: *SSL: CERTIFICATE_VERIFY_FAILED* | Usually a company proxy. Add `--cert path\to\company-ca.pem` to the pip command, or ask IT for the pip settings. |
| *Address already in use* / port error | Pick another port; see [Change ports](#change-ports). |
| The UI loads but every action fails or the dashboard is empty | The backend isn't running, or `API_PORT` doesn't match the backend port. |
| PowerShell: *running scripts is disabled* | Use the `.\.venv\Scripts\python …` commands as shown here, which need no activation script. |
| Want to start over with empty data | Stop the backend and delete `backend\var\storage`. |
| *ModuleNotFoundError: cryptography* after updating | Re-run `pip install -r requirements.txt` (new dependency). The database upgrades itself on start-up. |
| Settings: *Jira rejected the credentials* | Check the e-mail address and token; the token must belong to that Atlassian account. |

---

## 8. What's in the folder

```
s2t-qa-agent/
├── backend/            Python API (FastAPI): Jira, requirements, profiling, mappings, transformation engine,
│   │                   synthetic data, validation, execution, reports
│   ├── app/            application code
│   ├── fixtures/jira/  sample Jira tickets used in mock mode (DATA-1234, SALES-77)
│   ├── tests/          automated tests (pytest)
│   └── requirements.txt
├── frontend/           React UI (Vite)
├── sample_data/        demo source and target files
├── docs/               ARCHITECTURE.md (design, safety model, status and gaps) and screenshots
├── .vscode/            ready-made VS Code tasks and run configurations
└── .env.example        configuration template
```

**Windows note:** user-written Python transformations run in a restricted sandbox. On Windows, the code checks,
file and network blocking and the time limit all apply, but **memory and CPU limits are only enforced on Linux** (macOS applies them partially).
