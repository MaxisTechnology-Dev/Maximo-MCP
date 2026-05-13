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
    # ── core ─────────────────────────────────────────────────────────────────
    "health_check": ToolSpec("health_check", "core", "core", "Check Maximo, cache, and server health.", tool_models.EmptyArgs),

    # ── assets ───────────────────────────────────────────────────────────────
    "list_assets": ToolSpec("list_assets", "assets", "core", "List assets with optional filters and paging.", tool_models.ListAssetsArgs),
    "get_asset": ToolSpec("get_asset", "assets", "core", "Get full details for a specific asset.", tool_models.GetAssetArgs),
    "get_asset_history": ToolSpec("get_asset_history", "assets", "advanced", "Retrieve work order and failure history for an asset.", tool_models.GetAssetHistoryArgs),
    "get_asset_downtime_stats": ToolSpec("get_asset_downtime_stats", "assets", "advanced", "Calculate MTTR, MTBF, and availability for an asset.", tool_models.GetAssetDowntimeStatsArgs),
    "search_assets": ToolSpec("search_assets", "assets", "core", "Search assets by keyword.", tool_models.SearchAssetsArgs),
    "get_failure_class_hierarchy": ToolSpec("get_failure_class_hierarchy", "assets", "core", "List Maximo failure classes (problem/cause/remedy).", tool_models.GetFailureClassHierarchyArgs),
    "get_meter_readings": ToolSpec("get_meter_readings", "assets", "core", "Return asset meter readings over a look-back window.", tool_models.GetMeterReadingsArgs),
    "get_asset_criticality_matrix": ToolSpec("get_asset_criticality_matrix", "assets", "core", "Asset criticality matrix bucketed by priority.", tool_models.GetAssetCriticalityMatrixArgs),
    "get_warranty_status": ToolSpec("get_warranty_status", "assets", "core", "Bucket assets by warranty status for claim-recovery review.", tool_models.GetWarrantyStatusArgs),

    # ── work orders / SR / job plans ─────────────────────────────────────────
    "list_workorders": ToolSpec("list_workorders", "workorders", "core", "List work orders with filters and paging.", tool_models.ListWorkordersArgs),
    "get_workorder": ToolSpec("get_workorder", "workorders", "core", "Get full details for a specific work order.", tool_models.GetWorkorderArgs),
    "get_workorder_kpis": ToolSpec("get_workorder_kpis", "workorders", "core", "Compute work order KPIs for a site.", tool_models.GetWorkorderKpisArgs),
    "list_service_requests": ToolSpec("list_service_requests", "workorders", "core", "List service requests (SRs) — upstream intake records.", tool_models.ListServiceRequestsArgs),
    "get_service_request": ToolSpec("get_service_request", "workorders", "core", "Get full details for a specific service request.", tool_models.GetServiceRequestArgs),
    "list_job_plans": ToolSpec("list_job_plans", "workorders", "core", "List job plans (reusable work templates).", tool_models.ListJobPlansArgs),
    "get_my_assigned_workorders": ToolSpec("get_my_assigned_workorders", "workorders", "core", "List work orders assigned to a labor (or to the current identity).", tool_models.GetMyAssignedWorkordersArgs),
    "get_workorder_tasks": ToolSpec("get_workorder_tasks", "workorders", "core", "List the task breakdown of a parent work order.", tool_models.GetWorkorderTasksArgs),
    "get_job_plan": ToolSpec("get_job_plan", "workorders", "core", "Get full job plan with embedded tasks, labor, materials, tools.", tool_models.GetJobPlanArgs),
    "get_workorder_actuals_vs_planned": ToolSpec("get_workorder_actuals_vs_planned", "workorders", "core", "Compare estimated vs actual labor / material / cost for a WO.", tool_models.GetWorkorderActualsVsPlannedArgs),
    "get_schedule_calendar": ToolSpec("get_schedule_calendar", "workorders", "core", "Return scheduled work orders within a date window grouped by date.", tool_models.GetScheduleCalendarArgs),
    "estimate_workorder_cost": ToolSpec("estimate_workorder_cost", "workorders", "core", "Estimate labor + material + tool cost from a job plan.", tool_models.EstimateWorkorderCostArgs),
    "get_workorder_costs": ToolSpec("get_workorder_costs", "workorders", "core", "Labor + material + service + tool actual cost breakdown for a WO.", tool_models.GetWorkorderCostsArgs),

    # ── inventory ────────────────────────────────────────────────────────────
    "check_stock_level": ToolSpec("check_stock_level", "inventory", "core", "Check stock levels for an item.", tool_models.CheckStockLevelArgs),
    "list_low_stock_items": ToolSpec("list_low_stock_items", "inventory", "core", "List inventory items at or below reorder point.", tool_models.ListLowStockItemsArgs),
    "get_reorder_recommendations": ToolSpec("get_reorder_recommendations", "inventory", "core", "Get prioritized reorder recommendations.", tool_models.GetReorderRecommendationsArgs),
    "list_items": ToolSpec("list_items", "inventory", "core", "List item-master records (catalog).", tool_models.ListItemsArgs),
    "get_item": ToolSpec("get_item", "inventory", "core", "Get item-master details for an item number.", tool_models.GetItemArgs),
    "list_storerooms": ToolSpec("list_storerooms", "inventory", "core", "List storeroom locations for a site.", tool_models.ListStoreroomsArgs),
    "get_inventory_valuation": ToolSpec("get_inventory_valuation", "inventory", "core", "Total inventory valuation plus top-N items by line value.", tool_models.GetInventoryValuationArgs),
    "get_critical_spares_check": ToolSpec("get_critical_spares_check", "inventory", "core", "Stockout risk for spare parts of critical assets.", tool_models.GetCriticalSparesCheckArgs),

    # ── purchasing ───────────────────────────────────────────────────────────
    "get_purchase_order": ToolSpec("get_purchase_order", "purchasing", "core", "Get full purchase order details.", tool_models.GetPurchaseOrderArgs),
    "list_purchase_orders": ToolSpec("list_purchase_orders", "purchasing", "core", "List purchase orders with filters.", tool_models.ListPurchaseOrdersArgs),
    "list_vendors": ToolSpec("list_vendors", "purchasing", "core", "List vendor / company records.", tool_models.ListVendorsArgs),
    "list_purchase_requisitions": ToolSpec("list_purchase_requisitions", "purchasing", "core", "List purchase requisitions (upstream of POs).", tool_models.ListPurchaseRequisitionsArgs),
    "get_spend_analysis": ToolSpec("get_spend_analysis", "purchasing", "advanced", "Aggregate spend by vendor / status / worktype.", tool_models.GetSpendAnalysisArgs),
    "get_vendor_performance": ToolSpec("get_vendor_performance", "purchasing", "core", "Analyze vendor delivery and quality metrics.", tool_models.GetVendorPerformanceArgs),

    # ── AI intelligence ──────────────────────────────────────────────────────
    "nl_to_oslc_query": ToolSpec("nl_to_oslc_query", "ai", "advanced", "Convert natural language into an OSLC query.", tool_models.NlToOslcQueryArgs),
    "detect_asset_anomalies": ToolSpec("detect_asset_anomalies", "ai", "advanced", "Detect statistical anomalies (>2σ) in asset failure patterns.", tool_models.DetectAssetAnomaliesArgs),
    "suggest_root_cause": ToolSpec("suggest_root_cause", "ai", "advanced", "Suggest probable root causes from failure history (LLM-enhanced when available).", tool_models.SuggestRootCauseArgs),
    "summarize_asset_health": ToolSpec("summarize_asset_health", "ai", "advanced", "Generate an asset health score (0-100) with key issues and recommendations.", tool_models.SummarizeAssetHealthArgs),

    # ── Wave 8: AI moat (LLM-enhanced w/ rule-based fallback) ────────────────
    "generate_workorder_summary": ToolSpec("generate_workorder_summary", "ai_moat", "advanced", "Natural-language summary of a work order with structured timeline + resolution.", tool_models.GenerateWorkorderSummaryArgs),
    "auto_classify_failure": ToolSpec("auto_classify_failure", "ai_moat", "advanced", "Pick the best-fit failure code from a free-text problem description.", tool_models.AutoClassifyFailureArgs),
    "chat_with_asset": ToolSpec("chat_with_asset", "ai_moat", "advanced", "Conversational Q&A over an asset's full WO + downtime + meter context.", tool_models.ChatWithAssetArgs),
    "recommend_pm_optimization": ToolSpec("recommend_pm_optimization", "ai_moat", "advanced", "Tune PM frequency per asset based on actual failure rate.", tool_models.RecommendPmOptimizationArgs),
    "predict_failure_window": ToolSpec("predict_failure_window", "ai_moat", "advanced", "Statistical next-failure projection from MTBF + time since last failure.", tool_models.PredictFailureWindowArgs),
    "generate_runbook_from_history": ToolSpec("generate_runbook_from_history", "ai_moat", "advanced", "Synthesise a step-by-step runbook from past WO resolutions.", tool_models.GenerateRunbookFromHistoryArgs),

    # ── Wave 9: spatial / GIS (graceful when Maximo Spatial absent) ──────────
    "find_assets_near_location": ToolSpec("find_assets_near_location", "spatial", "advanced", "[Spatial] Radius search for assets around a lat/lon — falls back gracefully when no coordinate fields are populated.", tool_models.FindAssetsNearLocationArgs),
    "get_route_for_technician": ToolSpec("get_route_for_technician", "spatial", "advanced", "[Spatial] Optimised daily route for a technician's open WOs — falls back to priority order when coords unavailable.", tool_models.GetRouteForTechnicianArgs),

    # ── reporting ────────────────────────────────────────────────────────────
    "get_maintenance_kpi_dashboard": ToolSpec("get_maintenance_kpi_dashboard", "reporting", "advanced", "Return a maintenance KPI dashboard.", tool_models.GetMaintenanceKpiDashboardArgs),
    "get_failure_pareto": ToolSpec("get_failure_pareto", "reporting", "advanced", "Pareto chart of failure codes by frequency.", tool_models.GetFailureParetoArgs),
    "get_bad_actor_assets": ToolSpec("get_bad_actor_assets", "reporting", "advanced", "Top-N bad-actor assets by corrective WO count, hours, cost.", tool_models.GetBadActorAssetsArgs),
    "export_workorders_excel": ToolSpec("export_workorders_excel", "reporting", "advanced", "Export work orders to base64-encoded Excel (.xlsx).", tool_models.ExportWorkordersExcelArgs),
    "export_asset_report_pdf": ToolSpec("export_asset_report_pdf", "reporting", "advanced", "Export assets to base64-encoded PDF report.", tool_models.ExportAssetReportPdfArgs),
    "generate_carbon_table": ToolSpec("generate_carbon_table", "reporting", "advanced", "Render data as a Carbon HTML table.", tool_models.GenerateCarbonTableArgs),

    # ── admin ────────────────────────────────────────────────────────────────
    "list_users": ToolSpec("list_users", "admin", "advanced", "List Maximo users.", tool_models.ListUsersArgs),
    "get_user": ToolSpec("get_user", "admin", "advanced", "Get a specific Maximo user.", tool_models.GetUserArgs),
    "query_audit_log": ToolSpec("query_audit_log", "admin", "advanced", "Search the MCP audit log.", tool_models.QueryAuditLogArgs),

    # ── schema / dev ─────────────────────────────────────────────────────────
    "list_object_structures": ToolSpec("list_object_structures", "schema", "advanced", "List available Maximo object structures.", tool_models.ListObjectStructuresArgs),
    "get_schema_details": ToolSpec("get_schema_details", "schema", "advanced", "Describe a Maximo object structure.", tool_models.GetSchemaDetailsArgs),
    "validate_oslc_query": ToolSpec("validate_oslc_query", "schema", "advanced", "Validate an OSLC query against Maximo.", tool_models.ValidateOslcQueryArgs),
    "generate_api_code": ToolSpec("generate_api_code", "schema", "advanced", "Generate example Maximo API code.", tool_models.GenerateApiCodeArgs),

    # ── integrations ─────────────────────────────────────────────────────────
    "list_event_subscriptions": ToolSpec("list_event_subscriptions", "integrations", "advanced", "List active event subscriptions.", tool_models.EmptyArgs),

    # ── labor ────────────────────────────────────────────────────────────────
    "list_labor": ToolSpec("list_labor", "labor", "core", "List labor resources for a site.", tool_models.ListLaborArgs),
    "get_labor_utilization": ToolSpec("get_labor_utilization", "labor", "core", "Calculate labor utilization.", tool_models.GetLaborUtilizationArgs),
    "list_crews": ToolSpec("list_crews", "labor", "core", "List maintenance crews.", tool_models.ListCrewsArgs),
    "list_crafts": ToolSpec("list_crafts", "labor", "core", "List craft / trade master records.", tool_models.ListCraftsArgs),
    "find_available_technician": ToolSpec("find_available_technician", "labor", "core", "List active technicians at a site ordered by open-assignment count.", tool_models.FindAvailableTechnicianArgs),

    # ── locations ────────────────────────────────────────────────────────────
    "list_locations": ToolSpec("list_locations", "locations", "core", "List locations for a site.", tool_models.ListLocationsArgs),
    "get_location": ToolSpec("get_location", "locations", "core", "Get details for a specific location.", tool_models.GetLocationArgs),
    "get_location_hierarchy": ToolSpec("get_location_hierarchy", "locations", "core", "Build a location hierarchy tree.", tool_models.GetLocationHierarchyArgs),

    # ── compliance / EHS ─────────────────────────────────────────────────────
    "list_calibration_due": ToolSpec("list_calibration_due", "compliance", "advanced", "Calibration PMs due in a look-ahead window.", tool_models.ListCalibrationDueArgs),
    "list_inspections_due": ToolSpec("list_inspections_due", "compliance", "advanced", "Open INSP-type WOs due in a look-ahead window.", tool_models.ListInspectionsDueArgs),
    "list_permits_to_work": ToolSpec("list_permits_to_work", "compliance", "advanced", "Permit to Work records (HSE add-on).", tool_models.ListPermitsToWorkArgs),
    "list_certifications_expiring": ToolSpec("list_certifications_expiring", "compliance", "advanced", "Labor qualifications/certs expiring soon.", tool_models.ListCertificationsExpiringArgs),
    "list_incidents": ToolSpec("list_incidents", "compliance", "advanced", "Safety / HSE incidents (MXINCIDENT or SR fallback).", tool_models.ListIncidentsArgs),
    "get_compliance_dashboard": ToolSpec("get_compliance_dashboard", "compliance", "advanced", "Site-wide compliance dashboard composing the other 5 tools.", tool_models.GetComplianceDashboardArgs),

    # ── industry verticals ────────────────────────────────────────────────────
    # Pharma / life sciences
    "get_calibration_audit_trail": ToolSpec("get_calibration_audit_trail", "pharma", "advanced", "[Pharma] Chronological calibration log for an asset (FDA/GxP audit trail).", tool_models.GetCalibrationAuditTrailArgs),
    "list_cleanroom_assets": ToolSpec("list_cleanroom_assets", "pharma", "advanced", "[Pharma] Assets located in cleanrooms by classification keyword.", tool_models.ListCleanroomAssetsArgs),
    "get_gxp_compliance_status": ToolSpec("get_gxp_compliance_status", "pharma", "advanced", "[Pharma] GxP compliance rollup — calibration + certs + incidents → risk score.", tool_models.GetGxpComplianceStatusArgs),
    # Oil & gas
    "get_turnaround_status": ToolSpec("get_turnaround_status", "oilgas", "advanced", "[Oil & Gas] Turnaround status by parent WO grouping.", tool_models.GetTurnaroundStatusArgs),
    "list_pressure_vessels_due": ToolSpec("list_pressure_vessels_due", "oilgas", "advanced", "[Oil & Gas] Pressure-vessel inspections due within window.", tool_models.ListPressureVesselsDueArgs),
    "get_lifting_register": ToolSpec("get_lifting_register", "oilgas", "advanced", "[Oil & Gas] Crane / lifting operations log.", tool_models.GetLiftingRegisterArgs),
    # Manufacturing
    "get_oee": ToolSpec("get_oee", "manufacturing", "advanced", "[Manufacturing] OEE — Availability + flagged Performance/Quality gaps.", tool_models.GetOeeArgs),
    "get_production_line_status": ToolSpec("get_production_line_status", "manufacturing", "advanced", "[Manufacturing] Open WOs + downtime by production-line subtree.", tool_models.GetProductionLineStatusArgs),
    "list_changeover_workorders": ToolSpec("list_changeover_workorders", "manufacturing", "advanced", "[Manufacturing] SMED / changeover WOs with avg duration.", tool_models.ListChangeoverWorkordersArgs),
    # Utilities
    "get_outage_impact_analysis": ToolSpec("get_outage_impact_analysis", "utilities", "advanced", "[Utilities] Downstream impact of an asset outage.", tool_models.GetOutageImpactAnalysisArgs),
    "list_grid_zone_assets": ToolSpec("list_grid_zone_assets", "utilities", "advanced", "[Utilities] Every asset in a grid-zone location.", tool_models.ListGridZoneAssetsArgs),
    "get_reliability_indices": ToolSpec("get_reliability_indices", "utilities", "advanced", "[Utilities] SAIDI / SAIFI proxies from outage WOs.", tool_models.GetReliabilityIndicesArgs),
    # Healthcare
    "list_medical_devices_due": ToolSpec("list_medical_devices_due", "healthcare", "advanced", "[Healthcare] Medical-device assets with PM/cal due.", tool_models.ListMedicalDevicesDueArgs),
    "get_device_lifecycle_status": ToolSpec("get_device_lifecycle_status", "healthcare", "advanced", "[Healthcare] Lifecycle bucket from installdate.", tool_models.GetDeviceLifecycleStatusArgs),
    "get_environment_of_care_status": ToolSpec("get_environment_of_care_status", "healthcare", "advanced", "[Healthcare] Joint Commission EOC rollup.", tool_models.GetEnvironmentOfCareStatusArgs),
    # Transportation
    "get_fleet_readiness": ToolSpec("get_fleet_readiness", "transportation", "advanced", "[Transportation] Fleet readiness % and status mix.", tool_models.GetFleetReadinessArgs),
    "list_mileage_based_pm_due": ToolSpec("list_mileage_based_pm_due", "transportation", "advanced", "[Transportation] PMs tracked against odometer/mileage meter.", tool_models.ListMileageBasedPmDueArgs),
    "get_fuel_consumption_trend": ToolSpec("get_fuel_consumption_trend", "transportation", "advanced", "[Transportation] Fuel consumption trend + spike detection.", tool_models.GetFuelConsumptionTrendArgs),
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
