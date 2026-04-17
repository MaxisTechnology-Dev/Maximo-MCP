"""
Metadata for the stable public tool surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any, Awaitable, Callable, Dict, Optional, Type

from pydantic import BaseModel

from core import tool_models


ToolFunc = Callable[..., Awaitable[Dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: str
    stability: str
    summary: str
    request_model: Optional[Type[BaseModel]] = None
    func: Optional[ToolFunc] = None


TOOL_METADATA: Dict[str, ToolSpec] = {
    "health_check": ToolSpec("health_check", "core", "core", "Check Maximo, cache, and server health.", tool_models.EmptyArgs),
    "list_assets": ToolSpec("list_assets", "assets", "core", "List assets with optional filters and paging.", tool_models.ListAssetsArgs),
    "get_asset": ToolSpec("get_asset", "assets", "core", "Get full details for a specific asset.", tool_models.GetAssetArgs),
    "get_asset_history": ToolSpec("get_asset_history", "assets", "advanced", "Retrieve work order and failure history for an asset."),
    "get_asset_downtime_stats": ToolSpec("get_asset_downtime_stats", "assets", "advanced", "Calculate MTTR, MTBF, and availability for an asset."),
    "search_assets": ToolSpec("search_assets", "assets", "core", "Search assets by keyword.", tool_models.SearchAssetsArgs),
    "list_workorders": ToolSpec("list_workorders", "workorders", "core", "List work orders with filters and paging.", tool_models.ListWorkordersArgs),
    "get_workorder": ToolSpec("get_workorder", "workorders", "core", "Get full details for a specific work order.", tool_models.GetWorkorderArgs),
    "get_workorder_kpis": ToolSpec("get_workorder_kpis", "workorders", "core", "Compute work order KPIs for a site.", tool_models.GetWorkorderKpisArgs),
    "check_stock_level": ToolSpec("check_stock_level", "inventory", "core", "Check stock levels for an item.", tool_models.CheckStockLevelArgs),
    "list_low_stock_items": ToolSpec("list_low_stock_items", "inventory", "core", "List inventory items at or below reorder point.", tool_models.ListLowStockItemsArgs),
    "get_reorder_recommendations": ToolSpec("get_reorder_recommendations", "inventory", "core", "Get prioritized reorder recommendations."),
    "get_purchase_order": ToolSpec("get_purchase_order", "purchasing", "core", "Get full purchase order details."),
    "get_vendor_performance": ToolSpec("get_vendor_performance", "purchasing", "core", "Analyze vendor delivery and quality metrics."),
    "nl_to_oslc_query": ToolSpec("nl_to_oslc_query", "ai", "advanced", "Convert natural language into an OSLC query."),
    "get_maintenance_kpi_dashboard": ToolSpec("get_maintenance_kpi_dashboard", "reporting", "advanced", "Return a maintenance KPI dashboard."),
    "generate_carbon_table": ToolSpec("generate_carbon_table", "reporting", "advanced", "Render data as a Carbon HTML table."),
    "list_users": ToolSpec("list_users", "admin", "advanced", "List Maximo users."),
    "get_user": ToolSpec("get_user", "admin", "advanced", "Get a specific Maximo user."),
    "query_audit_log": ToolSpec("query_audit_log", "admin", "advanced", "Search the MCP audit log."),
    "list_object_structures": ToolSpec("list_object_structures", "schema", "advanced", "List available Maximo object structures."),
    "get_schema_details": ToolSpec("get_schema_details", "schema", "advanced", "Describe a Maximo object structure."),
    "validate_oslc_query": ToolSpec("validate_oslc_query", "schema", "advanced", "Validate an OSLC query against Maximo."),
    "generate_api_code": ToolSpec("generate_api_code", "schema", "advanced", "Generate example Maximo API code."),
    "list_event_subscriptions": ToolSpec("list_event_subscriptions", "integrations", "advanced", "List active event subscriptions."),
    "list_labor": ToolSpec("list_labor", "labor", "core", "List labor resources for a site."),
    "get_labor_utilization": ToolSpec("get_labor_utilization", "labor", "core", "Calculate labor utilization."),
    "list_crews": ToolSpec("list_crews", "labor", "core", "List maintenance crews."),
    "list_locations": ToolSpec("list_locations", "locations", "core", "List locations for a site."),
    "get_location": ToolSpec("get_location", "locations", "core", "Get details for a specific location."),
    "get_location_hierarchy": ToolSpec("get_location_hierarchy", "locations", "core", "Build a location hierarchy tree."),
}

ACTIVE_TOOL_NAMES = tuple(TOOL_METADATA.keys())
ACTIVE_TOOL_COUNT = len(ACTIVE_TOOL_NAMES)


def bind_runtime(module: ModuleType) -> Dict[str, ToolSpec]:
    bound: Dict[str, ToolSpec] = {}
    for name, spec in TOOL_METADATA.items():
        func = getattr(module, name, None)
        if func is None or not callable(func):
            raise RuntimeError(
                f"Tool '{name}' is listed in TOOL_METADATA but not found in server module. "
                "Either add the tool function or remove it from TOOL_METADATA."
            )
        bound[name] = ToolSpec(
            name=spec.name,
            category=spec.category,
            stability=spec.stability,
            summary=spec.summary,
            request_model=spec.request_model,
            func=func,
        )
    return bound
