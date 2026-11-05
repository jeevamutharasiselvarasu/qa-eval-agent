"""Deterministic validation engine checks on small hand-built tables."""
import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.services.validation import CheckError, ValidationContext, evaluate_guards, run_check


def write(tmp_path, name, rows, schema=None):
    p = tmp_path / f"{name}.parquet"
    t = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
    pq.write_table(t, p)
    return p


@pytest.fixture
def mk(tmp_path):
    def _mk(expected, actual, sources=None, schema=None):
        e = write(tmp_path, "e", expected, schema)
        a = write(tmp_path, "a", actual, schema)
        srcs = {k: write(tmp_path, "s_" + k, v) for k, v in (sources or {}).items()}
        return ValidationContext(e, a, srcs)
    return _mk


E = [{"ID": "1", "AMT": 10.0, "NAME": "A"}, {"ID": "2", "AMT": 20.0, "NAME": "B"}, {"ID": "3", "AMT": None, "NAME": "C"}]


def test_schema_mismatch(mk):
    ctx = mk(E, [{"ID": "1", "AMT": "x", "EXTRA": 1}])
    r = run_check(ctx, {"type": "SCHEMA_MATCH"})
    assert r.status == "FAIL" and r.metrics["missing"] == ["NAME"] and r.metrics["unexpected"] == ["EXTRA"] and r.metrics["type_mismatches"] == 1


def test_count_mismatch_and_match(mk):
    assert run_check(mk(E, E[:2]), {"type": "ROW_COUNT"}).status == "FAIL"
    assert run_check(mk(E, E), {"type": "ROW_COUNT"}).status == "PASS"


def test_duplicate_and_null_keys(mk):
    ctx = mk(E, E + [{"ID": "1", "AMT": 1.0, "NAME": "Z"}, {"ID": None, "AMT": 1.0, "NAME": "Q"}])
    u = run_check(ctx, {"type": "KEY_UNIQUE", "columns": ["ID"]})
    assert u.status == "FAIL" and u.metrics["duplicate_keys"] == 1
    n = run_check(ctx, {"type": "NOT_NULL", "columns": ["id"]})  # case-insensitive column resolution
    assert n.status == "FAIL" and n.metrics["null_counts"]["ID"] == 1


def test_missing_and_extra_records(mk):
    ctx = mk(E, [E[0], E[1], {"ID": "9", "AMT": 1.0, "NAME": "N"}])
    m = run_check(ctx, {"type": "RECON_MISSING", "keys": ["ID"]})
    x = run_check(ctx, {"type": "RECON_EXTRA", "keys": ["ID"]})
    assert m.status == x.status == "FAIL"
    assert m.evidence_rows[0]["ID"] == "3" and x.evidence_rows[0]["ID"] == "9"


def test_column_mismatch_decimal_tolerance_and_null_safety(mk):
    act = [{"ID": "1", "AMT": 10.004, "NAME": "A"}, {"ID": "2", "AMT": 20.2, "NAME": "B"}, {"ID": "3", "AMT": None, "NAME": "C"}]
    r = run_check(mk(E, act), {"type": "COLUMN_MATCH", "column": "AMT", "keys": ["ID"], "tolerance": 0.005})
    assert r.status == "FAIL" and r.metrics["mismatches"] == 1 and r.evidence_rows[0]["ID"] == "2"
    assert abs(r.evidence_rows[0]["difference"] - 0.2) < 1e-9
    act2 = [dict(a) for a in act]
    act2[2]["AMT"] = 0.0  # NULL vs 0 is a mismatch (null-safe) even though 20.0 vs 20.2 is within tolerance
    r2 = run_check(mk(E, act2), {"type": "COLUMN_MATCH", "column": "AMT", "keys": ["ID"], "tolerance": 0.5})
    assert r2.metrics["mismatches"] == 1 and r2.evidence_rows[0]["ID"] == "3"


def test_case_and_whitespace_options(mk):
    act = [{"ID": "1", "AMT": 10.0, "NAME": " a "}, {"ID": "2", "AMT": 20.0, "NAME": "B"}, {"ID": "3", "AMT": None, "NAME": "C"}]
    strict = run_check(mk(E, act), {"type": "COLUMN_MATCH", "column": "NAME", "keys": ["ID"]})
    loose = run_check(mk(E, act), {"type": "COLUMN_MATCH", "column": "NAME", "keys": ["ID"], "trim": True, "case_insensitive": True})
    assert strict.status == "FAIL" and loose.status == "PASS"


