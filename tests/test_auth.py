"""
tests/test_auth.py — Authentication and settings tests.
"""

import pytest


def test_settings_basic_auth():
    """Settings load correctly for basic auth mode."""
    from core.settings import Settings
    s = Settings(
        MAXIMO_URL="https://test.com/oslc",
        MAXIMO_HOST="https://test.com",
        AUTH_MODE="basic",
        MAXIMO_USERNAME="admin",
        MAXIMO_PASSWORD="secret",
    )
    assert s.AUTH_MODE == "basic"
    assert s.MAXIMO_USERNAME == "admin"


def test_settings_basic_auth_missing_password():
    """Settings raise error when basic auth password is missing."""
    from pydantic import ValidationError
    from core.settings import Settings
    with pytest.raises(ValidationError):
        Settings(
            MAXIMO_URL="https://test.com/oslc",
            MAXIMO_HOST="https://test.com",
            AUTH_MODE="basic",
            MAXIMO_USERNAME="admin",
            MAXIMO_PASSWORD="",  # empty = missing
        )


def test_settings_apikey_mode():
    """Settings load correctly for API key mode."""
    from core.settings import Settings
    s = Settings(
        MAXIMO_URL="https://test.com/oslc",
        MAXIMO_HOST="https://test.com",
        AUTH_MODE="apikey",
        MAXIMO_API_KEY="mykey123",
    )
    assert s.AUTH_MODE == "apikey"
    assert s.MAXIMO_API_KEY == "mykey123"


def test_settings_invalid_log_level():
    """Settings raise error for invalid log level."""
    from pydantic import ValidationError
    from core.settings import Settings
    with pytest.raises(ValidationError):
        Settings(
            MAXIMO_URL="https://test.com/oslc",
            MAXIMO_HOST="https://test.com",
            AUTH_MODE="basic",
            MAXIMO_USERNAME="a",
            MAXIMO_PASSWORD="b",
            LOG_LEVEL="VERBOSE",  # invalid
        )


def test_audit_logger_sanitizes_passwords(tmp_path):
    """Audit logger never writes passwords to log files."""
    import asyncio
    from core.audit import AuditLogger

    log_file = str(tmp_path / "test_audit.jsonl")
    logger = AuditLogger(log_file)

    asyncio.run(logger.record(
        tool_name="test_tool",
        input_params={"password": "secret123", "site_id": "BEDFORD"},
        result={"success": True},
        user_id="testuser",
        duration_ms=50,
    ))

    with open(log_file) as f:
        content = f.read()

    assert "secret123" not in content
    assert "***REDACTED***" in content
    assert "BEDFORD" in content


def test_audit_logger_query(tmp_path):
    """Audit logger query returns filtered records."""
    import asyncio
    from core.audit import AuditLogger

    log_file = str(tmp_path / "query_test.jsonl")
    logger = AuditLogger(log_file)

    async def run():
        await logger.record("create_workorder", {"site_id": "A"}, {"success": True}, "user1", 100)
        await logger.record("list_assets", {"site_id": "B"}, {"success": True}, "user2", 50)
        await logger.record("create_workorder", {"site_id": "C"}, {"success": True}, "user1", 80)
        return await logger.query(tool_name="create_workorder", limit=10)

    records = asyncio.run(run())
    assert len(records) == 2
    assert all("create_workorder" in r["tool"] for r in records)


def test_parse_response_reads_totalcount_from_responseinfo():
    """Maximo puts totalCount in responseInfo unless collectioncount=1 is omitted."""
    from core.maximo_client import MaximoClient

    data = {
        "member": [{"wonum": "1"}],
        "responseInfo": {"totalCount": 34859, "pagenum": 1},
    }
    out = MaximoClient._parse_response(data, 0)
    assert out["totalCount"] == 34859
    assert len(out["member"]) == 1


def test_build_oslc_query_collectioncount():
    from core.maximo_client import MaximoClient

    p = MaximoClient().build_oslc_query(
        where='status="WSCH"', page_size=1, collectioncount=1
    )
    assert p["collectioncount"] == 1
