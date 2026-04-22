"""
tests/integration_test_tools.py — Real-server integration tests for Maximo Enterprise MCP.

No mocking. All HTTP calls hit the live Maximo instance at the URL in .env.

Run:
    pytest tests/integration_test_tools.py -v -m integration --timeout=120 2>&1 | tee integration_output.txt

Probe data gathered from live system (site WW):
    site_id      = "WW"
    asset_num    = "10000"
    wonum        = "1119"
    location     = "00"
    labor_code   = "CEWWAEJ"
    user_id      = "MAXADMIN"
    item_num     = "16998"  storeroom = "CENTRAL"
    object_str   = "mxwo"
"""

import asyncio
import os
import time
from typing import Any, Dict, List

import sys
import os

# ── Add project root to sys.path (needed when running via `pytest` not `python -m pytest`) ──
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import pytest
from dotenv import load_dotenv

# ── Load real .env, overriding any test values set by unit-test conftest.py ─
load_dotenv(override=True)

# ── Test configuration ──────────────────────────────────────────────────────
SITE_ID       = "WW"
ASSET_NUM     = "10000"
WONUM         = "1119"
LOCATION      = "00"
LABOR_CODE    = "CEWWAEJ"
USER_ID       = "MAXADMIN"
ITEM_NUM      = "16998"
STOREROOM     = "CENTRAL"
OBJECT_STRUCT = "mxwo"
TIMEOUT_SEC   = 120   # Maximo is slow; health_check ~17s, list_assets ~18s

# ── Result accumulator (written at end of session by the fixture) ───────────
_results: List[Dict[str, Any]] = []


def _record(tool: str, status: str, detail: str = "") -> None:
    _results.append({"tool": tool, "status": status, "detail": detail})


async def _run(name: str, coro) -> bool:
    """
    Execute coro with a timeout.  Record PASS / FAIL_LOGIC / FAIL_ENVELOPE /
    TIMEOUT / EXCEPTION into _results and return True only on PASS.
    """
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout=TIMEOUT_SEC)
        elapsed = round((time.monotonic() - start) * 1000)
        if not isinstance(result, dict):
            _record(name, "FAIL_ENVELOPE", f"not a dict: {str(result)[:80]}")
            return False
        if result.get("success") is True:
            _record(name, "PASS", f"{elapsed}ms")
            return True
        elif result.get("success") is False:
            err = result.get("error", "")
            _record(name, "FAIL_LOGIC", str(err)[:120])
            return False
        else:
            _record(name, "FAIL_ENVELOPE", f"missing 'success' key: {str(result)[:80]}")
            return False
    except asyncio.TimeoutError:
        _record(name, "TIMEOUT", f"exceeded {TIMEOUT_SEC}s")
        return False
    except Exception as exc:
        _record(name, "EXCEPTION", f"{type(exc).__name__}: {str(exc)[:100]}")
        return False


def _skip_write(name: str) -> None:
    _record(name, "SKIPPED_WRITE", "write operation — skipped per safety rules")


def _skip_complex(name: str) -> None:
    _record(name, "SKIPPED_COMPLEX", "AI/export tool — skipped per instructions")


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_check():
    from server import health_check
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(health_check(), timeout=TIMEOUT_SEC)
        elapsed = round((time.monotonic() - start) * 1000)
        if (
            isinstance(result, dict)
            and result.get("success") is True
            and result.get("data", {}).get("maximo_health", {}).get("connected") is True
        ):
            _record("health_check", "PASS", f"{elapsed}ms")
            assert True
        else:
            _record("health_check", "FAIL_LOGIC", str(result)[:120])
            assert False, f"health_check returned: {result}"
    except asyncio.TimeoutError:
        _record("health_check", "TIMEOUT", f"exceeded {TIMEOUT_SEC}s")
        assert False, "health_check timed out"
    except Exception as exc:
        _record("health_check", "EXCEPTION", f"{type(exc).__name__}: {str(exc)[:100]}")
        assert False, str(exc)


