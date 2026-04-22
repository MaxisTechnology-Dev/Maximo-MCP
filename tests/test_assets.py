"""
tests/test_assets.py — Unit tests for asset tools.
"""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_list_assets_no_filters(mock_client, mock_maximo_response, sample_asset):
    """list_assets with no filters returns paginated assets."""
    mock_client.get.return_value = mock_maximo_response([sample_asset])
    with patch("core.maximo_client.get_connected_client", return_value=mock_client), \
         patch("core.cache.get_cache") as mock_cache_factory:
        mock_cache = AsyncMock()
        mock_cache.get_or_fetch = AsyncMock(
            return_value=(mock_maximo_response([sample_asset]), False)
        )
        mock_cache_factory.return_value = mock_cache

        from tools.assets import list_assets
        result = await list_assets(site_id="BEDFORD")

    assert result["success"] is True
    assert "assets" in result["data"]
    assert result["metadata"]["cached"] is False


@pytest.mark.asyncio
async def test_get_asset_found(mock_client, mock_maximo_response, sample_asset):
    """get_asset returns asset when found."""
    with patch("tools.assets.get_cache") as mock_cache_factory:
        mock_cache = AsyncMock()
        mock_cache.get_or_fetch = AsyncMock(
            return_value=(
                {
                    "data": [sample_asset],
                    "totalCount": 1,
                    "object_structure": "mxasset",
                    "entity": "asset",
                    "filters": {},
                },
                False,
            )
        )
        mock_cache_factory.return_value = mock_cache

        from tools.assets import get_asset
        result = await get_asset("PUMP-001", "BEDFORD")

    assert result["success"] is True
    assert result["data"]["assetnum"] == "PUMP-001"


@pytest.mark.asyncio
async def test_get_asset_not_found(mock_client, mock_maximo_response):
    """get_asset returns NOT_FOUND error when asset doesn't exist."""
    with patch("tools.assets.get_connected_client", return_value=mock_client), \
         patch("tools.assets.get_cache") as mock_cache_factory:
        mock_cache = AsyncMock()
        mock_cache.get_or_fetch = AsyncMock(
            return_value=(mock_maximo_response([]), False)
        )
        mock_cache_factory.return_value = mock_cache

        from tools.assets import get_asset
        result = await get_asset("NONEXISTENT", "BEDFORD")

    assert result["success"] is False
    assert result["error_code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_asset_missing_params():
    """get_asset returns VALIDATION_ERROR when required params missing."""
    from tools.assets import get_asset
    result = await get_asset("", "BEDFORD")
    assert result["success"] is False
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_get_asset_downtime_stats(mock_client, mock_maximo_response, sample_asset):
    """get_asset_downtime_stats computes MTTR/MTBF from WO history."""
    wos = [
        {
            "wonum": f"WO-{i}",
            "worktype": "CM",
            "actfinish": "2024-01-20T10:00:00+00:00",
            "actlabhrs": 4.0,
            "reportdate": "2024-01-15T08:00:00+00:00",
            "status": "COMP",
        }
        for i in range(3)
    ]

    with patch("tools.assets.get_asset_history") as mock_history:
        mock_history.return_value = {
            "success": True,
            "data": {"work_orders": wos, "asset_num": "PUMP-001", "site_id": "BEDFORD", "lookback_days": 360},
            "metadata": {},
        }
        from tools.assets import get_asset_downtime_stats
        result = await get_asset_downtime_stats("PUMP-001", "BEDFORD", period_months=12)

    assert result["success"] is True
    assert result["data"]["total_failures"] == 3
    assert "mttr_hours" in result["data"]
    assert "mtbf_hours" in result["data"]
    assert 0 <= result["data"]["availability_pct"] <= 100
