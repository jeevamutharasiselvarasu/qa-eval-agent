"""Unit tests: the checks that follow from the transformation logic, and edge-case scoping of any check."""

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.services.validation import CheckError, ValidationContext, compare_targets, run_check


def _write(tmp_path, name, rows):
    p = tmp_path / f"{name}.parquet"
    pq.write_table(pa.Table.from_pylist(rows), p)
    return p


@pytest.fixture
def ctx(tmp_path):
    def make(actual, expected=None, sources=None):
        a = _write(tmp_path, "actual", actual)
        e = _write(tmp_path, "expected", expected or actual)
        srcs = {k: _write(tmp_path, "src_" + k, v) for k, v in (sources or {}).items()}
        return ValidationContext(e, a, srcs)

    return make


def prop(ctx_factory, rows, column, prop_name, arg=None, allow_null=None):
    spec = {"type": "VALUE_PROPERTY", "table": "actual", "column": column, "property": prop_name, "arg": arg, "allow_null": allow_null}
    return run_check(ctx_factory(rows), spec)


@pytest.mark.parametrize(
    ("prop_name", "good", "bad", "arg"),
    [
        ("UPPER", ["ANITA", "RAVI", None], ["ANITA", "Ravi"], None),
        ("LOWER", ["anita", None], ["anita", "Ravi"], None),
        ("TRIMMED", ["A B", "C"], ["A", " B "], None),
        ("MAX_DECIMALS", [1.25, 3.0, None], [1.25, 1.255], 2),
        ("NOT_NULL", [0, 1], [0, None], None),
        ("NON_NEGATIVE", [0, 5.5], [0, -0.01], None),
        ("NON_NEGATIVE_INTEGER", [0, 3], [2, 2.5], None),
        ("MAX_LENGTH", ["ABC", "AB"], ["ABC", "ABCD"], 3),
    ],
)
def test_each_property_passes_good_values_and_fails_bad_ones(ctx, prop_name, good, bad, arg):
    ok = prop(ctx, [{"V": v} for v in good], "V", prop_name, arg)
    assert ok.status == "PASS", ok.message
    ko = prop(ctx, [{"V": v} for v in bad], "V", prop_name, arg)
    assert ko.status == "FAIL" and ko.metrics["violations"] == 1
    assert ko.evidence_rows, "a failure carries the violating rows as evidence"


def test_non_negative_integer_also_rejects_nulls(ctx):
    assert prop(ctx, [{"N": 1}, {"N": None}], "N", "NON_NEGATIVE_INTEGER").status == "FAIL"


def test_allowed_values_with_and_without_null(ctx):
    rows = [{"S": "GOLD"}, {"S": "SILVER"}, {"S": None}]
    assert prop(ctx, rows, "S", "ALLOWED_VALUES", ["GOLD", "SILVER"], allow_null=True).status == "PASS"
    r = prop(ctx, rows, "S", "ALLOWED_VALUES", ["GOLD", "SILVER"], allow_null=False)
    assert r.status == "FAIL" and r.metrics["violations"] == 1
    assert prop(ctx, rows + [{"S": "BRONZE"}], "S", "ALLOWED_VALUES", ["GOLD", "SILVER"], allow_null=True).status == "FAIL"


def test_column_lookup_is_case_insensitive_and_missing_column_is_an_error(ctx):
    assert prop(ctx, [{"Name": "A"}], "NAME", "UPPER").status == "PASS"
    with pytest.raises(CheckError, match="not found in the uploaded target"):
        prop(ctx, [{"X": 1}], "NAME", "UPPER")
    with pytest.raises(CheckError, match="Unknown property"):
        prop(ctx, [{"X": 1}], "X", "SHINY")


def test_key_subset_flags_records_that_do_not_come_from_the_allowed_source_records(ctx):
    c = ctx([{"ID": "1"}, {"ID": "2"}, {"ID": "9"}], sources={"cust": [{"ID": "1"}, {"ID": "2"}, {"ID": "3"}]})
    spec = {"type": "KEY_SUBSET", "table": "actual", "key": "ID", "source_sql": 'SELECT "ID" FROM "src_cust"', "source_desc": "customers"}
    r = run_check(c, spec)
    assert r.status == "FAIL" and r.metrics["violations"] == 1 and r.evidence_rows[0]["ID"] == "9"


def test_key_subset_with_a_broken_source_query_is_an_error_not_a_failure(ctx):
    c = ctx([{"ID": "1"}])
    with pytest.raises(CheckError, match="Could not evaluate"):
        run_check(c, {"type": "KEY_SUBSET", "table": "actual", "key": "ID", "source_sql": "SELECT nope FROM nowhere"})


# ------------------------------------------------------------------ scope (edge-case records only)
def _scoped_match(ctx, expected, actual, scope_sql, restrict=("actual", "expected")):
    c = ctx(actual, expected, sources={"tx": [{"CID": "C1", "AMT": 0.0}, {"CID": "C2", "AMT": 5.0}]})
    spec = {
        "type": "COLUMN_MATCH",
        "column": "V",
        "keys": ["CID"],
        "scope": {"sql": scope_sql, "desc": "a zero amount", "restrict": {t: "CID" for t in restrict}},
    }
    return run_check(c, spec)


