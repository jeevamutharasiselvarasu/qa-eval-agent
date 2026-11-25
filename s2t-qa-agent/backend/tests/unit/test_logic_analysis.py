"""Unit tests: reading the transformation SQL and deriving tests from it."""

from types import SimpleNamespace

import pytest
import sqlglot

from app.services.logic_tests import PY_NOTE, Logic, generate, note, props


def p(expr: str) -> dict:
    return props(sqlglot.parse_one(expr, read="duckdb"))


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("UPPER(x)", {"UPPER"}),
        ("LOWER(TRIM(x))", {"LOWER", "TRIMMED"}),
        ("TRIM(UPPER(x))", {"TRIMMED", "UPPER"}),
        ("ROUND(x, 2)", {"MAX_DECIMALS"}),
        ("ROUND(x)", {"MAX_DECIMALS"}),
        ("COUNT(x)", {"NOT_NULL", "NON_NEGATIVE", "NON_NEGATIVE_INTEGER", "MAX_DECIMALS"}),
        ("ABS(x)", {"NON_NEGATIVE"}),
        ("LEFT(x, 3)", {"MAX_LENGTH"}),
        ("SUBSTRING(x, 1, 4)", {"MAX_LENGTH"}),
        ("SUM(x)", set()),
        ("x + y", set()),
    ],
)
def test_properties_of_common_functions(expr, expected):
    assert {k for k in p(expr) if not k.startswith("_")} == expected


def test_round_decimals_and_length_arguments_are_kept():
    assert p("ROUND(x, 3)")["MAX_DECIMALS"] == 3
    assert p("ROUND(x)")["MAX_DECIMALS"] == 0
    assert p("LEFT(x, 5)")["MAX_LENGTH"] == 5
    assert "MAX_DECIMALS" not in p("ROUND(x, y)")  # a non-literal precision guarantees nothing


def test_coalesce_with_a_literal_fallback_is_never_null_and_keeps_shared_properties():
    r = p("COALESCE(ROUND(SUM(a), 2), 0)")
    assert r["NOT_NULL"] and r["_FALLBACK"] == "0" and r["MAX_DECIMALS"] == 2
    assert "NOT_NULL" not in p("COALESCE(a, b)")  # no literal fallback: can still be null
    assert p("COALESCE(UPPER(a), 'N/A')").get("UPPER") is True
    assert "UPPER" not in p("COALESCE(UPPER(a), 'n/a')")  # the fallback is not upper-case


def test_case_with_literal_results_gives_the_allowed_values():
    r = p("CASE WHEN a > 1 THEN 'HIGH' WHEN a > 0 THEN 'LOW' ELSE 'NONE' END")
    assert r["ALLOWED_VALUES"] == ["HIGH", "LOW", "NONE"] and r["_ALLOW_NULL"] is False and r["UPPER"]
    r2 = p("CASE WHEN a > 1 THEN 'HIGH' END")
    assert r2["_ALLOW_NULL"] is True and "NOT_NULL" not in r2
    assert "ALLOWED_VALUES" not in p("CASE WHEN a > 1 THEN b ELSE 'X' END")


def test_literals_and_casts():
    assert p("'ABC'")["UPPER"] and p("'ABC'")["TRIMMED"]
    assert p("12")["NON_NEGATIVE_INTEGER"] and p("-1.5").get("NON_NEGATIVE") is None
    assert p("CAST(COALESCE(a, 0) AS INT)") == {"NOT_NULL": True}


SQL = """WITH c AS (SELECT ID, UPPER(TRIM(NAME)) AS NAME FROM customers)
SELECT c.ID AS CUSTOMER_ID, c.NAME AS CUSTOMER_NAME, COALESCE(SUM(t.AMT), 0) AS TOTAL, COUNT(t.ID) AS N
FROM c LEFT JOIN (SELECT * FROM transactions WHERE STATUS = 'OK') AS t ON c.ID = t.CID
GROUP BY c.ID, c.NAME"""


