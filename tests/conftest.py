"""
tests/conftest.py — Shared fixtures for Maximo Enterprise MCP tests.
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock

# Set test environment variables before importing settings
os.environ.setdefault("MAXIMO_URL", "https://test-maximo.example.com/maximo/oslc")
os.environ.setdefault("MAXIMO_HOST", "https://test-maximo.example.com")
os.environ.setdefault("AUTH_MODE", "basic")
os.environ.setdefault("MAXIMO_USERNAME", "testuser")
os.environ.setdefault("MAXIMO_PASSWORD", "testpass")
os.environ.setdefault("CACHE_ENABLED", "false")
os.environ.setdefault("RBAC_ENABLED", "false")
os.environ.setdefault("CURRENT_USER_ROLE", "admin")
os.environ.setdefault("AUDIT_LOG_FILE", "/tmp/test_audit.jsonl")
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture
def mock_maximo_response():
    """Standard Maximo OSLC lean response with member list."""
    def _make(members: list, total: int = None):
        return {
            "member": members,
            "totalCount": total or len(members),
            "_duration_ms": 42,
        }
    return _make


@pytest.fixture
def mock_client():
    """Mock MaximoClient that returns empty results by default."""
    client = AsyncMock()
    client.get = AsyncMock(return_value={"member": [], "totalCount": 0, "_duration_ms": 10})
    client.post = AsyncMock(return_value={"_duration_ms": 10})
    client.patch = AsyncMock(return_value={"_duration_ms": 10})
    client.delete = AsyncMock(return_value={"_duration_ms": 10})
    client.build_oslc_query = MagicMock(return_value={"lean": "1", "oslc.pageSize": 50})
    client._client = MagicMock()  # so _init_client doesn't re-run
    return client


@pytest.fixture
def sample_asset():
    return {
        "assetnum": "PUMP-001",
        "description": "Main cooling pump",
        "siteid": "BEDFORD",
        "status": "OPERATING",
        "assettype": "PRODUCTION",
        "serialnum": "SN-12345",
        "location": "PLANT-A",
        "purchaseprice": 15000.0,
        "href": "https://test-maximo.example.com/maximo/oslc/os/mxasset/100",
        "_duration_ms": 42,
    }


@pytest.fixture
def sample_workorder():
    return {
        "wonum": "WO-10001",
        "description": "Pump bearing replacement",
        "siteid": "BEDFORD",
        "status": "APPR",
        "priority": 2,
        "assetnum": "PUMP-001",
        "worktype": "CM",
        "reportdate": "2024-01-15T08:00:00+00:00",
        "href": "https://test-maximo.example.com/maximo/oslc/os/mxwo/10001",
        "_duration_ms": 35,
    }
