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

1. Click **New QA run**. Enter Jira key **`DATA-1234`**, a built-in sample ticket.
   Upload `sample_data/customers.csv` and `sample_data/transactions.csv` as sources and
   `sample_data/customer_master_target.csv` as the target. Then click **Create run…**.
2. On the **Jira requirements** tab, review the extracted rules and open gaps. Reject the two *AI inferred* rows,
   or accept them.
3. On the **Transformation studio** tab, pick SQL, Python or Visual builder, then click **AI generate**,
   **Run preview** and **Approve**.
4. On **Test cases**, click **Generate actual-track tests**. On **Synthetic data**, click **Generate all**.
5. On **Approval center**, approve the remaining items. Then on **Execution monitor**, run **Track A** and **Track B**.
6. On **QA report**, click **Generate report files** and download Excel, HTML, JSON or Power BI.

The sample target contains four deliberate errors, and the run should find all of them: C102 net amount −100,
C105 name not trimmed or upper-cased, C108 missing, and C999 extra.

A second sample uses Jira key **`SALES-77`** with the files in `sample_data/sales/`. Upload `order_lines.json`
(alias `order_lines`) and `products.xlsx` (alias `products`) as sources and `daily_sales.csv` as the target.

---

## 4. Configuration (optional)

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
| `JIRA_MODE` | `mock` | `cloud` reads from your Jira Cloud site. Also set `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` (create a token at id.atlassian.com → Security → API tokens). |
| `LLM_MODE` | `mock` | `openai` uses a real model through any OpenAI-compatible API. Also set `LLM_API_KEY`, and optionally `LLM_BASE_URL` and `LLM_MODEL`. |
| `LLM_ALLOW_DATA_SAMPLES` | `false` | Keep `false` so record samples are never sent to an external model. |
| `DATABASE_URL` | *(empty → SQLite)* | Optional PostgreSQL, e.g. `postgresql+psycopg://user:password@localhost:5432/qa_agent`. Create the database first; tables are created automatically. |
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

## 5. Run the tests

```bat
cd backend
.\.venv\Scripts\python -m pytest -q
```
(On macOS/Linux, use `.venv/bin/python -m pytest -q`.) The result should be **102 passed**. Jira and the AI are mocked,
so no credentials or internet access are needed. In VS Code, you can also use the **Testing** panel (beaker icon) or
**Run Task… → Run tests**.

To run the suite against PostgreSQL instead of SQLite, point it at an **empty test database**. Its tables are cleared
between tests.
```bat
set "QA_TEST_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/qa_test"
.\.venv\Scripts\python -m pytest -q
```

---

## 6. Troubleshooting

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

---

## 7. What's in the folder

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
