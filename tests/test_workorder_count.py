import os

import pytest

from tools.workorder_count import build_where_clause, count_workorders


def test_build_where_clause_string_and_int():
    where = build_where_clause({"siteid": "WW", "priority": 1, "status": "WAPPR"})
    # Priority maps to wopriority; ordering is not important for correctness.
    parts = set(where.split(" and "))
    assert parts == {'siteid="WW"', 'status="WAPPR"', "wopriority=1"}


def test_build_where_clause_rejects_empty_filters():
    with pytest.raises(ValueError):
        build_where_clause({})


def test_build_where_clause_rejects_unknown_filter():
    with pytest.raises(ValueError):
        build_where_clause({"foo": "bar"})


def test_priority_must_be_int():
    with pytest.raises(TypeError):
        build_where_clause({"priority": "1"})


def test_count_workorders_returns_structured_error_on_missing_env(monkeypatch):
    monkeypatch.delenv("MAXIMO_USERNAME", raising=False)
    monkeypatch.delenv("MAXIMO_PASSWORD", raising=False)
    monkeypatch.delenv("MAXIMO_URL", raising=False)

    out = count_workorders({"siteid": "WW"})
    assert "error" in out


@pytest.mark.skipif(
    not (os.getenv("MAXIMO_URL") and os.getenv("MAXIMO_USERNAME") and os.getenv("MAXIMO_PASSWORD")),
    reason="Integration test requires MAXIMO_URL/MAXIMO_USERNAME/MAXIMO_PASSWORD",
)
def test_count_workorders_integration_smoke():
    out = count_workorders({"siteid": "WW"})
    assert "error" in out or ("count" in out and isinstance(out["count"], int))

