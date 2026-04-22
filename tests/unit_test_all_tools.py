"""
tests/unit_test_all_tools.py — Broad unit coverage for the stable MCP tool surface.

Each test:
  - Calls the tool function directly (not via MCP protocol)
  - Mocks all Maximo HTTP calls — no real network calls are made
  - Asserts:  (1) a normalized response envelope is returned
              (2) No unhandled Python exception is raised

Run with:
    pytest tests/unit_test_all_tools.py -v
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# SHARED MOCK DATA — realistic Maximo OSLC lean=1 responses
# ─────────────────────────────────────────────────────────────────────────────

MOCK_ASSET_MEMBER = {
    "assetnum": "P-101",
    "description": "Test Pump",
    "siteid": "BEDFORD",
    "status": "OPERATING",
    "assettype": "PRODUCTION",
    "serialnum": "SN-12345",
    "location": "PLANT-A",
    "purchaseprice": 15000.0,
    "href": "http://test/oslc/os/mxasset/100",
    "_duration_ms": 10,
}

MOCK_OSLC_LIST = {
    "member": [MOCK_ASSET_MEMBER],
    "totalCount": 1,
    "_duration_ms": 10,
}

MOCK_WO_MEMBER = {
    "wonum": "WO10001",
    "description": "Pump bearing replacement",
    "siteid": "BEDFORD",
    "status": "APPR",
    "wopriority": 3,
    "priority": 3,
    "worktype": "CM",
    "reportdate": "2024-01-15T08:00:00+00:00",
    "actfinish": "2024-01-16T10:00:00+00:00",
    "actlabhrs": 4.0,
    "actlabcost": 200.0,
    "schedfinish": "2099-01-20T10:00:00+00:00",
    "assetnum": "P-101",
    "href": "http://test/oslc/os/mxwo/10001",
    "_duration_ms": 10,
}

MOCK_WO = {
    "member": [MOCK_WO_MEMBER],
    "totalCount": 1,
    "_duration_ms": 10,
}

MOCK_PM_MEMBER = {
    "pmnum": "PM-001",
    "description": "Quarterly pump inspection",
    "siteid": "BEDFORD",
    "status": "ACTIVE",
    "assetnum": "P-101",
    "frequency": 90,
    "frequnit": "DAYS",
    "nextduedate": "2099-06-01T00:00:00+00:00",
    "lastcompdate": "2024-01-01T00:00:00+00:00",
    "estlabhrs": 2.0,
    "href": "http://test/oslc/os/mxpm/1",
    "_duration_ms": 10,
}

MOCK_PM = {
    "member": [MOCK_PM_MEMBER],
    "totalCount": 1,
    "_duration_ms": 10,
}

MOCK_INV_MEMBER = {
    "itemnum": "BOLT-M10",
    "description": "M10 Bolt",
    "storeloc": "CENTRAL",
    "siteid": "BEDFORD",
    "curbal": 5.0,
    "reorderpoint": 20.0,
    "minlevel": 10.0,
    "maxlevel": 200.0,
    "orderqty": 50.0,
    "stdcost": 0.50,
    "issueunit": "EA",
    "_duration_ms": 10,
}

MOCK_INV = {
    "member": [MOCK_INV_MEMBER],
    "totalCount": 1,
    "_duration_ms": 10,
}

MOCK_PO_MEMBER = {
    "ponum": "PO-1001",
    "vendor": "ACME",
    "siteid": "BEDFORD",
    "status": "COMP",
    "orderdate": "2024-01-01T00:00:00+00:00",
    "requireddate": "2024-02-15T00:00:00+00:00",
    "poline": [{"polinenum": 1, "itemnum": "BOLT-M10", "orderqty": 50.0}],
    "_duration_ms": 10,
}

MOCK_PO = {
    "member": [MOCK_PO_MEMBER],
    "totalCount": 1,
    "_duration_ms": 10,
}

MOCK_LABOR_MEMBER = {
    "laborcode": "JOHN",
    "personid": "JOHND",
    "siteid": "BEDFORD",
    "craft": "MECHANIC",
    "status": "ACTIVE",
    "_duration_ms": 10,
}

MOCK_LABOR = {
    "member": [MOCK_LABOR_MEMBER],
    "totalCount": 1,
    "_duration_ms": 10,
}

MOCK_LOC_MEMBER = {
    "location": "PLANT-A",
    "description": "Plant A",
    "siteid": "BEDFORD",
    "type": "OPERATING",
    "parent": None,
    "status": "OPERATING",
    "_duration_ms": 10,
}

MOCK_LOC = {
    "member": [MOCK_LOC_MEMBER],
    "totalCount": 1,
    "_duration_ms": 10,
}

MOCK_USER_MEMBER = {
    "personid": "JOHND",
    "displayname": "John Doe",
    "status": "ACTIVE",
    "defsite": "BEDFORD",
    "_duration_ms": 10,
}

MOCK_USER = {
    "member": [MOCK_USER_MEMBER],
    "totalCount": 1,
    "_duration_ms": 10,
}

MOCK_POST_RESULT = {"_duration_ms": 10, "wonum": "WO10002"}
MOCK_PATCH_RESULT = {"_duration_ms": 10}

# query_object_structure success envelope
QUERY_ASSET_RESULT = {
    "data": [MOCK_ASSET_MEMBER],
    "totalCount": 1,
    "object_structure": "mxasset",
    "entity": "asset",
    "filters": {},
    "_duration_ms": 10,
}
QUERY_WO_RESULT = {
    "data": [MOCK_WO_MEMBER],
    "totalCount": 1,
    "object_structure": "mxwo",
    "entity": "workorder",
    "filters": {},
    "_duration_ms": 10,
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def make_cache(return_data):
    """Return a mock CacheClient whose get_or_fetch returns (return_data, False)."""
    cache = MagicMock()
    cache.get_or_fetch = AsyncMock(return_value=(return_data, False))
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=None)
    cache.invalidate = AsyncMock(return_value=None)
    return cache


def make_client(get_data=None, post_data=None, patch_data=None, get_side_effect=None):
    """Return a mock MaximoClient."""
    c = MagicMock()
    if get_side_effect:
        c.get = AsyncMock(side_effect=get_side_effect)
    else:
        c.get = AsyncMock(
            return_value=get_data or {"member": [], "totalCount": 0, "_duration_ms": 10}
        )
    c.post = AsyncMock(return_value=post_data or MOCK_POST_RESULT)
    c.patch = AsyncMock(return_value=patch_data or MOCK_PATCH_RESULT)
    c.build_oslc_query = MagicMock(return_value={"lean": "1", "oslc.pageSize": 50})
    c._request = AsyncMock(return_value={"_duration_ms": 10})
    return c


def make_audit():
    """Return a mock AuditLogger."""
    audit = MagicMock()
    audit.record = AsyncMock(return_value=None)
    audit.query = AsyncMock(return_value=[])
    return audit


# ─────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset rate-limiter singleton before each test to prevent exhaustion."""
    from core import rate_limiter as rl_mod
    rl_mod._limiter_instance = None
    yield
    rl_mod._limiter_instance = None


