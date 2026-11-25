"""Unit tests: helpers behind the Requirements tab (rule checking, display and defaults)."""

import pytest

from app.services import mapping as mapping_svc
from app.services.target_spec import SpecError, _check_expression, _lit, _strip_default, _summary


@pytest.fixture
def ctx():
    """A mapping context for two sources, built without a database."""
    c = object.__new__(mapping_svc.Ctx)
    c.cols = {"customers": ["CUSTOMER_ID", "NAME"], "transactions": ["CUSTOMER_ID", "AMOUNT", "DISCOUNT"]}
    c.types = {}
    return c


def test_check_expression_qualifies_columns_and_reads_aggregation_and_precision(ctx):
    q, srcs, agg, precision = _check_expression("ROUND(SUM(AMOUNT - COALESCE(DISCOUNT, 0)), 2);", ctx)
    assert "transactions.AMOUNT" in q and "transactions.DISCOUNT" in q
    assert srcs == ["transactions.AMOUNT", "transactions.DISCOUNT"] and agg is True and precision == 2
    q, srcs, agg, precision = _check_expression("upper(trim(customers.name))", ctx)
    assert srcs == ["customers.NAME"] and agg is False and precision is None


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("", "Enter the rule"),
        ("SUM(", "not a valid SQL expression"),
        ("(SELECT 1)", "not a query"),
        ("SUM(NOPE)", "not found"),
        ("CUSTOMER_ID", "multiple sources"),
        ("x" * 4001, "too long"),
    ],
)
def test_check_expression_rejects_bad_rules_with_a_clear_message(ctx, expr, message):
    with pytest.raises(SpecError, match=message):
        _check_expression(expr, ctx)


def test_strip_default_shows_the_rule_without_the_unmatched_default():
    assert _strip_default("COALESCE(SUM(t.A), 0)", 0) == "SUM(t.A)"
    assert _strip_default("COALESCE(t.S, 'N/A')", "N/A") == "t.S"
    assert _strip_default("COALESCE(SUM(t.A), 1)", 0) == "COALESCE(SUM(t.A), 1)"  # a different default is part of the rule
    assert _strip_default("SUM(t.A)", None) == "SUM(t.A)"


@pytest.mark.parametrize(
    ("mtype", "srcs", "group", "text"),
    [
        ("DIRECT", ["c.ID"], [], "Copied as-is from c.ID"),
        ("RENAMED", ["c.X"], [], "Copied from c.X (renamed)"),
        ("AGGREGATED", ["t.A"], ["ID"], "Aggregated from t.A, per ID"),
        ("CONSTANT", [], [], "Constant value"),
        ("CONDITIONAL", ["c.T"], [], "Conditional (CASE) on c.T"),
        ("CONCATENATION", ["c.A", "c.B"], [], "Concatenation of c.A, c.B"),
        ("DERIVED", [], [], "Calculated"),
    ],
)
def test_summary_describes_how_a_column_is_built(mtype, srcs, group, text):
    assert _summary(mtype, "expr", srcs, group) == text


def test_literals_are_quoted_safely():
    assert _lit(5) == "5" and _lit("12.5") == "12.5"
    assert _lit("COMPLETED") == "'COMPLETED'" and _lit("O'Brien") == "'O''Brien'"