def test_composite_keys_and_duplicate_exclusion(mk):
    e = [{"K1": "a", "K2": 1, "V": 1}, {"K1": "a", "K2": 2, "V": 2}]
    a = [{"K1": "a", "K2": 1, "V": 1}, {"K1": "a", "K2": 2, "V": 3}, {"K1": "a", "K2": 2, "V": 2}]
    r = run_check(mk(e, a), {"type": "COLUMN_MATCH", "column": "V", "keys": ["K1", "K2"]})
    assert r.metrics["compared"] == 1 and r.status == "PASS"  # duplicated actual key excluded, flagged by KEY_UNIQUE instead
    assert run_check(mk(e, a), {"type": "KEY_UNIQUE", "columns": ["K1", "K2"]}).status == "FAIL"


def test_date_precision(mk):
    s = pa.schema([("ID", pa.string()), ("TS", pa.timestamp("us"))])
    e = [{"ID": "1", "TS": dt.datetime(2026, 1, 1, 10, 0, 0)}]
    a = [{"ID": "1", "TS": dt.datetime(2026, 1, 1, 10, 0, 0, 500)}]
    assert run_check(mk(e, a, schema=s), {"type": "COLUMN_MATCH", "column": "TS", "keys": ["ID"]}).status == "FAIL"


def test_empty_source_and_target(mk):
    s = pa.schema([("ID", pa.string()), ("AMT", pa.float64())])
    ctx = mk([], [], schema=s)
    assert run_check(ctx, {"type": "ROW_COUNT"}).status == "PASS"
    assert run_check(ctx, {"type": "RECON_MISSING", "keys": ["ID"]}).status == "PASS"
    assert run_check(ctx, {"type": "COLUMN_MATCH", "column": "AMT", "keys": ["ID"]}).status == "PASS"


def test_no_key_is_technical_error_not_failure(mk):
    with pytest.raises(CheckError):
        run_check(mk(E, E), {"type": "RECON_MISSING", "keys": []})
    with pytest.raises(CheckError):
        run_check(mk(E, E), {"type": "BOGUS"})


def test_join_coverage_defaults_and_filter_boundary(mk):
    base = [{"CID": "c1"}, {"CID": "c2"}, {"CID": "c3"}]
    right = [{"CID": "c1", "ST": "OK"}, {"CID": "c2", "ST": "NO"}]
    tgt = [{"CID": "c1", "N": 1}, {"CID": "c2", "N": 5}]
    ctx = mk(tgt, tgt, {"base": base, "right": right})
    cov = run_check(ctx, {"type": "KEY_COVERAGE", "base": "base", "base_key": "CID", "key": "CID"})
    assert cov.status == "FAIL" and cov.evidence_rows == [{"CID": "c3"}]
    d = run_check(ctx, {"type": "UNMATCHED_DEFAULTS", "base": "base", "right": "right", "join_keys": ["CID"],
                        "right_filter": {"column": "ST", "op": "=", "value": "OK"}, "key": "CID", "defaults": {"N": 0}})
    assert d.status == "FAIL" and d.metrics["unmatched_keys"] == 2 and d.metrics["violations"] == 1  # c2 filtered out but N=5


def test_aggregation_mismatch_and_join_duplication_via_snapshot(mk):
    e = [{"ID": "1", "TOTAL": 30.0}, {"ID": "2", "TOTAL": 5.0}]
    a = [{"ID": "1", "TOTAL": 60.0}, {"ID": "2", "TOTAL": 5.0}]  # doubled by a fan-out join
    r = run_check(mk(e, a), {"type": "SNAPSHOT_MATCH", "keys": ["ID"]})
    assert r.status == "FAIL" and r.metrics["changed_values"] == 1


def test_rule_predicate(mk):
    r = run_check(mk(E, E), {"type": "RULE_PREDICATE", "condition": {"column": "AMT", "op": ">=", "value": 0}})
    assert r.status == "FAIL" and r.metrics["violations"] == 1  # NULL AMT violates (unknown is not satisfied)


def test_runtime_guards(mk):
    ctx = mk([{"ID": "1", "N": -5.0}], [{"ID": "1", "N": -5.0}], {"tx": [{"ID": "1", "D": None}]})
    assert evaluate_guards(ctx, [{"gap_id": "G1", "when": "NULLS_IN_SOURCE", "columns": ["tx.D"]}], {"G1"})
    assert evaluate_guards(ctx, [{"gap_id": "G2", "when": "NEGATIVE_IN_EXPECTED", "column": "N"}], {"G2"})
    assert evaluate_guards(ctx, [{"gap_id": "G2", "when": "NEGATIVE_IN_EXPECTED", "column": "N"}], set()) is None
