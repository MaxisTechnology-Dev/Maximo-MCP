"""
server.py — Maximo Enterprise MCP Server entry point.
Registers the stable public Maximo tool surface and supports both stdio and
hosted HTTP/SSE transports.

Usage:
    python server.py                  # stdio transport (default)
    python server.py --http           # HTTP SSE on port 8080
    python server.py --test           # List all registered tools and exit
    TRANSPORT_MODE=http python server.py
"""

import argparse
import asyncio
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

# ── Core modules ──────────────────────────────────────────────────────────────
from core.response_utils import success_response
from core.settings import get_settings
from core.tool_catalog import ACTIVE_TOOL_COUNT

# ── Tool modules ──────────────────────────────────────────────────────────────
import tools.assets as assets
import tools.workorders as workorders
import tools.pm_scheduling as pm
import tools.inventory as inventory
import tools.purchasing as purchasing
import tools.labor as labor
import tools.locations as locations
import tools.ai_intelligence as ai
import tools.reporting as reporting
import tools.admin as admin
import tools.schema_dev as schema_dev
import tools.integrations as integrations

# ── Logging setup ─────────────────────────────────────────────────────────────
settings = get_settings()

_log_level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
if settings.LOG_FORMAT == "json":
    _log_fmt = '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
else:
    _log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logging.basicConfig(
    level=_log_level,
    format=_log_fmt,
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

# ── FastMCP Server ────────────────────────────────────────────────────────────
mcp = FastMCP(
    "Maximo Enterprise MCP",
    instructions=(
        f"A production-focused MCP server for IBM Maximo Asset Management by Maxis Technology, with {ACTIVE_TOOL_COUNT} "
        "stable tools covering assets, work orders, inventory, purchasing, labor, "
        "locations, reporting, schema discovery, and administration. "
        f"Connected to: {settings.MAXIMO_URL}"
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def health_check() -> Dict[str, Any]:
    """
    Check Maximo connectivity and return server version info.
    Use this to verify the MCP is correctly connected to your Maximo instance.
    """
    start = time.monotonic()
    from core.cache import get_cache
    from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client

    server_health: Dict[str, Any] = {
        "service": "Maximo Enterprise MCP",
        "transport": settings.TRANSPORT_MODE,
        "auth_mode": settings.AUTH_MODE,
        "rbac_enabled": settings.RBAC_ENABLED,
        "tool_count": ACTIVE_TOOL_COUNT,
    }
    maximo_health: Dict[str, Any] = {"connected": False, "url": settings.MAXIMO_URL}
    cache_health = await get_cache().get_status()

    try:
        client = await get_connected_client()
        try:
            info = await client.get("/whoami", params={"lean": "1"})
            maximo_health.update(
                {
                    "connected": True,
                    "user": info.get("userName") or info.get("personid", "unknown"),
                    "version": info.get("maximoVersion", "unknown"),
                }
            )
        except MaximoAuthError as exc:
            maximo_health["error"] = str(exc)
        except MaximoAPIError as exc:
            try:
                await client.get("/login", params={"lean": "1"})
                maximo_health.update({"connected": True, "user": "authenticated"})
            except Exception:
                maximo_health["error"] = str(exc)
    except Exception as exc:
        maximo_health["error"] = str(exc)

    duration_ms = int((time.monotonic() - start) * 1000)
    overall_ok = bool(maximo_health.get("connected")) and bool(cache_health.get("healthy"))
    return success_response(
        {
            "status": "ok" if overall_ok else "degraded",
            "server_health": server_health,
            "maximo_health": maximo_health,
            "cache_health": cache_health,
        },
        duration_ms=duration_ms,
        metadata={"tool_count": ACTIVE_TOOL_COUNT},
    )


# ══════════════════════════════════════════════════════════════════════════════
# ASSET TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_assets(
    site_id: Optional[str] = None, status: Optional[str] = None, asset_type: Optional[str] = None,
    page_size: Optional[int] = None, page_num: int = 1
) -> Dict[str, Any]:
    """List assets with optional filters. Supports pagination and caching."""
    return await assets.list_assets(site_id, status, asset_type, page_size, page_num)


@mcp.tool()
async def get_asset(asset_num: str, site_id: str) -> Dict[str, Any]:
    """Get full details for a specific Maximo asset."""
    return await assets.get_asset(asset_num, site_id)


# DISABLED — write operation
# @mcp.tool()
async def create_asset(
    asset_num: str, description: str, site_id: str,
    location: Optional[str] = None, asset_type: Optional[str] = None,
    serial_num: Optional[str] = None, purchase_price: Optional[float] = None
) -> Dict[str, Any]:
    """Create a new asset record in Maximo. Requires admin role."""
    return await assets.create_asset(asset_num, description, site_id, location, asset_type, serial_num, purchase_price)


# DISABLED — write operation
# @mcp.tool()
async def update_asset(
    asset_num: str, site_id: str, description: Optional[str] = None,
    location: Optional[str] = None, asset_type: Optional[str] = None, serial_num: Optional[str] = None,
    purchase_price: Optional[float] = None, manufacturer: Optional[str] = None, vendor: Optional[str] = None
) -> Dict[str, Any]:
    """Update specific fields on an existing asset. Requires supervisor role."""
    return await assets.update_asset(asset_num, site_id, description, location, asset_type, serial_num, purchase_price, manufacturer, vendor)


# DISABLED — write operation
# @mcp.tool()
async def retire_asset(
    asset_num: str, site_id: str,
    retirement_date: Optional[str] = None, reason: Optional[str] = None
) -> Dict[str, Any]:
    """Retire an asset by changing its status to DECOMMISSIONED. Requires manager role."""
    return await assets.retire_asset(asset_num, site_id, retirement_date, reason)


@mcp.tool()
async def get_asset_history(
    asset_num: str, site_id: str, lookback_days: int = 365
) -> Dict[str, Any]:
    """Retrieve work order and failure history for an asset."""
    return await assets.get_asset_history(asset_num, site_id, lookback_days)


@mcp.tool()
async def get_asset_downtime_stats(
    asset_num: str, site_id: str, period_months: int = 12
) -> Dict[str, Any]:
    """Calculate MTTR, MTBF, and availability percentage for an asset."""
    return await assets.get_asset_downtime_stats(asset_num, site_id, period_months)


@mcp.tool()
async def search_assets(
    keyword: str, site_id: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None, page_size: Optional[int] = None
) -> Dict[str, Any]:
    """Search assets by keyword across description and serial number."""
    return await assets.search_assets(keyword, site_id, filters, page_size)


# ══════════════════════════════════════════════════════════════════════════════
# WORK ORDER TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_workorders(
    site_id: Optional[str] = None, status: Optional[str] = None, asset_num: Optional[str] = None,
    priority: Optional[int] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    page_size: Optional[int] = None, page_num: int = 1
) -> Dict[str, Any]:
    """List work orders with filters for status, asset, priority, and date range."""
    return await workorders.list_workorders(site_id, status, asset_num, priority, date_from, date_to, page_size, page_num)


@mcp.tool()
async def get_workorder(wonum: str, site_id: str) -> Dict[str, Any]:
    """Get complete details for a specific work order."""
    return await workorders.get_workorder(wonum, site_id)


# DISABLED — write operation
# @mcp.tool()
async def create_workorder(
    description: str, asset_num: str, site_id: str,
    priority: int = 3, work_type: str = "CM",
    reported_by: Optional[str] = None, location: Optional[str] = None, notes: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new work order in Maximo. Requires technician role."""
    return await workorders.create_workorder(description, asset_num, site_id, priority, work_type, reported_by, location, notes)


# DISABLED — write operation
# @mcp.tool()
async def update_workorder(
    wonum: str, site_id: str, description: Optional[str] = None,
    priority: Optional[int] = None, location: Optional[str] = None,
    asset_num: Optional[str] = None, notes: Optional[str] = None
) -> Dict[str, Any]:
    """Update fields on an existing work order. Requires technician role."""
    return await workorders.update_workorder(wonum, site_id, description, priority, location, asset_num, notes)


# DISABLED — write operation
# @mcp.tool()
async def approve_workorder(wonum: str, site_id: str) -> Dict[str, Any]:
    """Approve a work order (status → APPR). Requires supervisor role."""
    return await workorders.approve_workorder(wonum, site_id)


# DISABLED — write operation
# @mcp.tool()
async def assign_technician(
    wonum: str, site_id: str, labor_code: str, craft: str,
    start_date: Optional[str] = None, hours_planned: float = 8.0
) -> Dict[str, Any]:
    """Assign a technician to a work order. Requires supervisor role."""
    return await workorders.assign_technician(wonum, site_id, labor_code, craft, start_date, hours_planned)


# DISABLED — write operation
# @mcp.tool()
async def close_workorder(
    wonum: str, site_id: str, actual_hours: float = 0.0,
    failure_code: Optional[str] = None, resolution_notes: Optional[str] = None
) -> Dict[str, Any]:
    """Close a work order with actual hours and resolution notes (status → COMP)."""
    return await workorders.close_workorder(wonum, site_id, actual_hours, failure_code, resolution_notes)


# DISABLED — write operation
# @mcp.tool()
async def cancel_workorder(wonum: str, site_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """Cancel a work order (status → CAN). Requires supervisor role."""
    return await workorders.cancel_workorder(wonum, site_id, reason)


@mcp.tool()
async def get_workorder_kpis(site_id: str, period_months: int = 3) -> Dict[str, Any]:
    """Compute work order KPIs: totals, completion time, overdue, backlog, priority breakdown."""
    return await workorders.get_workorder_kpis(site_id, period_months)


# ══════════════════════════════════════════════════════════════════════════════
# PM SCHEDULING TOOLS
# ══════════════════════════════════════════════════════════════════════════════

# DISABLED — BMXAA0024E PM READ permission denied on this instance
# @mcp.tool()
async def list_pm_schedules(
    site_id: str, asset_num: Optional[str] = None, active_only: bool = True
) -> Dict[str, Any]:
    """List preventive maintenance schedules for a site."""
    return await pm.list_pm_schedules(site_id, asset_num, active_only)


# DISABLED — write operation
# @mcp.tool()
async def generate_pm_workorders(site_id: str, date_range_days: int = 30) -> Dict[str, Any]:
    """Trigger PM work order generation for upcoming scheduled maintenance. Requires supervisor role."""
    return await pm.generate_pm_workorders(site_id, date_range_days)


# DISABLED — BMXAA0024E PM READ permission denied (delegates to list_pm_schedules)
# @mcp.tool()
async def get_pm_forecast(site_id: str, months_ahead: int = 3) -> Dict[str, Any]:
    """Forecast upcoming PMs with estimated labor hours and costs by month."""
    return await pm.get_pm_forecast(site_id, months_ahead)


# DISABLED — write operation
# @mcp.tool()
async def update_pm_frequency(
    pm_num: str, site_id: str, frequency: int, frequency_unit: str = "DAYS"
) -> Dict[str, Any]:
    """Update the maintenance interval for a PM schedule. Requires supervisor role."""
    return await pm.update_pm_frequency(pm_num, site_id, frequency, frequency_unit)


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def check_stock_level(item_num: str, storeroom: str, site_id: str) -> Dict[str, Any]:
    """Check current stock level, reorder point, and min/max levels for an item."""
    return await inventory.check_stock_level(item_num, storeroom, site_id)


@mcp.tool()
async def list_low_stock_items(site_id: str, storeroom: Optional[str] = None) -> Dict[str, Any]:
    """List all inventory items at or below their reorder point."""
    return await inventory.list_low_stock_items(site_id, storeroom)


# DISABLED — write operation
# @mcp.tool()
async def create_material_request(
    items: List[Dict[str, Any]], destination_storeroom: str, site_id: str, needed_by_date: Optional[str] = None
) -> Dict[str, Any]:
    """Create a material request (MATRECTRANS) for items needed from storeroom."""
    return await inventory.create_material_request(
        items=items, destination_storeroom=destination_storeroom,
        site_id=site_id, needed_by_date=needed_by_date
    )


# DISABLED — write operation
# @mcp.tool()
async def transfer_inventory(
    item_num: str, from_storeroom: str, to_storeroom: str, quantity: float, site_id: str
) -> Dict[str, Any]:
    """Transfer inventory between storerooms. Requires supervisor role."""
    return await inventory.transfer_inventory(item_num, from_storeroom, to_storeroom, quantity, site_id)


@mcp.tool()
async def get_reorder_recommendations(site_id: str) -> Dict[str, Any]:
    """Get prioritised reorder recommendations with suggested quantities and cost estimates."""
    return await inventory.get_reorder_recommendations(site_id)


# ══════════════════════════════════════════════════════════════════════════════
# PURCHASING TOOLS
# ══════════════════════════════════════════════════════════════════════════════

# DISABLED — write operation
# @mcp.tool()
async def create_purchase_order(
    vendor_id: str, items: List[Dict[str, Any]], site_id: str,
    required_date: Optional[str] = None, notes: Optional[str] = None, storeroom: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new purchase order. Requires manager role."""
    return await purchasing.create_purchase_order(
        vendor_id=vendor_id, items=items, site_id=site_id,
        required_date=required_date, notes=notes, storeroom=storeroom
    )


@mcp.tool()
async def get_purchase_order(ponum: str, site_id: str) -> Dict[str, Any]:
    """Get full purchase order details including all line items."""
    return await purchasing.get_purchase_order(ponum, site_id)


# DISABLED — write operation
# @mcp.tool()
async def receive_items(ponum: str, site_id: str, received_lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Record receipt of items against a purchase order. Requires supervisor role."""
    return await purchasing.receive_items(ponum, site_id, received_lines)


@mcp.tool()
async def get_vendor_performance(vendor_id: str, period_months: int = 12) -> Dict[str, Any]:
    """Analyse vendor on-time delivery and quality metrics."""
    return await purchasing.get_vendor_performance(vendor_id, period_months)


# ══════════════════════════════════════════════════════════════════════════════
# AI INTELLIGENCE TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def nl_to_oslc_query(
    natural_language_query: str, object_structure: Optional[str] = "mxwo", dry_run: bool = False
) -> Dict[str, Any]:
    """
    Convert natural language to OSLC query. Example: 'show overdue work orders in Bedford with priority 1'.
    Set dry_run=True to execute the query and return sample results.
    """
    return await ai.nl_to_oslc_query(natural_language_query, object_structure, dry_run)


# DISABLED — AI tool (SKIPPED_COMPLEX)
# @mcp.tool()
async def detect_asset_anomalies(
    asset_num: str, site_id: str, lookback_days: int = 90
) -> Dict[str, Any]:
    """Detect statistical anomalies in asset failure patterns. Flags >2σ deviations."""
    return await ai.detect_asset_anomalies(asset_num, site_id, lookback_days)


# DISABLED — AI tool (SKIPPED_COMPLEX)
# @mcp.tool()
async def suggest_root_cause(
    asset_num: str, site_id: str, failure_description: str
) -> Dict[str, Any]:
    """AI-powered root cause analysis using failure history. Returns top 3 causes with confidence scores."""
    return await ai.suggest_root_cause(asset_num, site_id, failure_description)


# DISABLED — AI tool (SKIPPED_COMPLEX)
# @mcp.tool()
async def summarize_asset_health(asset_num: str, site_id: str) -> Dict[str, Any]:
    """Generate asset health score (0-100) with status, key issues, and recommendations."""
    return await ai.summarize_asset_health(asset_num, site_id)


# DISABLED — AI tool (SKIPPED_COMPLEX)
# @mcp.tool()
async def search_maximo_knowledge(query: str, doc_type: str = "all") -> Dict[str, Any]:
    """Semantic search over Maximo documentation. doc_type: procedures|api_docs|failure_codes|all."""
    return await ai.search_maximo_knowledge(query, doc_type)


# ══════════════════════════════════════════════════════════════════════════════
# REPORTING TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_maintenance_kpi_dashboard(site_id: str, period_months: int = 3) -> Dict[str, Any]:
    """Full maintenance KPI dashboard: MTTR, MTBF, PM compliance, costs, backlog. Requires manager role."""
    return await reporting.get_maintenance_kpi_dashboard(site_id, period_months)


# DISABLED — export tool (SKIPPED_COMPLEX)
# @mcp.tool()
async def export_workorders_excel(
    site_id: str, filters: Optional[Dict[str, str]] = None, max_records: int = 1000
) -> Dict[str, Any]:
    """Export work orders to Excel. Returns base64-encoded file + filename."""
    return await reporting.export_workorders_excel(site_id, filters, max_records)


# DISABLED — export tool (SKIPPED_COMPLEX)
# @mcp.tool()
async def export_asset_report_pdf(
    site_id: str, asset_group: Optional[str] = None, max_records: int = 200
) -> Dict[str, Any]:
    """Export asset report to PDF. Returns base64-encoded file + filename."""
    return await reporting.export_asset_report_pdf(site_id, asset_group, max_records)


@mcp.tool()
async def generate_carbon_table(
    object_structure: str, data: List[Dict[str, Any]], columns: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Render data as an IBM Carbon Design System HTML table. columns: list of {key, header} dicts."""
    return await reporting.generate_carbon_table(object_structure, data, columns)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_users(site_id: Optional[str] = None) -> Dict[str, Any]:
    """List Maximo user records. Requires admin role."""
    return await admin.list_users(site_id)


@mcp.tool()
async def get_user(user_id: str) -> Dict[str, Any]:
    """Get detailed information for a specific Maximo user. Requires manager role."""
    return await admin.get_user(user_id)


# DISABLED — /os/mxsecgroup 404 object structure not configured on this instance
# @mcp.tool()
async def list_security_groups() -> Dict[str, Any]:
    """List all Maximo security groups with member counts. Requires admin role."""
    return await admin.list_security_groups()


@mcp.tool()
async def query_audit_log(
    tool_name: Optional[str] = None, user_id: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 100
) -> Dict[str, Any]:
    """Search MCP audit trail for tool call history. Requires manager role."""
    return await admin.query_audit_log(tool_name, user_id, date_from, date_to, limit)


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA / DEV TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_object_structures(
    filter_keyword: Optional[str] = None, include_custom: bool = True
) -> Dict[str, Any]:
    """List all Maximo object structures (APIs). Cached for 24 hours."""
    return await schema_dev.list_object_structures(filter_keyword, include_custom)


@mcp.tool()
async def get_schema_details(
    object_structure: str, include_relationships: bool = True
) -> Dict[str, Any]:
    """Get field names, types, and requirements for a Maximo object structure."""
    return await schema_dev.get_schema_details(object_structure, include_relationships)


@mcp.tool()
async def validate_oslc_query(
    object_structure: str, where_clause: Optional[str] = None, select_clause: Optional[str] = None
) -> Dict[str, Any]:
    """Validate OSLC query syntax with a dry-run against Maximo."""
    return await schema_dev.validate_oslc_query(object_structure, where_clause, select_clause)


@mcp.tool()
async def generate_api_code(
    object_structure: str, operation: str = "list",
    language: str = "python", where_clause: Optional[str] = None
) -> Dict[str, Any]:
    """Generate ready-to-use Maximo API code in Python, JavaScript, curl, or SQL."""
    return await schema_dev.generate_api_code(object_structure, operation, language, where_clause)


# DISABLED — write operation
# @mcp.tool()
async def build_custom_object_structure(
    name: str, base_object: str, fields: List[str]
) -> Dict[str, Any]:
    """Create a new custom Maximo object structure via the API. Requires admin role."""
    return await schema_dev.build_custom_object_structure(name, base_object, fields)


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TOOLS
# ══════════════════════════════════════════════════════════════════════════════

# DISABLED — write operation
# @mcp.tool()
async def subscribe_to_event(
    event_type: str, callback_url: str,
    filter_conditions: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Register a Maximo webhook/event listener. Requires admin role."""
    return await integrations.subscribe_to_event(event_type, callback_url, filter_conditions)


@mcp.tool()
async def list_event_subscriptions() -> Dict[str, Any]:
    """List all active Maximo event listener subscriptions."""
    return await integrations.list_event_subscriptions()


# DISABLED — write operation
# @mcp.tool()
async def ingest_iot_alert(
    asset_num: str, sensor_type: str, reading_value: float,
    threshold: float, site_id: str, unit: Optional[str] = None, severity: Optional[str] = None
) -> Dict[str, Any]:
    """Create a Maximo work order triggered by an IoT sensor alert (SCADA/IIoT bridge)."""
    return await integrations.ingest_iot_alert(asset_num, sensor_type, reading_value, threshold, site_id, unit, severity)


# DISABLED — write operation
# @mcp.tool()
async def trigger_webhook(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Manually fire a test webhook for a registered event type. Requires admin role."""
    return await integrations.trigger_webhook(event_type, payload)


# ══════════════════════════════════════════════════════════════════════════════
# LABOR & LOCATION TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_labor(
    site_id: str, craft: Optional[str] = None, status: str = "ACTIVE"
) -> Dict[str, Any]:
    """List available labor resources and technicians for a site."""
    return await labor.list_labor(site_id, craft, status)


@mcp.tool()
async def get_labor_utilization(
    site_id: str, labor_code: Optional[str] = None, period_days: int = 30
) -> Dict[str, Any]:
    """Calculate technician utilization percentages and hours worked."""
    return await labor.get_labor_utilization(site_id, labor_code, period_days)


@mcp.tool()
async def list_crews(site_id: str) -> Dict[str, Any]:
    """List maintenance crews and their members for a site."""
    return await labor.list_crews(site_id)


@mcp.tool()
async def list_locations(
    site_id: str, parent_location: Optional[str] = None, location_type: Optional[str] = None
) -> Dict[str, Any]:
    """List operational locations for a site with optional hierarchy filtering."""
    return await locations.list_locations(site_id, parent_location, location_type)


@mcp.tool()
async def get_location(location: str, site_id: str) -> Dict[str, Any]:
    """Get full details for a specific location including its assets."""
    return await locations.get_location(location, site_id)


@mcp.tool()
async def get_location_hierarchy(site_id: str, root_location: Optional[str] = None) -> Dict[str, Any]:
    """Build a hierarchical tree of all locations for a site."""
    return await locations.get_location_hierarchy(site_id, root_location)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def _list_tools():
    """Print all registered tools and exit."""
    tools_list = asyncio.run(mcp.list_tools())
    print(f"\n{'='*60}")
    print(f"  Maximo Enterprise MCP - Registered Tools")
    print(f"{'='*60}")
    for i, tool in enumerate(sorted(tools_list, key=lambda t: t.name), 1):
        print(f"  {i:2d}. {tool.name}")
    print(f"\n  Total: {len(tools_list)} tools registered")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Maximo Enterprise MCP Server")
    parser.add_argument("--http", action="store_true", help="Run in HTTP SSE mode")
    parser.add_argument("--test", action="store_true", help="List tools and exit")
    parser.add_argument("--port", type=int, default=settings.HTTP_PORT, help="HTTP port")
    parser.add_argument("--host", default=settings.HTTP_HOST, help="HTTP host")
    args = parser.parse_args()

    if args.test:
        _list_tools()
        sys.exit(0)

    # Determine transport
    use_http = args.http or settings.TRANSPORT_MODE.lower() == "http"

    if use_http:
        logger.info("Starting Maximo Enterprise MCP in HTTP SSE mode on %s:%d", args.host, args.port)
        _run_http(args.host, args.port)
    else:
        logger.info("Starting Maximo Enterprise MCP in stdio mode")
        mcp.run(transport="stdio")


def _run_http(host: str, port: int) -> None:
    """
    Start hosted HTTP mode with a FastAPI wrapper around the MCP SSE transport.
    Hosted mode is fail-closed: at least one inbound auth method must be active.

    MCP_AUTH_MODE controls which methods are accepted:
      static — MCP_ACCESS_TOKEN is required
      jwt    — OIDC_ISSUER + OIDC_AUDIENCE are required (validated in settings)
      both   — MCP_ACCESS_TOKEN AND OIDC_* are required
    """
    token = settings.MCP_ACCESS_TOKEN
    if settings.MCP_AUTH_MODE in ("static", "both") and not token:
        raise RuntimeError(
            f"MCP_ACCESS_TOKEN is required when MCP_AUTH_MODE={settings.MCP_AUTH_MODE!r}."
        )

    from core.web import create_http_app
    import uvicorn

    app = create_http_app(mcp, sys.modules[__name__], token)
    logger.info(
        "Hosted auth enabled: mode=%s (static=%s, jwt=%s).",
        settings.MCP_AUTH_MODE,
        bool(token),
        settings.MCP_AUTH_MODE in ("jwt", "both"),
    )

    ssl_kwargs: Dict[str, Any] = {}
    if settings.MCP_SSL_CERTFILE and settings.MCP_SSL_KEYFILE:
        ssl_kwargs = {
            "ssl_certfile": settings.MCP_SSL_CERTFILE,
            "ssl_keyfile": settings.MCP_SSL_KEYFILE,
        }
        logger.info("In-process TLS enabled (cert=%s).", settings.MCP_SSL_CERTFILE)
    else:
        logger.warning(
            "Serving plaintext HTTP. Terminate TLS at the edge "
            "(ALB / Front Door / API Gateway) before exposing this port."
        )
    uvicorn.run(app, host=host, port=port, log_level="warning", **ssl_kwargs)


if __name__ == "__main__":
    main()