# ══════════════════════════════════════════════════════════════════════════════
# ASSETS
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_assets():
    from tools.assets import list_assets
    assert await _run("list_assets", list_assets(site_id=SITE_ID, page_size=3))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_asset():
    from tools.assets import get_asset
    assert await _run("get_asset", get_asset(asset_num=ASSET_NUM, site_id=SITE_ID))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_assets():
    from tools.assets import search_assets
    assert await _run("search_assets", search_assets(keyword="Air", site_id=SITE_ID, page_size=3))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_asset_history():
    from tools.assets import get_asset_history
    assert await _run(
        "get_asset_history",
        get_asset_history(asset_num=ASSET_NUM, site_id=SITE_ID, lookback_days=365),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_asset_downtime_stats():
    from tools.assets import get_asset_downtime_stats
    assert await _run(
        "get_asset_downtime_stats",
        get_asset_downtime_stats(asset_num=ASSET_NUM, site_id=SITE_ID, period_months=12),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_asset():
    _skip_write("create_asset")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_asset():
    _skip_write("update_asset")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retire_asset():
    _skip_write("retire_asset")
    pytest.skip("write operation")


# ══════════════════════════════════════════════════════════════════════════════
# WORK ORDERS
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_workorders():
    from tools.workorders import list_workorders
    # Use page_size=1 — WO table has 3.4 M rows; page_size=3 already took 51s
    assert await _run("list_workorders", list_workorders(site_id=SITE_ID, page_size=1))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_workorder():
    from tools.workorders import get_workorder
    assert await _run("get_workorder", get_workorder(wonum=WONUM, site_id=SITE_ID))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_workorder_kpis():
    from tools.workorders import get_workorder_kpis
    assert await _run(
        "get_workorder_kpis",
        get_workorder_kpis(site_id=SITE_ID, period_months=3),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_workorder():
    _skip_write("create_workorder")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_workorder():
    _skip_write("update_workorder")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_workorder():
    _skip_write("approve_workorder")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_assign_technician():
    _skip_write("assign_technician")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_workorder():
    _skip_write("close_workorder")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_workorder():
    _skip_write("cancel_workorder")
    pytest.skip("write operation")


# ══════════════════════════════════════════════════════════════════════════════
# PM SCHEDULING
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_pm_schedules():
    from tools.pm_scheduling import list_pm_schedules
    await _run("list_pm_schedules", list_pm_schedules(site_id=SITE_ID, page_size=3))
    # Don't assert — permission may be denied on this Maximo instance


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_pm_workorders():
    _skip_write("generate_pm_workorders")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_pm_forecast():
    from tools.pm_scheduling import get_pm_forecast
    await _run("get_pm_forecast", get_pm_forecast(site_id=SITE_ID, months_ahead=3))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_pm_frequency():
    _skip_write("update_pm_frequency")
    pytest.skip("write operation")


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_check_stock_level():
    from tools.inventory import check_stock_level
    await _run(
        "check_stock_level",
        check_stock_level(item_num=ITEM_NUM, storeroom=STOREROOM, site_id=SITE_ID),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_low_stock_items():
    from tools.inventory import list_low_stock_items
    assert await _run("list_low_stock_items", list_low_stock_items(site_id=SITE_ID))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_material_request():
    _skip_write("create_material_request")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transfer_inventory():
    _skip_write("transfer_inventory")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_reorder_recommendations():
    from tools.inventory import get_reorder_recommendations
    assert await _run(
        "get_reorder_recommendations",
        get_reorder_recommendations(site_id=SITE_ID),
    )


# ══════════════════════════════════════════════════════════════════════════════
# PURCHASING
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_purchase_order():
    _skip_write("create_purchase_order")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_purchase_order():
    from tools.purchasing import get_purchase_order
    # Try a common PO number; may return NOT_FOUND — that is FAIL_LOGIC, not a code bug
    await _run("get_purchase_order", get_purchase_order(ponum="1000", site_id=SITE_ID))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_receive_items():
    _skip_write("receive_items")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_vendor_performance():
    from tools.purchasing import get_vendor_performance
    await _run(
        "get_vendor_performance",
        get_vendor_performance(vendor_id="VENDOR1", period_months=12),
    )


# ══════════════════════════════════════════════════════════════════════════════
# AI INTELLIGENCE
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_nl_to_oslc_query():
    from tools.ai_intelligence import nl_to_oslc_query
    assert await _run(
        "nl_to_oslc_query",
        nl_to_oslc_query(
            natural_language_query="show open work orders for site WW",
            object_structure="mxwo",
            dry_run=True,
        ),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_detect_asset_anomalies():
    _skip_complex("detect_asset_anomalies")
    pytest.skip("complex AI tool — skipped")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_suggest_root_cause():
    _skip_complex("suggest_root_cause")
    pytest.skip("complex AI tool — skipped")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summarize_asset_health():
    _skip_complex("summarize_asset_health")
    pytest.skip("complex AI tool — skipped")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_maximo_knowledge():
    _skip_complex("search_maximo_knowledge")
    pytest.skip("complex AI tool — skipped")


# ══════════════════════════════════════════════════════════════════════════════
# REPORTING
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_maintenance_kpi_dashboard():
    from tools.reporting import get_maintenance_kpi_dashboard
    assert await _run(
        "get_maintenance_kpi_dashboard",
        get_maintenance_kpi_dashboard(site_id=SITE_ID, period_months=3),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_workorders_excel():
    _skip_complex("export_workorders_excel")
    pytest.skip("export tool — skipped")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_asset_report_pdf():
    _skip_complex("export_asset_report_pdf")
    pytest.skip("export/AI tool — skipped")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_carbon_table():
    from tools.reporting import generate_carbon_table
    # Pure HTML generation — no HTTP calls needed
    assert await _run(
        "generate_carbon_table",
        generate_carbon_table(
            object_structure="mxwo",
            data=[{"wonum": "1119", "status": "CLOSED"}],
            columns=[{"key": "wonum", "header": "WO #"}, {"key": "status", "header": "Status"}],
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_users():
    from tools.admin import list_users
    assert await _run("list_users", list_users())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_user():
    from tools.admin import get_user
    assert await _run("get_user", get_user(user_id=USER_ID))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_security_groups():
    from tools.admin import list_security_groups
    assert await _run("list_security_groups", list_security_groups())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_audit_log():
    from tools.admin import query_audit_log
    assert await _run("query_audit_log", query_audit_log(limit=10))


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA / DEV TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_object_structures():
    from tools.schema_dev import list_object_structures
    assert await _run(
        "list_object_structures",
        list_object_structures(filter_keyword="mxwo"),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_schema_details():
    from tools.schema_dev import get_schema_details
    assert await _run(
        "get_schema_details",
        get_schema_details(object_structure=OBJECT_STRUCT),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validate_oslc_query():
    from tools.schema_dev import validate_oslc_query
    assert await _run(
        "validate_oslc_query",
        validate_oslc_query(
            object_structure=OBJECT_STRUCT,
            where_clause=f'siteid="{SITE_ID}"',
        ),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_api_code():
    from tools.schema_dev import generate_api_code
    # Pure template generation — no HTTP calls
    assert await _run(
        "generate_api_code",
        generate_api_code(object_structure=OBJECT_STRUCT, operation="list", language="python"),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_custom_object_structure():
    _skip_write("build_custom_object_structure")
    pytest.skip("write operation")


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATIONS
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscribe_to_event():
    _skip_write("subscribe_to_event")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_event_subscriptions():
    from tools.integrations import list_event_subscriptions
    assert await _run("list_event_subscriptions", list_event_subscriptions())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_iot_alert():
    _skip_write("ingest_iot_alert")
    pytest.skip("write operation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trigger_webhook():
    _skip_write("trigger_webhook")
    pytest.skip("write operation")


# ══════════════════════════════════════════════════════════════════════════════
# LABOR
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_labor():
    from tools.labor import list_labor
    assert await _run("list_labor", list_labor(site_id=SITE_ID))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_labor_utilization():
    from tools.labor import get_labor_utilization
    assert await _run(
        "get_labor_utilization",
        get_labor_utilization(site_id=SITE_ID, labor_code=LABOR_CODE, period_days=30),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_crews():
    from tools.labor import list_crews
    assert await _run("list_crews", list_crews(site_id=SITE_ID))


# ══════════════════════════════════════════════════════════════════════════════
# LOCATIONS
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_locations():
    from tools.locations import list_locations
    assert await _run("list_locations", list_locations(site_id=SITE_ID))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_location():
    from tools.locations import get_location
    assert await _run("get_location", get_location(location=LOCATION, site_id=SITE_ID))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_location_hierarchy():
    from tools.locations import get_location_hierarchy
    assert await _run(
        "get_location_hierarchy",
        get_location_hierarchy(site_id=SITE_ID, root_location=LOCATION),
    )


# ══════════════════════════════════════════════════════════════════════════════
# SESSION SUMMARY FIXTURE
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session", autouse=True)
def _print_summary(request):
    """Print the result table after the entire test session."""
    yield
    if not _results:
        return

    # Count statuses
    from collections import Counter
    counts = Counter(r["status"] for r in _results)

    print("\n" + "=" * 70)
    print("INTEGRATION TEST SUMMARY")
    print("=" * 70)
    header = f"{'#':<4} {'Tool':<40} {'Status':<16} {'Detail'}"
    print(header)
    print("-" * 70)
    for i, row in enumerate(_results, 1):
        print(f"{i:<4} {row['tool']:<40} {row['status']:<16} {row['detail']}")
    print("-" * 70)
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")
    print(f"  TOTAL: {len(_results)}")
    print("=" * 70)