# ═════════════════════════════════════════════════════════════════════════════
# 1.  HEALTH CHECK  (server.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_check():
    mock_cache_obj = MagicMock()
    mock_cache_obj.get_status = AsyncMock(
        return_value={"backend": "in-memory", "healthy": True, "cache_enabled": True}
    )

    client = make_client(get_data={"userName": "maxadmin", "maximoVersion": "7.6.1"})

    with patch("core.maximo_client.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("core.cache.get_cache", return_value=mock_cache_obj):
        from server import health_check
        result = await health_check()

    assert result["success"] is True
    assert result["data"]["maximo_health"]["connected"] is True


# ═════════════════════════════════════════════════════════════════════════════
# 2.  ASSETS (tools/assets.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_assets():
    from tools.assets import list_assets
    with patch("tools.assets.get_cache", return_value=make_cache(QUERY_ASSET_RESULT)):
        result = await list_assets(site_id="BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_asset():
    from tools.assets import get_asset
    with patch("tools.assets.get_cache", return_value=make_cache(MOCK_OSLC_LIST)):
        result = await get_asset("P-101", "BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_create_asset():
    from tools.assets import create_asset
    client = make_client(post_data=MOCK_POST_RESULT)
    with patch("tools.assets.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.assets.get_cache", return_value=make_cache({})), \
         patch("tools.assets.get_audit_logger", return_value=make_audit()):
        result = await create_asset("P-999", "New pump", "BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_update_asset():
    from tools.assets import update_asset
    client = make_client(get_data=MOCK_OSLC_LIST, patch_data=MOCK_PATCH_RESULT)
    with patch("tools.assets.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.assets.get_cache", return_value=make_cache({})), \
         patch("tools.assets.get_audit_logger", return_value=make_audit()):
        result = await update_asset("P-101", "BEDFORD", description="Updated pump")
    assert "success" in result


@pytest.mark.asyncio
async def test_retire_asset():
    from tools.assets import retire_asset
    client = make_client(get_data=MOCK_OSLC_LIST, patch_data=MOCK_PATCH_RESULT)
    with patch("tools.assets.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.assets.get_cache", return_value=make_cache({})), \
         patch("tools.assets.get_audit_logger", return_value=make_audit()):
        result = await retire_asset("P-101", "BEDFORD", reason="End of life")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_asset_history():
    from tools.assets import get_asset_history
    client = make_client(get_data=MOCK_WO)
    with patch("tools.assets.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await get_asset_history("P-101", "BEDFORD", lookback_days=90)
    assert "success" in result


@pytest.mark.asyncio
async def test_get_asset_downtime_stats():
    from tools.assets import get_asset_downtime_stats
    history_response = {
        "success": True,
        "data": {
            "asset_num": "P-101",
            "site_id": "BEDFORD",
            "lookback_days": 360,
            "work_orders": [
                {
                    "wonum": f"WO-{i}",
                    "worktype": "CM",
                    "actfinish": "2024-01-20T10:00:00+00:00",
                    "actlabhrs": 4.0,
                    "status": "COMP",
                }
                for i in range(3)
            ],
        },
        "metadata": {},
    }
    with patch("tools.assets.get_asset_history", new_callable=AsyncMock, return_value=history_response):
        result = await get_asset_downtime_stats("P-101", "BEDFORD", period_months=12)
    assert "success" in result


@pytest.mark.asyncio
async def test_search_assets():
    from tools.assets import search_assets
    client = make_client(get_data=MOCK_OSLC_LIST)
    with patch("tools.assets.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await search_assets("pump", site_id="BEDFORD")
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 3.  WORK ORDERS (tools/workorders.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_workorders():
    from tools.workorders import list_workorders
    with patch("tools.workorders.get_cache", return_value=make_cache(QUERY_WO_RESULT)):
        result = await list_workorders(site_id="BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_workorder():
    from tools.workorders import get_workorder
    with patch("tools.workorders.get_cache", return_value=make_cache(MOCK_WO)):
        result = await get_workorder("WO10001", "BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_create_workorder():
    from tools.workorders import create_workorder
    client = make_client(post_data=MOCK_POST_RESULT)
    with patch("tools.workorders.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.workorders.get_cache", return_value=make_cache({})), \
         patch("tools.workorders.get_audit_logger", return_value=make_audit()):
        result = await create_workorder("Bearing failure", "P-101", "BEDFORD", priority=2)
    assert "success" in result


@pytest.mark.asyncio
async def test_update_workorder():
    from tools.workorders import update_workorder
    client = make_client(get_data=MOCK_WO, patch_data=MOCK_PATCH_RESULT)
    with patch("tools.workorders.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.workorders.get_cache", return_value=make_cache({})), \
         patch("tools.workorders.get_audit_logger", return_value=make_audit()):
        result = await update_workorder("WO10001", "BEDFORD", priority=1)
    assert "success" in result


@pytest.mark.asyncio
async def test_approve_workorder():
    from tools.workorders import approve_workorder
    client = make_client(get_data=MOCK_WO, patch_data=MOCK_PATCH_RESULT)
    with patch("tools.workorders.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.workorders.get_cache", return_value=make_cache({})), \
         patch("tools.workorders.get_audit_logger", return_value=make_audit()):
        result = await approve_workorder("WO10001", "BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_assign_technician():
    from tools.workorders import assign_technician
    client = make_client(get_data=MOCK_WO, patch_data=MOCK_PATCH_RESULT)
    with patch("tools.workorders.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.workorders.get_cache", return_value=make_cache({})), \
         patch("tools.workorders.get_audit_logger", return_value=make_audit()):
        result = await assign_technician("WO10001", "BEDFORD", "JOHN", craft="MECHANIC")
    assert "success" in result


@pytest.mark.asyncio
async def test_close_workorder():
    from tools.workorders import close_workorder
    client = make_client(get_data=MOCK_WO, patch_data=MOCK_PATCH_RESULT)
    with patch("tools.workorders.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.workorders.get_cache", return_value=make_cache({})), \
         patch("tools.workorders.get_audit_logger", return_value=make_audit()):
        result = await close_workorder("WO10001", "BEDFORD", actual_hours=4.0)
    assert "success" in result


@pytest.mark.asyncio
async def test_cancel_workorder():
    from tools.workorders import cancel_workorder
    client = make_client(get_data=MOCK_WO, patch_data=MOCK_PATCH_RESULT)
    with patch("tools.workorders.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.workorders.get_cache", return_value=make_cache({})), \
         patch("tools.workorders.get_audit_logger", return_value=make_audit()):
        result = await cancel_workorder("WO10001", "BEDFORD", reason="Duplicate")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_workorder_kpis():
    from tools.workorders import get_workorder_kpis
    client = make_client(get_data=MOCK_WO)
    with patch("tools.workorders.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await get_workorder_kpis("BEDFORD", period_months=3)
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 4.  PM SCHEDULING (tools/pm_scheduling.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_pm_schedules():
    from tools.pm_scheduling import list_pm_schedules
    with patch("tools.pm_scheduling.get_cache", return_value=make_cache(MOCK_PM)), \
         patch("tools.pm_scheduling._get_pm_os", new_callable=AsyncMock, return_value="/os/mxpm"):
        result = await list_pm_schedules("BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_generate_pm_workorders():
    from tools.pm_scheduling import generate_pm_workorders
    client = make_client(post_data={"message": "PM generation triggered", "_duration_ms": 10})
    with patch("tools.pm_scheduling.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.pm_scheduling._get_pm_os", new_callable=AsyncMock, return_value="/os/mxpm"), \
         patch("tools.pm_scheduling.get_cache", return_value=make_cache({})), \
         patch("tools.pm_scheduling.get_audit_logger", return_value=make_audit()):
        result = await generate_pm_workorders("BEDFORD", date_range_days=30)
    assert "success" in result


@pytest.mark.asyncio
async def test_get_pm_forecast():
    from tools.pm_scheduling import get_pm_forecast
    client = make_client(get_data=MOCK_PM)
    with patch("tools.pm_scheduling.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.pm_scheduling._get_pm_os", new_callable=AsyncMock, return_value="/os/mxpm"):
        result = await get_pm_forecast("BEDFORD", months_ahead=3)
    assert "success" in result


@pytest.mark.asyncio
async def test_update_pm_frequency():
    from tools.pm_scheduling import update_pm_frequency
    client = make_client(get_data=MOCK_PM, patch_data=MOCK_PATCH_RESULT)
    with patch("tools.pm_scheduling.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.pm_scheduling._get_pm_os", new_callable=AsyncMock, return_value="/os/mxpm"), \
         patch("tools.pm_scheduling.get_cache", return_value=make_cache({})), \
         patch("tools.pm_scheduling.get_audit_logger", return_value=make_audit()):
        result = await update_pm_frequency("PM-001", "BEDFORD", frequency=90, frequency_unit="DAYS")
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 5.  INVENTORY (tools/inventory.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_check_stock_level():
    from tools.inventory import check_stock_level
    with patch("tools.inventory.get_cache", return_value=make_cache(MOCK_INV)):
        result = await check_stock_level("BOLT-M10", "CENTRAL", "BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_list_low_stock_items():
    from tools.inventory import list_low_stock_items
    client = make_client(get_data=MOCK_INV)
    with patch("tools.inventory.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await list_low_stock_items("BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_create_material_request():
    from tools.inventory import create_material_request
    client = make_client(post_data={"_duration_ms": 10, "mrnum": "MR-001"})
    with patch("tools.inventory.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.inventory.get_audit_logger", return_value=make_audit()):
        result = await create_material_request(
            items=[{"itemnum": "BOLT-M10", "quantity": 50}],
            destination_storeroom="CENTRAL",
            site_id="BEDFORD",
        )
    assert "success" in result


@pytest.mark.asyncio
async def test_transfer_inventory():
    from tools.inventory import transfer_inventory
    client = make_client(post_data={"_duration_ms": 10})
    with patch("tools.inventory.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.inventory.get_audit_logger", return_value=make_audit()):
        result = await transfer_inventory("BOLT-M10", "CENTRAL", "ANNEX", 25.0, "BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_reorder_recommendations():
    from tools.inventory import get_reorder_recommendations
    client = make_client(get_data=MOCK_INV)
    with patch("tools.inventory.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await get_reorder_recommendations("BEDFORD")
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 6.  PURCHASING (tools/purchasing.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_purchase_order():
    from tools.purchasing import create_purchase_order
    client = make_client(post_data={"_duration_ms": 10, "ponum": "PO-1002"})
    with patch("tools.purchasing.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.purchasing.get_audit_logger", return_value=make_audit()):
        result = await create_purchase_order(
            vendor_id="ACME",
            items=[{"itemnum": "BOLT-M10", "quantity": 100, "unitcost": 0.50}],
            site_id="BEDFORD",
        )
    assert "success" in result


@pytest.mark.asyncio
async def test_get_purchase_order():
    from tools.purchasing import get_purchase_order
    with patch("tools.purchasing.get_cache", return_value=make_cache(MOCK_PO)):
        result = await get_purchase_order("PO-1001", "BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_receive_items():
    from tools.purchasing import receive_items
    client = make_client(post_data={"_duration_ms": 10})
    with patch("tools.purchasing.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.purchasing.get_cache", return_value=make_cache({})), \
         patch("tools.purchasing.get_audit_logger", return_value=make_audit()):
        result = await receive_items(
            "PO-1001",
            "BEDFORD",
            received_lines=[{"polinenum": 1, "receivedqty": 50, "storeroom": "CENTRAL"}],
        )
    assert "success" in result


@pytest.mark.asyncio
async def test_get_vendor_performance():
    from tools.purchasing import get_vendor_performance
    client = make_client(get_data=MOCK_PO)
    with patch("tools.purchasing.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await get_vendor_performance("ACME", period_months=12)
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 7.  AI INTELLIGENCE (tools/ai_intelligence.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_nl_to_oslc_query():
    from tools.ai_intelligence import nl_to_oslc_query
    # No HTTP calls when dry_run=False and no OpenAI key
    result = await nl_to_oslc_query(
        "show overdue work orders in BEDFORD with priority 1", dry_run=False
    )
    assert "success" in result


@pytest.mark.asyncio
async def test_detect_asset_anomalies():
    from tools.ai_intelligence import detect_asset_anomalies
    client = make_client(get_data=MOCK_WO)
    with patch("tools.ai_intelligence.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await detect_asset_anomalies("P-101", "BEDFORD", lookback_days=90)
    assert "success" in result


@pytest.mark.asyncio
async def test_suggest_root_cause():
    from tools.ai_intelligence import suggest_root_cause
    client = make_client(get_data=MOCK_WO)
    with patch("tools.ai_intelligence.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await suggest_root_cause("P-101", "BEDFORD", "Vibration and noise from bearing")
    assert "success" in result


@pytest.mark.asyncio
async def test_summarize_asset_health():
    from tools.ai_intelligence import summarize_asset_health
    # client.get called twice: once for WOs, once for PMs
    client = make_client(get_side_effect=[MOCK_WO, MOCK_PM])
    with patch("tools.ai_intelligence.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await summarize_asset_health("P-101", "BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_search_maximo_knowledge():
    from tools.ai_intelligence import search_maximo_knowledge
    mock_rag = MagicMock()
    mock_rag._ready = True
    mock_rag._collection = MagicMock()
    mock_rag._collection.count = MagicMock(return_value=5)
    mock_rag.search = AsyncMock(
        return_value=[
            {"text": "Maximo work order lifecycle", "source": "docs", "score": 0.92}
        ]
    )
    with patch("tools.ai_intelligence.get_rag_engine", return_value=mock_rag):
        result = await search_maximo_knowledge("work order approval process")
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 8.  REPORTING (tools/reporting.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_maintenance_kpi_dashboard():
    from tools.reporting import get_maintenance_kpi_dashboard
    # client.get called twice: WOs first, PMs second
    client = make_client(get_side_effect=[MOCK_WO, MOCK_PM])
    with patch("tools.reporting.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await get_maintenance_kpi_dashboard("BEDFORD", period_months=3)
    assert "success" in result


@pytest.mark.asyncio
async def test_export_workorders_excel():
    from tools.reporting import export_workorders_excel
    client = make_client(get_data=MOCK_WO)
    with patch("tools.reporting.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await export_workorders_excel("BEDFORD", max_records=10)
    # Either succeeds (openpyxl installed) or returns DEPENDENCY_ERROR — both are valid envelopes
    assert "success" in result


@pytest.mark.asyncio
async def test_export_asset_report_pdf():
    from tools.reporting import export_asset_report_pdf
    client = make_client(get_data=MOCK_OSLC_LIST)
    with patch("tools.reporting.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await export_asset_report_pdf("BEDFORD", max_records=10)
    # Either succeeds (reportlab installed) or returns DEPENDENCY_ERROR
    assert "success" in result


@pytest.mark.asyncio
async def test_generate_carbon_table():
    from tools.reporting import generate_carbon_table
    result = await generate_carbon_table(
        object_structure="mxwo",
        data=[{"wonum": "WO10001", "status": "APPR"}],
        columns=[{"key": "wonum", "header": "WO Number"}, {"key": "status", "header": "Status"}],
    )
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 9.  ADMIN (tools/admin.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_users():
    from tools.admin import list_users
    with patch("tools.admin.get_cache", return_value=make_cache({"member": [MOCK_USER_MEMBER], "totalCount": 1})):
        result = await list_users(site_id="BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_user():
    from tools.admin import get_user
    with patch("tools.admin.get_cache", return_value=make_cache(MOCK_USER)):
        result = await get_user("JOHND")
    assert "success" in result


@pytest.mark.asyncio
async def test_list_security_groups():
    from tools.admin import list_security_groups
    mock_groups = {
        "member": [{"groupname": "MAXADMIN", "description": "Maximo Admins", "grouptype": "USER", "groupuser": []}],
        "totalCount": 1,
        "_duration_ms": 10,
    }
    with patch("tools.admin.get_cache", return_value=make_cache(mock_groups)):
        result = await list_security_groups()
    assert "success" in result


@pytest.mark.asyncio
async def test_query_audit_log():
    from tools.admin import query_audit_log
    with patch("tools.admin.get_audit_logger", return_value=make_audit()):
        result = await query_audit_log(tool_name="create_workorder", limit=10)
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 10. SCHEMA / DEV TOOLS (tools/schema_dev.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_object_structures():
    from tools.schema_dev import list_object_structures
    mock_os_list = {
        "member": [{"intobjectname": "MXASSET", "description": "Asset object structure"}],
        "totalCount": 1,
        "_duration_ms": 10,
    }
    with patch("tools.schema_dev.get_cache", return_value=make_cache(mock_os_list)):
        result = await list_object_structures(filter_keyword="asset")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_schema_details():
    from tools.schema_dev import get_schema_details
    # The cache returns the structured metadata result
    cached_meta = {
        "_source": "describe",
        "payload": {
            "properties": {
                "assetnum": {"type": "string", "title": "Asset Number"},
                "siteid": {"type": "string", "title": "Site ID"},
                "status": {"type": "string", "title": "Status"},
            }
        },
    }
    with patch("tools.schema_dev.get_cache", return_value=make_cache(cached_meta)):
        result = await get_schema_details("mxasset", include_relationships=False)
    assert "success" in result


@pytest.mark.asyncio
async def test_validate_oslc_query():
    from tools.schema_dev import validate_oslc_query
    client = make_client(get_data=MOCK_OSLC_LIST)
    with patch("tools.schema_dev.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await validate_oslc_query(
            "mxasset",
            where_clause='siteid="BEDFORD"',
            select_clause="assetnum,description",
        )
    assert "success" in result


@pytest.mark.asyncio
async def test_generate_api_code():
    from tools.schema_dev import generate_api_code
    # No HTTP calls — pure code generation
    result = await generate_api_code(
        object_structure="mxasset",
        operation="list",
        language="python",
        where_clause='siteid="BEDFORD"',
    )
    assert "success" in result


@pytest.mark.asyncio
async def test_build_custom_object_structure():
    from tools.schema_dev import build_custom_object_structure
    client = make_client(post_data={"_duration_ms": 10, "name": "ZMYASSET"})
    with patch("tools.schema_dev.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("core.audit.get_audit_logger", return_value=make_audit()):
        result = await build_custom_object_structure(
            name="ZMYASSET",
            base_object="ASSET",
            fields=[{"name": "CUSTFIELD", "type": "ALN", "description": "Custom field"}],
        )
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 11. INTEGRATIONS (tools/integrations.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_subscribe_to_event():
    from tools.integrations import subscribe_to_event
    # Use local fallback (MXEVNTCFG not available)
    with patch("tools.integrations._maximo_event_os_available", new_callable=AsyncMock, return_value=False), \
         patch("tools.integrations._local_subs_get", new_callable=AsyncMock, return_value=[]), \
         patch("tools.integrations._local_subs_set", new_callable=AsyncMock, return_value=None), \
         patch("tools.integrations.get_cache", return_value=make_cache({})), \
         patch("tools.integrations.get_audit_logger", return_value=make_audit()):
        result = await subscribe_to_event(
            event_type="WOSTATUSCHANGE",
            callback_url="http://myserver/webhook",
        )
    assert "success" in result


@pytest.mark.asyncio
async def test_list_event_subscriptions():
    from tools.integrations import list_event_subscriptions
    local_subs = [
        {
            "evtname": "WOSTATUSCHANGE",
            "url": "http://myserver/webhook",
            "active": True,
        }
    ]
    with patch("tools.integrations._maximo_event_os_available", new_callable=AsyncMock, return_value=False), \
         patch("tools.integrations._local_subs_get", new_callable=AsyncMock, return_value=local_subs), \
         patch("tools.integrations.get_cache", return_value=make_cache({"member": local_subs, "totalCount": 1})):
        result = await list_event_subscriptions()
    assert "success" in result


@pytest.mark.asyncio
async def test_ingest_iot_alert():
    from tools.integrations import ingest_iot_alert
    client = make_client(post_data=MOCK_POST_RESULT)
    with patch("tools.integrations.get_connected_client", new_callable=AsyncMock, return_value=client), \
         patch("tools.integrations.get_audit_logger", return_value=make_audit()):
        result = await ingest_iot_alert(
            asset_num="P-101",
            sensor_type="VIBRATION",
            reading_value=12.5,
            threshold=10.0,
            site_id="BEDFORD",
            unit="mm/s",
            severity="HIGH",
        )
    assert "success" in result


@pytest.mark.asyncio
async def test_trigger_webhook():
    from tools.integrations import trigger_webhook
    subs_response = {
        "success": True,
        "data": {
            "subscriptions": [
                {"evtname": "WOSTATUSCHANGE", "url": "http://myserver/webhook", "active": True}
            ]
        },
        "metadata": {"cached": False, "duration_ms": 5},
    }
    mock_http_response = MagicMock()
    mock_http_response.status_code = 200

    mock_http_client = AsyncMock()
    mock_http_client.post = AsyncMock(return_value=mock_http_response)
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch("tools.integrations.list_event_subscriptions", new_callable=AsyncMock, return_value=subs_response), \
         patch("tools.integrations.get_audit_logger", return_value=make_audit()), \
         patch("httpx.AsyncClient", return_value=mock_http_client):
        result = await trigger_webhook("WOSTATUSCHANGE", {"wonum": "WO10001", "status": "APPR"})
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 12. LABOR (tools/labor.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_labor():
    from tools.labor import list_labor
    with patch("tools.labor.get_cache", return_value=make_cache(MOCK_LABOR)):
        result = await list_labor("BEDFORD", craft="MECHANIC")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_labor_utilization():
    from tools.labor import get_labor_utilization
    # WO response with wplabor sub-records
    wo_with_labor = {
        "member": [
            {
                **MOCK_WO_MEMBER,
                "wplabor": [{"laborcode": "JOHN", "laborhrs": 4.0}],
            }
        ],
        "totalCount": 1,
        "_duration_ms": 10,
    }
    client = make_client(get_data=wo_with_labor)
    with patch("tools.labor.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await get_labor_utilization("BEDFORD", period_days=30)
    assert "success" in result


@pytest.mark.asyncio
async def test_list_crews():
    from tools.labor import list_crews
    mock_crew_data = {
        "member": [
            {
                "crewid": "CREW-A",
                "description": "Mechanical crew",
                "crewtype": "MAINT",
                "siteid": "BEDFORD",
                "crewmember": [{"laborcode": "JOHN"}],
            }
        ],
        "totalCount": 1,
        "_duration_ms": 10,
    }
    with patch("tools.labor.get_cache", return_value=make_cache(mock_crew_data)):
        result = await list_crews("BEDFORD")
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# 13. LOCATIONS (tools/locations.py)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_locations():
    from tools.locations import list_locations
    with patch("tools.locations.get_cache", return_value=make_cache(MOCK_LOC)):
        result = await list_locations("BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_location():
    from tools.locations import get_location
    with patch("tools.locations.get_cache", return_value=make_cache(MOCK_LOC)):
        result = await get_location("PLANT-A", "BEDFORD")
    assert "success" in result


@pytest.mark.asyncio
async def test_get_location_hierarchy():
    from tools.locations import get_location_hierarchy
    client = make_client(get_data=MOCK_LOC)
    with patch("tools.locations.get_connected_client", new_callable=AsyncMock, return_value=client):
        result = await get_location_hierarchy("BEDFORD")
    assert "success" in result


# ═════════════════════════════════════════════════════════════════════════════
# VALIDATION / EDGE-CASE SMOKE TESTS
# (one representative per module to confirm _error() envelope is correct)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_asset_missing_params():
    from tools.assets import get_asset
    result = await get_asset("", "BEDFORD")
    assert result["success"] is False
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_create_workorder_invalid_priority():
    from tools.workorders import create_workorder
    result = await create_workorder("Test", "P-101", "BEDFORD", priority=99)
    assert result["success"] is False
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_transfer_inventory_zero_quantity():
    from tools.inventory import transfer_inventory
    result = await transfer_inventory("BOLT-M10", "CENTRAL", "ANNEX", 0.0, "BEDFORD")
    assert result["success"] is False
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_subscribe_to_event_invalid_url():
    from tools.integrations import subscribe_to_event
    result = await subscribe_to_event("WOSTATUSCHANGE", "not-a-valid-url")
    assert result["success"] is False
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_build_custom_object_structure_bad_name():
    from tools.schema_dev import build_custom_object_structure
    # Name must start with Z
    result = await build_custom_object_structure(
        name="MYASSET",  # does not start with Z
        base_object="ASSET",
        fields=[{"name": "FIELD1", "type": "ALN"}],
    )
    assert result["success"] is False
    assert result["error_code"] == "VALIDATION_ERROR"
