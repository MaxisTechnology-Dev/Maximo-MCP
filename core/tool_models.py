"""
core/tool_models.py — Pydantic request models for the public tool surface.

Every model inherits StrictBaseModel which sets `extra="forbid"` so unknown
fields raise instead of being silently dropped. Range constraints
(`Field(ge=..., le=...)`) live here too, on top of any in-tool validation.

Adding a new tool? Add the model here and reference it in the corresponding
ToolSpec in `core/tool_catalog.py` so HTTP / FastAPI provider endpoints get
strict input validation for free.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Page-bound helpers ────────────────────────────────────────────────────────
# Page sizes are always (1..200) per MaximoClient.build_oslc_query cap.
# Page numbers are always 1-based.

class _Paginated(StrictBaseModel):
    """Mixin for tools that return paginated lists."""
    page_size: Optional[int] = Field(default=None, ge=1, le=200)
    page_num: int = Field(default=1, ge=1)


# ══════════════════════════════════════════════════════════════════════════════
# CORE / SHARED
# ══════════════════════════════════════════════════════════════════════════════

class EmptyArgs(StrictBaseModel):
    """For tools that take no arguments (e.g. health_check, list_event_subscriptions)."""


# ══════════════════════════════════════════════════════════════════════════════
# ASSETS
# ══════════════════════════════════════════════════════════════════════════════

class ListAssetsArgs(_Paginated):
    site_id: Optional[str] = None
    status: Optional[str] = None
    asset_type: Optional[str] = None


class GetAssetArgs(StrictBaseModel):
    asset_num: str
    site_id: str


class SearchAssetsArgs(_Paginated):
    keyword: str
    site_id: Optional[str] = None
    filters: Optional[Dict[str, str]] = None


class GetAssetHistoryArgs(StrictBaseModel):
    asset_num: str
    site_id: str
    lookback_days: int = Field(default=365, ge=1, le=3650)


class GetAssetDowntimeStatsArgs(StrictBaseModel):
    asset_num: str
    site_id: str
    period_months: int = Field(default=12, ge=1, le=60)


class GetFailureClassHierarchyArgs(StrictBaseModel):
    parent: Optional[str] = None
    page_size: Optional[int] = Field(default=None, ge=1, le=200)


class GetMeterReadingsArgs(StrictBaseModel):
    asset_num: str
    site_id: str
    period_days: int = Field(default=90, ge=1, le=3650)
    page_size: Optional[int] = Field(default=None, ge=1, le=200)


class GetAssetCriticalityMatrixArgs(StrictBaseModel):
    site_id: str
    top_n: int = Field(default=20, ge=1, le=200)


class GetWarrantyStatusArgs(StrictBaseModel):
    site_id: str
    asset_num: Optional[str] = None
    expiring_within_days: int = Field(default=90, ge=1, le=3650)


# ══════════════════════════════════════════════════════════════════════════════
# WORK ORDERS / SERVICE REQUESTS / JOB PLANS
# ══════════════════════════════════════════════════════════════════════════════

class ListWorkordersArgs(_Paginated):
    site_id: Optional[str] = None
    status: Optional[str] = None
    asset_num: Optional[str] = None
    priority: Optional[int] = Field(default=None, ge=1, le=5)
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class GetWorkorderArgs(StrictBaseModel):
    wonum: str
    site_id: str


class GetWorkorderKpisArgs(StrictBaseModel):
    site_id: str
    period_months: int = Field(default=3, ge=1, le=24)


class GetWorkorderTasksArgs(StrictBaseModel):
    wonum: str
    site_id: str


class GetWorkorderCostsArgs(StrictBaseModel):
    wonum: str
    site_id: str


class GetWorkorderActualsVsPlannedArgs(StrictBaseModel):
    wonum: str
    site_id: str


class GetMyAssignedWorkordersArgs(_Paginated):
    labor_code: Optional[str] = None
    site_id: Optional[str] = None
    open_only: bool = True


class ListServiceRequestsArgs(_Paginated):
    site_id: Optional[str] = None
    status: Optional[str] = None
    reported_by: Optional[str] = None


class GetServiceRequestArgs(StrictBaseModel):
    ticket_id: str
    site_id: str


class ListJobPlansArgs(_Paginated):
    site_id: Optional[str] = None
    keyword: Optional[str] = None
    active_only: bool = True


class GetJobPlanArgs(StrictBaseModel):
    jpnum: str
    site_id: Optional[str] = None


class EstimateWorkorderCostArgs(StrictBaseModel):
    jpnum: str
    site_id: Optional[str] = None


class GetScheduleCalendarArgs(StrictBaseModel):
    site_id: str
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    group_by: str = Field(default="date", pattern=r"^(date|flat)$")
    page_size: Optional[int] = Field(default=None, ge=1, le=200)


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY
# ══════════════════════════════════════════════════════════════════════════════

class CheckStockLevelArgs(StrictBaseModel):
    item_num: str
    storeroom: str
    site_id: str


class ListLowStockItemsArgs(StrictBaseModel):
    site_id: str
    storeroom: Optional[str] = None


class GetReorderRecommendationsArgs(StrictBaseModel):
    site_id: str


class ListItemsArgs(_Paginated):
    keyword: Optional[str] = None
    item_type: Optional[str] = None
    commodity_group: Optional[str] = None


class GetItemArgs(StrictBaseModel):
    item_num: str


class ListStoreroomsArgs(StrictBaseModel):
    site_id: str
    active_only: bool = True


class GetInventoryValuationArgs(StrictBaseModel):
    site_id: str
    storeroom: Optional[str] = None
    top_n: int = Field(default=20, ge=1, le=200)


class GetCriticalSparesCheckArgs(StrictBaseModel):
    site_id: str
    priority_threshold: int = Field(default=2, ge=1, le=5)


# ══════════════════════════════════════════════════════════════════════════════
# PURCHASING / VENDORS / PRs
# ══════════════════════════════════════════════════════════════════════════════

class GetPurchaseOrderArgs(StrictBaseModel):
    ponum: str
    site_id: str


class ListPurchaseOrdersArgs(_Paginated):
    site_id: Optional[str] = None
    status: Optional[str] = None
    vendor: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class ListPurchaseRequisitionsArgs(_Paginated):
    site_id: Optional[str] = None
    status: Optional[str] = None
    vendor: Optional[str] = None


class ListVendorsArgs(_Paginated):
    name_filter: Optional[str] = None
    active_only: bool = True


class GetVendorPerformanceArgs(StrictBaseModel):
    vendor_id: str
    period_months: int = Field(default=12, ge=1, le=60)


class GetSpendAnalysisArgs(StrictBaseModel):
    site_id: str
    period_months: int = Field(default=12, ge=1, le=60)
    group_by: str = Field(default="vendor", pattern=r"^(vendor|status|worktype)$")
    top_n: int = Field(default=10, ge=1, le=100)


# ══════════════════════════════════════════════════════════════════════════════
# LABOR / CRAFTS / LOCATIONS
# ══════════════════════════════════════════════════════════════════════════════

class ListLaborArgs(StrictBaseModel):
    site_id: str
    craft: Optional[str] = None
    status: str = "ACTIVE"


class GetLaborUtilizationArgs(StrictBaseModel):
    site_id: str
    labor_code: Optional[str] = None
    period_days: int = Field(default=30, ge=1, le=3650)


class ListCrewsArgs(StrictBaseModel):
    site_id: str


class ListCraftsArgs(_Paginated):
    pass


class FindAvailableTechnicianArgs(StrictBaseModel):
    site_id: str
    craft: Optional[str] = None
    page_size: Optional[int] = Field(default=None, ge=1, le=200)


class ListLocationsArgs(StrictBaseModel):
    site_id: str
    parent_location: Optional[str] = None
    location_type: Optional[str] = None


class GetLocationArgs(StrictBaseModel):
    location: str
    site_id: str


class GetLocationHierarchyArgs(StrictBaseModel):
    site_id: str
    root_location: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# AI INTELLIGENCE
# ══════════════════════════════════════════════════════════════════════════════

class NlToOslcQueryArgs(StrictBaseModel):
    natural_language_query: str
    object_structure: str = "mxwo"
    dry_run: bool = False


class DetectAssetAnomaliesArgs(StrictBaseModel):
    asset_num: str
    site_id: str
    lookback_days: int = Field(default=90, ge=1, le=3650)


class SuggestRootCauseArgs(StrictBaseModel):
    asset_num: str
    site_id: str
    failure_description: str = Field(..., min_length=1, max_length=2000)


class SummarizeAssetHealthArgs(StrictBaseModel):
    asset_num: str
    site_id: str


# ══════════════════════════════════════════════════════════════════════════════
# REPORTING
# ══════════════════════════════════════════════════════════════════════════════

class GetMaintenanceKpiDashboardArgs(StrictBaseModel):
    site_id: str
    period_months: int = Field(default=3, ge=1, le=24)


class GetFailureParetoArgs(StrictBaseModel):
    site_id: str
    asset_num: Optional[str] = None
    period_months: int = Field(default=12, ge=1, le=60)
    top_n: int = Field(default=10, ge=1, le=100)


class GetBadActorAssetsArgs(StrictBaseModel):
    site_id: str
    period_months: int = Field(default=12, ge=1, le=60)
    top_n: int = Field(default=10, ge=1, le=100)


class GenerateCarbonTableArgs(StrictBaseModel):
    object_structure: str
    data: List[Dict[str, Any]]
    columns: List[Dict[str, Any]]


class ExportWorkordersExcelArgs(StrictBaseModel):
    site_id: str
    filters: Optional[Dict[str, str]] = None
    max_records: int = Field(default=1000, ge=1, le=10000)


class ExportAssetReportPdfArgs(StrictBaseModel):
    site_id: str
    asset_group: Optional[str] = None
    max_records: int = Field(default=200, ge=1, le=2000)


# ══════════════════════════════════════════════════════════════════════════════
# COMPLIANCE / EHS
# ══════════════════════════════════════════════════════════════════════════════

class ListCalibrationDueArgs(StrictBaseModel):
    site_id: str
    days_ahead: int = Field(default=30, ge=1, le=3650)


class ListInspectionsDueArgs(StrictBaseModel):
    site_id: str
    days_ahead: int = Field(default=30, ge=1, le=3650)


class ListPermitsToWorkArgs(_Paginated):
    site_id: Optional[str] = None
    status: Optional[str] = None


class ListCertificationsExpiringArgs(StrictBaseModel):
    site_id: Optional[str] = None
    days_ahead: int = Field(default=90, ge=1, le=3650)


class ListIncidentsArgs(_Paginated):
    site_id: Optional[str] = None
    status: Optional[str] = None
    severity: Optional[str] = None


class GetComplianceDashboardArgs(StrictBaseModel):
    site_id: str
    days_ahead: int = Field(default=30, ge=1, le=3650)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════════════════════

class ListUsersArgs(StrictBaseModel):
    site_id: Optional[str] = None


class GetUserArgs(StrictBaseModel):
    user_id: str


class QueryAuditLogArgs(StrictBaseModel):
    tool_name: Optional[str] = None
    user_id: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=10000)


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA / DEV
# ══════════════════════════════════════════════════════════════════════════════

class ListObjectStructuresArgs(StrictBaseModel):
    filter_keyword: Optional[str] = None
    include_custom: bool = True


class GetSchemaDetailsArgs(StrictBaseModel):
    object_structure: str
    include_relationships: bool = True


class ValidateOslcQueryArgs(StrictBaseModel):
    object_structure: str
    where_clause: Optional[str] = None
    select_clause: Optional[str] = None


class GenerateApiCodeArgs(StrictBaseModel):
    object_structure: str
    operation: str = Field(default="list", pattern=r"^(list|get|create|update|delete)$")
    language: str = Field(default="python", pattern=r"^(python|javascript|curl|sql)$")
    where_clause: Optional[str] = None
