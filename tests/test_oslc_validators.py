"""
tests/test_oslc_validators.py — Adversarial coverage for the OSLC clause
validators that gate LLM-produced output in tools.ai_intelligence.

These guard the only path where free-text from a language model becomes an
OSLC parameter. A regression here is a SQL-injection-class bug.
"""

import pytest

from core.oslc_utils import (
    validate_oslc_orderby,
    validate_oslc_select,
    validate_oslc_where,
)


# ── validate_oslc_where ──────────────────────────────────────────────────────

class TestWhereValid:
    @pytest.mark.parametrize("clause", [
        "",
        'siteid="BEDFORD"',
        'siteid="BEDFORD" and status="WAPPR"',
        "priority=1",
        'status in ["WAPPR","APPR","INPRG"]',
        '(siteid="A" or siteid="B") and status="OPEN"',
        'assetnum="PUMP-001" and reportdate>="2024-01-01T00:00:00+00:00"',
        'description="quote\\"inside"',
        'not status="CLOSE"',
        'siteid="X" and (priority<=3 or priority>=8)',
    ])
    def test_accepts_valid_clauses(self, clause):
        assert validate_oslc_where(clause) == clause


class TestWhereAdversarial:
    @pytest.mark.parametrize("clause,why", [
        ('siteid="X"; drop wo', "semicolon"),
        ('siteid="X" -- ignore', "sql comment --"),
        ('siteid="X" /* hidden */', "block comment"),
        ('siteid="X" \x00 trailing', "null byte"),
        ('siteid="unterminated', "unterminated string literal"),
        ('1=1', "identifier may not start with digit"),
        ('siteid="X" and (priority=1', "unbalanced parenthesis"),
        ('siteid="X" and priority=1)', "extra closing parenthesis"),
        ('status in ["A", "B"', "unbalanced bracket"),
        ('site|id="X"', "pipe is not a permitted character"),
        ('siteid$="X"', "dollar sign is not a permitted character"),
        ('siteid=`X`', "backticks not permitted"),
    ])
    def test_rejects_adversarial_clauses(self, clause, why):
        with pytest.raises(ValueError):
            validate_oslc_where(clause)

    def test_length_cap(self):
        long_clause = 'siteid="' + ("A" * 2000) + '"'
        with pytest.raises(ValueError, match="too long"):
            validate_oslc_where(long_clause, max_length=1000)


# ── validate_oslc_select ─────────────────────────────────────────────────────

class TestSelectValid:
    @pytest.mark.parametrize("clause", [
        "",
        "*",
        "wonum",
        "wonum,description,status",
        "wonum, description , status",
        "asset.assetnum,workorder.wonum",
    ])
    def test_accepts_valid_select(self, clause):
        assert validate_oslc_select(clause) == clause


class TestSelectAdversarial:
    @pytest.mark.parametrize("clause", [
        'wonum;drop',
        'wonum--comment',
        'wonum /* hidden */, status',
        'wonum,',
        ',wonum',
        'wonum, , status',
        'wo num',
        'wo-num',
        'wonum=1',
    ])
    def test_rejects_adversarial_select(self, clause):
        with pytest.raises(ValueError):
            validate_oslc_select(clause)


# ── validate_oslc_orderby ────────────────────────────────────────────────────

class TestOrderByValid:
    @pytest.mark.parametrize("clause", [
        "",
        "reportdate",
        "+reportdate",
        "-reportdate",
        "asset.changedate",
    ])
    def test_accepts_valid_orderby(self, clause):
        assert validate_oslc_orderby(clause) == clause


class TestOrderByAdversarial:
    @pytest.mark.parametrize("clause", [
        "reportdate, wonum",          # multi-field smuggle via comma
        "reportdate;",
        "reportdate--",
        "reportdate /* */",
        "report date",
        "report-date",
        "*",                          # wildcard not allowed for orderBy
    ])
    def test_rejects_adversarial_orderby(self, clause):
        with pytest.raises(ValueError):
            validate_oslc_orderby(clause)
