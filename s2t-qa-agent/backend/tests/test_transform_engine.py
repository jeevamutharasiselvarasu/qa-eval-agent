"""Transformation engine: SQL/Python safety, limits, visual compilation and mode equivalence."""
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest

from app.services.transform.contract import TransformationDefinition
from app.services.transform.engine import execute, validate_definition
from app.services.transform.python_exec import execute_python, validate_python
from app.services.transform.sql_exec import execute_sql, validate_sql
from app.services.transform.visual import VisualSpecError, compile_visual, compile_visual_to_pandas, sql_to_expr

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"


@pytest.fixture
def inputs(tmp_path):
    out = {}
    with duckdb.connect() as c:
        for alias, f in (("customers", "customers.csv"), ("transactions", "transactions.csv")):
            p = tmp_path / f"{alias}.parquet"
            c.execute(f"COPY (SELECT * FROM read_csv('{SAMPLES / f}')) TO '{p}' (FORMAT parquet)")
            out[alias] = p
    return out


@pytest.mark.parametrize("sql,needle", [
    ("DROP TABLE customers", "Only read-only"),
    ("DELETE FROM customers", "Only read-only"),
    ("INSERT INTO customers VALUES (1)", "Only read-only"),
    ("UPDATE customers SET SEGMENT = 'x'", "Only read-only"),
    ("CREATE TABLE x AS SELECT 1", "Only read-only"),
    ("COPY customers TO '/tmp/x.csv'", "Only read-only"),
    ("ATTACH 'x.db' AS y", "Only read-only"),
    ("SELECT 1; SELECT 2", "Exactly one"),
    ("SELECT * FROM read_csv('/etc/passwd')", "not allowed"),
    ("SELECT * FROM read_parquet('x')", "not allowed"),
    ("SELECT getenv('HOME')", "not allowed"),
    ("SELECT * FROM main.customers", "qualified"),
    ("SELECT * FROM secret_table", "Unknown dataset"),
    ("SELECT 1 INTO t2", "not allowed"),
    ("PRAGMA database_list", "Only read-only"),
    ("SET memory_limit='100GB'", "Only read-only"),
    ("INSTALL httpfs", "Only read-only"),
    ("SELECT * FROM glob('/*')", "not allowed"),
])
def test_sql_policy_blocks_unsafe_statements(sql, needle):
    errs, _, _ = validate_sql(sql, {"customers", "transactions"})
    assert errs and any(needle.lower() in e.message.lower() for e in errs), [e.message for e in errs]


def test_sql_allows_ctes_and_set_operations():
    sql = "WITH a AS (SELECT CUSTOMER_ID FROM customers) SELECT * FROM a UNION ALL SELECT CUSTOMER_ID FROM transactions"
    errs, _, refs = validate_sql(sql, {"customers", "transactions"})
    assert not errs and refs == {"customers", "transactions"}


def test_sql_syntax_errors_have_line_numbers():
    errs, _, _ = validate_sql("SELECT a,\nFROM WHERE", {"customers"})
    assert errs[0].kind == "SYNTAX" and errs[0].line is not None


def test_sql_runtime_lockdown_even_if_parser_missed(inputs, tmp_path):
    # runtime check: external access is disabled after inputs are loaded
    con_errs, *_ = execute_sql("SELECT COUNT(*) AS n FROM customers", inputs, tmp_path / "o.parquet")
    assert not con_errs
    assert pq.read_table(tmp_path / "o.parquet").column("n")[0].as_py() == 10


def test_sql_timeout(inputs, tmp_path):
    sql = "SELECT COUNT(*) FROM customers a, customers b, customers c, customers d, customers e, customers f, customers g, customers h, customers i, customers j, customers k"
    errs, *_ = execute_sql(sql, inputs, tmp_path / "o.parquet", timeout_s=1)
    assert errs and errs[0].kind == "TIMEOUT"


def test_sql_unknown_column_runtime_error_with_line(inputs, tmp_path):
    errs, *_ = execute_sql("SELECT\n  NOPE\nFROM customers", inputs, tmp_path / "o.parquet")
    assert errs and errs[0].kind == "RUNTIME" and errs[0].line == 2