def test_scoped_check_only_looks_at_the_records_hitting_the_edge_case(ctx):
    expected = [{"CID": "C1", "V": 1}, {"CID": "C2", "V": 2}]
    actual = [{"CID": "C1", "V": 1}, {"CID": "C2", "V": 999}]  # C2 is wrong, but C2 has no zero amount
    r = _scoped_match(ctx, expected, actual, 'SELECT "CID" FROM "src_tx" WHERE "AMT" = 0')
    assert r.status == "PASS" and r.metrics["scope_records"] == 1
    assert r.message.startswith("Checked on the 1 record(s) with a zero amount")


def test_scoped_check_fails_when_an_edge_case_record_is_wrong(ctx):
    expected = [{"CID": "C1", "V": 1}, {"CID": "C2", "V": 2}]
    actual = [{"CID": "C1", "V": 7}, {"CID": "C2", "V": 2}]
    assert _scoped_match(ctx, expected, actual, 'SELECT "CID" FROM "src_tx" WHERE "AMT" = 0').status == "FAIL"


def test_scope_with_no_matching_records_is_skipped_never_passed(ctx):
    rows = [{"CID": "C1", "V": 1}]
    r = _scoped_match(ctx, rows, rows, 'SELECT "CID" FROM "src_tx" WHERE "AMT" < 0')
    assert r.status == "SKIPPED" and "no record in the uploaded data has a zero amount" in r.message


def test_scope_without_restriction_only_requires_the_edge_case_to_exist(ctx):
    c = ctx([{"ID": "1"}], sources={"cust": [{"ID": None}]})
    spec = {
        "type": "NOT_NULL",
        "columns": ["ID"],
        "table": "actual",
        "scope": {"sql": 'SELECT 1 FROM "src_cust" WHERE "ID" IS NULL', "desc": "a null key", "restrict": {}},
    }
    r = run_check(c, spec)
    assert r.status == "PASS" and r.metrics["scope_records"] == 1


def test_scope_key_missing_from_a_view_is_an_error(ctx):
    c = ctx([{"ID": "1", "V": 1}], sources={"tx": [{"CID": "1"}]})
    spec = {
        "type": "COLUMN_MATCH",
        "column": "V",
        "keys": ["ID"],
        "scope": {"sql": 'SELECT "CID" FROM "src_tx"', "restrict": {"actual": "NOPE"}},
    }
    with pytest.raises(CheckError, match="Scope key"):
        run_check(c, spec)


def test_mismatch_wording_is_singular_for_one_record(ctx):
    c = ctx([{"ID": "1", "V": 2}], [{"ID": "1", "V": 1}])
    r = run_check(c, {"type": "COLUMN_MATCH", "column": "V", "keys": ["ID"]})
    assert r.actual == "1 mismatch"


# ------------------------------------------------------------------ column comparison for the report
def test_compare_targets_lists_each_record_and_column_once(tmp_path):
    expected = [
        {"ID": "C1", "NAME": "ANITA", "AMT": 10.0},
        {"ID": "C2", "NAME": "RAVI", "AMT": 5.0},
        {"ID": "C3", "NAME": "MEERA", "AMT": 1.0},
    ]
    actual = [
        {"ID": "C1", "NAME": "Anita", "AMT": 10.0},  # name differs
        {"ID": "C2", "NAME": "RAVI", "AMT": -100.0},  # amount differs
        {"ID": "C3", "NAME": "MEERA", "AMT": 1.0},
        {"ID": "C9", "NAME": "X", "AMT": 0.0},  # extra record: not a column difference
    ]
    out = compare_targets(_write(tmp_path, "e", expected), _write(tmp_path, "a", actual), ["id"])
    assert out["total_differences"] == 2 and not out["truncated"]
    assert [(r["business_key"], r["column"]) for r in out["differences"]] == [("C1", "NAME"), ("C2", "AMT")]
    assert out["differences"][1]["difference"] == -105.0
    assert {c["column"]: c["differences"] for c in out["columns"]} == {"NAME": 1, "AMT": 1}
    assert all(c["compared"] == 3 for c in out["columns"])


def test_compare_targets_leaves_out_duplicate_keys_and_needs_a_key(tmp_path):
    e = _write(tmp_path, "e", [{"ID": "C1", "V": 1}, {"ID": "C2", "V": 2}])
    a = _write(tmp_path, "a", [{"ID": "C1", "V": 9}, {"ID": "C1", "V": 8}, {"ID": "C2", "V": 2}])
    out = compare_targets(e, a, ["ID"])
    assert out["total_differences"] == 0 and out["duplicate_keys_excluded"] == 2
    with pytest.raises(CheckError, match="No business key"):
        compare_targets(e, a, [])
    with pytest.raises(CheckError, match="not found"):
        compare_targets(e, a, ["NOPE"])


def test_compare_targets_truncates_long_lists(tmp_path):
    e = _write(tmp_path, "e", [{"ID": str(i), "V": i} for i in range(10)])
    a = _write(tmp_path, "a", [{"ID": str(i), "V": i + 1} for i in range(10)])
    out = compare_targets(e, a, ["ID"], limit=3)
    assert out["total_differences"] == 10 and len(out["differences"]) == 3 and out["truncated"]
