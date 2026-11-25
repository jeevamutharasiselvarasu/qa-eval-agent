"""Unit tests: the queries that find the uploaded records hitting each edge case (testgen._Scopes)."""

from types import SimpleNamespace

import duckdb
import pytest

from app.services.rule_model import Model
from app.services.testgen import _scope, _Scopes

CUSTOMERS = [("C1", "Anita", 10.0), ("C2", " Ravi ", 0.0), ("C3", "Meera", 5.0), ("C3", "Meera", 5.0), (None, "Ghost", 1.0)]
TXNS = [
    ("T1", "C1", 100.0, 10.0, "COMPLETED"),
    ("T2", "C1", 50.0, 80.0, "COMPLETED"),
    ("T3", "C2", 20.0, None, "CANCELLED"),
    ("T4", "C3", 30.555, 0.0, "COMPLETED"),
]


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute('CREATE TABLE "src_customers" ("CUSTOMER_ID" VARCHAR, "NAME" VARCHAR, "CREDIT" DOUBLE)')
    c.executemany('INSERT INTO "src_customers" VALUES (?, ?, ?)', CUSTOMERS)
    c.execute(
        'CREATE TABLE "src_transactions" ("TXN_ID" VARCHAR, "CUSTOMER_ID" VARCHAR, "AMOUNT" DOUBLE, "DISCOUNT" DOUBLE, "STATUS" VARCHAR)'
    )
    c.executemany('INSERT INTO "src_transactions" VALUES (?, ?, ?, ?, ?)', TXNS)
    yield c
    c.close()


def scopes(right_unique=False, flt=True, keys=("CUSTOMER_ID",)):
    cols = {
        "customers": [{"name": "CUSTOMER_ID", "type": "VARCHAR"}, {"name": "NAME", "type": "VARCHAR"}],
        "transactions": [{"name": "CUSTOMER_ID", "type": "VARCHAR"}, {"name": "AMOUNT", "type": "DOUBLE"}],
    }
    m = Model(
        aliases=list(cols),
        base="customers",
        columns=cols,
        key=["CUSTOMER_ID"],
        join={"type": "LEFT", "left": "customers", "right": "transactions", "keys": ["CUSTOMER_ID"], "req": []},
        filter={"alias": "transactions", "column": "STATUS", "op": "=", "value": "COMPLETED", "req": []} if flt else None,
        right_unique=right_unique,
    )
    return _Scopes(SimpleNamespace(model=m, keys=list(keys)))


def keys(con, sql):
    return sorted(r[0] for r in con.execute(f"SELECT DISTINCT * FROM ({sql}) s(k) WHERE k IS NOT NULL").fetchall())


def test_scopes_need_one_shared_business_key():
    assert scopes().ok
    assert not scopes(keys=("A", "B")).ok


def test_no_match_finds_base_records_without_any_joined_row(con):
    con.execute("""INSERT INTO "src_customers" VALUES ('C9', 'Lone', 0)""")
    assert keys(con, scopes().no_match()) == ["C9"]


def test_several_matches_and_its_absence_for_lookup_joins(con):
    assert keys(con, scopes().multi_match()) == ["C1"]
    assert scopes(right_unique=True).multi_match() is None  # a lookup (one row per key) cannot have several matches


def test_duplicate_and_null_source_keys(con):
    s = scopes()
    assert keys(con, s.duplicate_base()) == ["C3"]
    assert con.execute(f"SELECT COUNT(*) FROM ({s.null_base_key()})").fetchone()[0] == 1


def test_filter_pass_fail_and_all_fail(con):
    s = scopes()
    pred = s.filter_pred()
    assert pred == "x.\"STATUS\" = 'COMPLETED'"
    passes = s.keys_where("transactions", pred)
    fails = s.keys_where("transactions", f'NOT ({pred}) OR x."STATUS" IS NULL')
    assert keys(con, passes) == ["C1", "C3"] and keys(con, fails) == ["C2"]
    assert keys(con, f"{fails} EXCEPT {passes}") == ["C2"]
    assert scopes(flt=False).filter_pred() is None


@pytest.mark.parametrize(
    ("alias", "pred", "expected"),
    [
        ("transactions", 'x."DISCOUNT" IS NULL', ["C2"]),
        ("transactions", 'TRY_CAST(x."DISCOUNT" AS DOUBLE) > TRY_CAST(x."AMOUNT" AS DOUBLE)', ["C1"]),
        ("transactions", 'TRY_CAST(x."DISCOUNT" AS DOUBLE) = 0', ["C3"]),
        ("transactions", 'ROUND(TRY_CAST(x."AMOUNT" AS DOUBLE), 2) <> TRY_CAST(x."AMOUNT" AS DOUBLE)', ["C3"]),
        ("customers", 'x."NAME" <> TRIM(x."NAME")', ["C2"]),
    ],
)
def test_keys_where_maps_rows_of_either_source_to_business_keys(con, alias, pred, expected):
    assert keys(con, scopes().keys_where(alias, pred)) == expected


def test_keys_where_for_an_unrelated_source_is_none():
    assert scopes().keys_where("products", "TRUE") is None


def test_scope_spec_shape():
    assert _scope("SELECT 1", "a thing", {"actual": "K"}) == {"sql": "SELECT 1", "desc": "a thing", "restrict": {"actual": "K"}}