@pytest.mark.parametrize("code,needle", [
    ("import os\ndef transform(sources): return os.listdir('/')", "Import of 'os'"),
    ("import subprocess\ndef transform(s): pass", "Import of 'subprocess'"),
    ("from socket import socket\ndef transform(s): pass", "Import of 'socket'"),
    ("def transform(s):\n    return open('/etc/passwd').read()", "'open'"),
    ("def transform(s):\n    return eval('1')", "'eval'"),
    ("def transform(s):\n    return getattr(s, 'x')", "'getattr'"),
    ("def transform(s):\n    return ().__class__.__bases__", "dunder"),
    ("import pandas as pd\ndef transform(s):\n    return pd.read_csv('/etc/passwd')", "read_csv"),
    ("def transform(s):\n    s['customers'].to_csv('/tmp/leak.csv')", "to_csv"),
    ("def transform(s):\n    return __import__('os')", "__import__"),
    ("def nothing(): pass", "must define"),
    ("def transform(a, b): pass", "exactly one argument"),
])
def test_python_policy(code, needle):
    errs, _ = validate_python(code)
    assert errs and any(needle.lower() in e.message.lower() for e in errs), [e.message for e in errs]


def test_python_audit_hook_blocks_file_access_not_caught_by_ast(inputs, tmp_path):
    # pandas internals that open files are not in the AST denylist; the worker's audit hook must stop them
    code = ("import pandas as pd\n\ndef transform(sources):\n"
            "    h = pd.io.common.get_handle('/etc/hostname', 'r')\n    return sources['customers']\n")
    assert not validate_python(code)[0]
    errs, *_ = execute_python(code, inputs, tmp_path / "o.parquet")
    assert errs and "not permitted" in errs[0].message and errs[0].kind == "SAFETY"


def test_python_runtime_errors_timeout_memory_and_contract(inputs, tmp_path):
    out = tmp_path / "o.parquet"
    e, *_ = execute_python("def transform(s):\n    x = 1\n    return x / 0\n", inputs, out)
    assert e[0].kind == "RUNTIME" and e[0].line == 3
    e, *_ = execute_python("def transform(s):\n    while True:\n        pass\n", inputs, out, timeout_s=2)
    assert e[0].kind == "TIMEOUT"
    e, *_ = execute_python("import numpy as np\ndef transform(s):\n    return np.ones((40000, 40000))\n", inputs, out)
    assert e and ("MemoryError" in e[0].message or "resource limit" in e[0].message)
    e, *_ = execute_python("def transform(s):\n    return 42\n", inputs, out)
    assert e[0].kind == "CONTRACT"


def test_python_success_and_logs(inputs, tmp_path):
    code = "def transform(sources):\n    c = sources['customers']\n    print('rows', len(c))\n    return c[['CUSTOMER_ID']]\n"
    errs, logs, rows, schema, _ = execute_python(code, inputs, tmp_path / "o.parquet")
    assert not errs and rows == 10 and schema[0]["name"] == "CUSTOMER_ID" and "rows 10" in logs[0]


SPEC = {
    "sources": [{"alias": "customers", "as": "c"},
                {"alias": "transactions", "as": "t", "filters": [{"column": "t.STATUS", "op": "=", "value": "COMPLETED"}]}],
    "joins": [{"right": "t", "type": "LEFT", "on": [{"left": "c.CUSTOMER_ID", "right": "t.CUSTOMER_ID"}]}],
    "columns": [
        {"target": "CUSTOMER_ID", "expr": {"col": "c.CUSTOMER_ID"}},
        {"target": "CUSTOMER_NAME", "expr": {"fn": "UPPER", "args": [{"fn": "TRIM", "args": [{"col": "c.CUSTOMER_NAME"}]}]}},
        {"target": "NET_AMOUNT", "expr": {"fn": "COALESCE", "args": [{"fn": "ROUND", "args": [
            {"fn": "SUM", "args": [{"op": "-", "args": [{"col": "t.AMOUNT"}, {"col": "t.DISCOUNT"}]}]}, {"lit": 2}]}, {"lit": 0}]}},
        {"target": "TXN_COUNT", "expr": {"fn": "COUNT", "args": [{"col": "t.TXN_ID"}]}},
        {"target": "TIER", "expr": {"case": [{"when": {"column": "c.SEGMENT", "op": "=", "value": "CORPORATE"}, "then": {"lit": "GOLD"}}],
                                     "else": {"lit": "STD"}}},
    ],
}


def _rows(p):
    return sorted(tuple(r.values()) for r in pq.read_table(p).to_pylist())


