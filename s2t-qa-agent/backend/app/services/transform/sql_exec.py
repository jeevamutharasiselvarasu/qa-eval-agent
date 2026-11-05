"""SQLExecutionService: DuckDB SQL with a parser-based statement allowlist and runtime lockdown.

Layers of defence:
  1. sqlglot parse: exactly one read-only query (SELECT / set operation, optional CTEs).
  2. Reject DDL/DML/COPY/ATTACH/PRAGMA/SET/INSTALL/LOAD/SELECT INTO and any file/table functions.
  3. Only run-registered dataset aliases (and CTE names) may be referenced; no schema/catalog qualifiers.
  4. Runtime: fresh in-memory DuckDB, inputs loaded as tables, then external access disabled and the
     configuration locked; memory/thread limits; wall-clock timeout via interrupt().
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ...config import get_settings
from .contract import ExecError

FORBIDDEN_NODES = {"Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Copy", "Pragma", "Set",
                   "Use", "Transaction", "Commit", "Rollback", "Attach", "Detach", "Merge", "Into", "LoadData",
                   "Install", "Export", "Grant", "Revoke", "Analyze", "Describe", "Summarize", "Kill", "Refresh"}
FORBIDDEN_FUNC_PREFIXES = ("read_", "glob", "getenv", "current_setting", "duckdb_", "pragma_", "sniff_", "parquet_",
                           "query", "load_", "write_", "httpfs", "sqlite_", "postgres_", "mysql_", "iceberg_", "delta_",
                           "json_execute", "system", "copy_", "checkpoint", "enable_", "disable_", "which_secret",
                           "list_files", "file_", "import_", "export_")
ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    try:
        return node.sql_name().lower()
    except Exception:  # noqa: BLE001
        return type(node).__name__.lower()


def validate_sql(sql: str, allowed_tables: set[str]) -> tuple[list[ExecError], list[str], set[str]]:
    """Return (errors, warnings, referenced_tables)."""
    errors: list[ExecError] = []
    warnings: list[str] = []
    refs: set[str] = set()
    if not sql or not sql.strip():
        return [ExecError("SQL is empty", kind="SYNTAX")], warnings, refs
    if len(sql) > 200_000:
        return [ExecError("SQL is too long", kind="SAFETY")], warnings, refs
    try:
        stmts = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except ParseError as e:
        for d in e.errors[:5]:
            errors.append(ExecError(d.get("description", str(e)), d.get("line"), d.get("col"), "SYNTAX"))
        return errors or [ExecError(str(e), kind="SYNTAX")], warnings, refs
    if len(stmts) != 1:
        return [ExecError(f"Exactly one SELECT statement is allowed (found {len(stmts)})", kind="SAFETY")], warnings, refs
    root = stmts[0]
    if not isinstance(root, (exp.Select, exp.SetOperation, exp.Subquery)):
        return [ExecError(f"Only read-only SELECT queries are allowed (found {type(root).__name__.upper()})", kind="SAFETY")], warnings, refs
    cte_names = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    allowed_lower = {t.lower() for t in allowed_tables}
    for node in root.walk():
        name = type(node).__name__
        if name in FORBIDDEN_NODES:
            errors.append(ExecError(f"Statement element '{name.upper()}' is not allowed", kind="SAFETY"))
            continue
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                errors.append(ExecError(f"Table functions are not allowed: {node.this.sql('duckdb')[:80]}", kind="SAFETY"))
                continue
            if node.args.get("db") or node.args.get("catalog"):
                errors.append(ExecError(f"Schema/catalog-qualified table '{node.sql('duckdb')}' is not allowed", kind="SAFETY"))
                continue
            tname = node.name.lower()
            if tname in cte_names:
                continue
            if tname not in allowed_lower:
                errors.append(ExecError(f"Unknown dataset '{node.name}'. Available: {', '.join(sorted(allowed_tables)) or 'none'}",
                                        kind="SAFETY"))
            else:
                refs.add(tname)
        elif isinstance(node, exp.Func):
            fn = _func_name(node)
            if fn.startswith(FORBIDDEN_FUNC_PREFIXES) or "Read" in name:
                errors.append(ExecError(f"Function '{fn}' is not allowed", kind="SAFETY"))
    # line numbers for safety errors: best-effort lookup of the offending token
    for e in errors:
        if e.line is None:
            m = re.search(r"'([^']+)'", e.message)
            if m:
                tok = m.group(1).split("(")[0].lower()
                for i, line in enumerate(sql.splitlines(), 1):
                    if tok and tok in line.lower():
                        e.line = i
                        break
    if isinstance(root, exp.Select) and not root.args.get("group") and any(
        isinstance(s.unalias(), exp.AggFunc) for s in root.expressions) and len(root.expressions) > 1:
        warnings.append("Aggregate functions without GROUP BY: check that all non-aggregated columns are grouped.")
    if "*" in [s.sql() for s in getattr(root, "expressions", [])]:
        warnings.append("SELECT * output depends on input schemas; prefer explicit target columns.")
    return errors, warnings, refs


def _parse_duckdb_error(msg: str) -> ExecError:
    line = None
    m = re.search(r"LINE (\d+):", msg)
    if m:
        line = int(m.group(1))
    kind = "TIMEOUT" if "INTERRUPT" in msg.upper() else "RUNTIME"
    first = msg.split("\n")[0]
    return ExecError(first[:1000], line, None, kind)


def execute_sql(sql: str, inputs: dict[str, Path], output_path: Path, *, limit: int | None = None,
                timeout_s: int | None = None) -> tuple[list[ExecError], list[str], int, list[dict]]:
    """Run validated SQL against the given parquet inputs; stream output to parquet.
    Returns (errors, logs, row_count, schema)."""
    s = get_settings()
    timeout_s = timeout_s or s.sql_timeout_s
    for alias in inputs:
        if not ALIAS_RE.match(alias):
            return [ExecError(f"Invalid dataset alias '{alias}'", kind="SAFETY")], [], 0, []
    errors, _, _ = validate_sql(sql, set(inputs))
    if errors:
        return errors, [], 0, []
    logs: list[str] = []
    con = duckdb.connect(":memory:")
    timer = None
    try:
        con.execute(f"SET memory_limit='{s.sql_memory_limit}'")
        con.execute(f"SET threads={int(s.sql_threads)}")
        con.execute("SET autoinstall_known_extensions=false")
        con.execute("SET autoload_known_extensions=false")
        for alias, path in inputs.items():
            con.execute(f'CREATE TABLE "{alias}" AS SELECT * FROM read_parquet(?)', [str(path)])
        logs.append(f"Loaded inputs: {', '.join(inputs)}")
        con.execute("SET enable_external_access=false")
        con.execute("SET lock_configuration=true")
        body = sql.strip().rstrip(";")
        q = f"SELECT * FROM ({body}) AS __q LIMIT {int(limit)}" if limit else body
        timer = threading.Timer(timeout_s, con.interrupt)
        timer.start()
        t0 = time.perf_counter()
        reader = con.execute(q).to_arrow_reader(65536)
        writer = None
        rows = 0
        for batch in reader:
            if writer is None:
                writer = pq.ParquetWriter(output_path, batch.schema)
            writer.write_batch(batch)
            rows += batch.num_rows
        schema_obj = reader.schema
        if writer is None:
            pq.write_table(schema_obj.empty_table(), output_path)
        else:
            writer.close()
        logs.append(f"Query returned {rows} rows in {int((time.perf_counter() - t0) * 1000)} ms")
        schema = [{"name": f.name, "type": str(f.type)} for f in schema_obj]
        if len({c['name'].lower() for c in schema}) != len(schema):
            return [ExecError("Output has duplicate column names", kind="CONTRACT")], logs, 0, []
        return [], logs, rows, schema
    except duckdb.Error as e:
        err = _parse_duckdb_error(str(e))
        if err.kind == "TIMEOUT":
            err.message = f"Query exceeded the {timeout_s}s execution limit and was cancelled"
        return [err], logs, 0, []
    finally:
        if timer:
            timer.cancel()
        con.close()