def test_logic_reads_columns_joins_filters_and_grouping():
    lg = Logic(SQL)
    assert lg.error is None
    assert [n for n, _ in lg.columns] == ["CUSTOMER_ID", "CUSTOMER_NAME", "TOTAL", "N"]
    # a column taken from a CTE resolves to the CTE's expression
    assert props(dict(lg.columns)["CUSTOMER_NAME"]).keys() >= {"UPPER", "TRIMMED"}
    j = lg.joins[0]
    assert j["type"] == "LEFT" and j["table"] == "transactions" and j["alias"] == "t"
    assert j["pairs"] == [("c", "ID", "t", "CID")] and "STATUS" in j["conds"][0]
    assert lg.group == ["CUSTOMER_ID", "CUSTOMER_NAME"]
    assert lg.output_of("c", "ID") == "CUSTOMER_ID" and lg.output_of("c", "NOPE") is None


def test_group_by_all_groups_by_the_non_aggregated_columns():
    lg = Logic("SELECT a.K, a.N, SUM(a.V) AS S FROM a GROUP BY ALL")
    assert lg.group == ["K", "N"]


def test_unsupported_or_broken_sql_is_reported_not_guessed():
    assert "not a SELECT statement" in Logic("SELEC nope").error
    assert "could not be parsed" in Logic("SELECT (1").error
    assert "several SELECTs" in Logic("SELECT 1 AS a UNION SELECT 2").error


def test_note_explains_when_no_logic_tests_can_be_made():
    assert note(None) == "No transformation yet"
    assert note(SimpleNamespace(mode="PYTHON", code="")) == PY_NOTE
    assert note(SimpleNamespace(mode="SQL", code="SELECT 1 AS x")) is None


class FakeBuilder:
    def __init__(self):
        self.out = []

    def add(self, group, test_type, track, category, title, validation, **kw):
        self.out.append(
            SimpleNamespace(
                group=group,
                test_type=test_type,
                title=title,
                validation=validation,
                track=track,
                state="BLOCKED" if kw.get("blocked") else "READY",
                **kw,
            )
        )


def test_generate_turns_the_sql_into_target_checks():
    ctx = SimpleNamespace(
        keys=["CUSTOMER_ID"],
        run=SimpleNamespace(settings={}),
        target=SimpleNamespace(columns=[{"name": n, "type": "VARCHAR"} for n in ("CUSTOMER_ID", "CUSTOMER_NAME", "TOTAL", "N")]),
    )
    b = FakeBuilder()
    generate(b, ctx, SimpleNamespace(mode="SQL", code=SQL, version=3))
    kinds = {(t.validation["type"], t.validation.get("property")) for t in b.out}
    assert ("VALUE_PROPERTY", "UPPER") in kinds and ("VALUE_PROPERTY", "TRIMMED") in kinds
    assert ("VALUE_PROPERTY", "NOT_NULL") in kinds and ("VALUE_PROPERTY", "NON_NEGATIVE_INTEGER") in kinds
    assert ("KEY_COVERAGE", None) in kinds  # a LEFT JOIN keeps every base record even when the joined side is filtered
    assert ("KEY_UNIQUE", None) in kinds
    # columns not covered by an acceptance criterion are compared value by value (the key itself is not)
    matched = {t.validation["column"] for t in b.out if t.validation["type"] == "COLUMN_MATCH"}
    assert matched == {"CUSTOMER_NAME", "TOTAL", "N"}
    assert all(t.group == "TL" and t.basis == "TRANSFORMATION_LOGIC" for t in b.out)
    assert all("Derived from the SQL (v3)" in t.rationale for t in b.out)


def test_generate_adds_nothing_for_python_transformations():
    b = FakeBuilder()
    generate(b, SimpleNamespace(), SimpleNamespace(mode="PYTHON", code="def transform(s): ..."))
    assert b.out == []