def test_visual_sql_python_equivalence(inputs, tmp_path):
    vis = TransformationDefinition("t", "r", "VISUAL", inputs, visual_spec=SPEC)
    sql = TransformationDefinition("t", "r", "SQL", inputs, code=compile_visual(SPEC))
    spec_no_case = {**SPEC, "columns": SPEC["columns"][:4]}
    py = TransformationDefinition("t", "r", "PYTHON", inputs, code=compile_visual_to_pandas(spec_no_case, {
        "customers": ["CUSTOMER_ID", "CUSTOMER_NAME", "SEGMENT", "EMAIL"],
        "transactions": ["TXN_ID", "CUSTOMER_ID", "AMOUNT", "DISCOUNT", "STATUS", "TXN_DATE"]}))
    a = execute(vis, tmp_path / "a.parquet")
    b = execute(sql, tmp_path / "b.parquet")
    c = execute(py, tmp_path / "c.parquet")
    assert a.status == b.status == c.status == "SUCCESS", (a.errors, b.errors, c.errors)
    assert _rows(tmp_path / "a.parquet") == _rows(tmp_path / "b.parquet")
    four = sorted((r[0], r[1], round(r[2], 2), int(r[3])) for r in _rows(tmp_path / "a.parquet"))
    assert four == sorted((r[0], r[1], round(r[2], 2), int(r[3])) for r in _rows(tmp_path / "c.parquet"))
    assert ("C109", "DIVYA MENON", 0.0, 0) in four  # filter applied before LEFT JOIN keeps unmatched customers


def test_visual_rejects_unsafe_or_malformed_specs():
    with pytest.raises(VisualSpecError):
        compile_visual({"sources": [{"alias": "c"}], "columns": [{"target": "x", "expr": {"fn": "READ_CSV", "args": []}}]})
    with pytest.raises(VisualSpecError):
        compile_visual({"sources": [{"alias": "c"}], "columns": [{"target": 'x"; DROP', "expr": {"lit": 1}}]})
    with pytest.raises(VisualSpecError):
        compile_visual({"sources": [{"alias": "c"}, {"alias": "d"}], "columns": [{"target": "x", "expr": {"lit": 1}}]})
    with pytest.raises(VisualSpecError):
        compile_visual({"sources": [{"alias": "c"}], "columns": [{"target": "x", "expr": {"sql": "(SELECT 1 FROM secret)"}}]})
    # string literals are escaped
    sql = compile_visual({"sources": [{"alias": "c"}], "columns": [{"target": "x", "expr": {"lit": "it's'; DROP TABLE c; --"}}]})
    assert "'it''s''; DROP TABLE c; --'" in sql
    assert not validate_sql(sql, {"c"})[0]


def test_visual_dedupe_and_post_filter(inputs, tmp_path):
    spec = {"sources": [{"alias": "transactions", "as": "t"}],
            "columns": [{"target": "CUSTOMER_ID", "expr": {"col": "t.CUSTOMER_ID"}}, {"target": "AMOUNT", "expr": {"col": "t.AMOUNT"}}],
            "dedupe": {"keys": ["CUSTOMER_ID"], "order_by": [{"column": "AMOUNT", "desc": True}]},
            "post_filters": [{"column": "AMOUNT", "op": ">", "value": 100}]}
    r = execute(TransformationDefinition("t", "r", "VISUAL", inputs, visual_spec=spec), tmp_path / "o.parquet")
    assert r.status == "SUCCESS"
    rows = pq.read_table(tmp_path / "o.parquet").to_pylist()
    ids = [x["CUSTOMER_ID"] for x in rows]
    assert len(ids) == len(set(ids))
    assert next(x for x in rows if x["CUSTOMER_ID"] == "C106")["AMOUNT"] == 5000.0


def test_sql_to_expr_roundtrip():
    e = sql_to_expr("COALESCE(ROUND(SUM(t.AMOUNT - t.DISCOUNT), 2), 0)")
    assert e["fn"] == "COALESCE" and e["args"][0]["fn"] == "ROUND"
    assert compile_visual({"sources": [{"alias": "t"}], "columns": [{"target": "x", "expr": e}]}).count("SUM") == 1


def test_validate_definition_reports_compiled_sql(inputs):
    v = validate_definition(TransformationDefinition("t", "r", "VISUAL", inputs, visual_spec=SPEC))
    assert v["valid"] and "LEFT JOIN" in v["compiled_sql"]
