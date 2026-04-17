"""
tests/test_workorders.py — Unit tests for work order tools.
"""

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_create_workorder_success(mock_client, sample_workorder):
    """create_workorder posts to Maximo and returns result."""
    mock_client.post.return_value = {**sample_workorder, "_duration_ms": 80}
    with patch("tools.workorders.get_connected_client", return_value=mock_client), \
         patch("tools.workorders.get_cache") as mc, \
         patch("tools.workorders.get_audit_logger") as mal:
        mc.return_value = AsyncMock(invalidate=AsyncMock())
        mal.return_value = AsyncMock(record=AsyncMock())

        from tools.workorders import create_workorder
        result = await create_workorder(
            description="Pump leak repair",
            asset_num="PUMP-001",
            site_id="BEDFORD",
            priority=2,
        )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_create_workorder_validation():
    """create_workorder fails when required fields missing."""
    from tools.workorders import create_workorder
    result = await create_workorder(description="", asset_num="PUMP-001", site_id="BEDFORD")
    assert result["success"] is False
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_create_workorder_invalid_priority():
    """create_workorder fails with priority out of range."""
    from tools.workorders import create_workorder
    result = await create_workorder(
        description="Test", asset_num="PUMP-001", site_id="BEDFORD", priority=99
    )
    assert result["success"] is False
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_get_workorder_kpis(mock_client):
    """get_workorder_kpis returns structured KPI data."""
    now = datetime.now(timezone.utc)
    wos = [
        {"status": "COMP", "priority": "1", "worktype": "CM",
         "reportdate": (now - timedelta(days=10)).isoformat(), "actfinish": (now - timedelta(days=9)).isoformat(),
         "schedfinish": (now - timedelta(days=9)).isoformat(), "actlabhrs": 8, "actlabcost": 500, "assetnum": "P1"},
        {"status": "WAPPR", "priority": "2", "worktype": "CM",
         "reportdate": (now - timedelta(days=8)).isoformat(), "schedfinish": (now - timedelta(days=1)).isoformat(),
         "actlabhrs": 0, "actlabcost": 0, "assetnum": "P1"},
    ]
    with patch("tools.workorders.query_object_structure", new=AsyncMock(return_value={"data": wos, "totalCount": 2})):
        from tools.workorders import get_workorder_kpis
        result = await get_workorder_kpis("BEDFORD", period_months=3)

    assert result["success"] is True
    data = result["data"]
    assert data["total_workorders"] == 2
    assert data["completed"] == 1
    assert data["backlog"] == 1
    assert data["overdue"] == 1  # WAPPR with past schedfinish


@pytest.mark.asyncio
async def test_approve_workorder_not_found(mock_client):
    """approve_workorder returns NOT_FOUND for missing WO."""
    mock_client.get.return_value = {"member": [], "_duration_ms": 10}
    with patch("tools.workorders.get_connected_client", return_value=mock_client), \
         patch("tools.workorders.get_audit_logger") as mal:
        mal.return_value = AsyncMock(record=AsyncMock())

        from tools.workorders import approve_workorder
        result = await approve_workorder("NONEXISTENT", "BEDFORD")

    assert result["success"] is False
    assert result["error_code"] == "NOT_FOUND"
