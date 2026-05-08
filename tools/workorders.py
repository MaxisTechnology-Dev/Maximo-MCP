"""
tools/workorders.py — Work order full lifecycle management for IBM Maximo.

list_workorders() is a thin wrapper over core.generic_oslc.query_object_structure().
All other functions (get, create, update, approve, cancel, close, assign, KPIs)
retain direct client access because they perform write operations or multi-step
logic that the generic read engine does not cover.
"""

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.audit import get_audit_logger
from core.cache import get_cache
from core.generic_oslc import query_object_structure
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role

# Kept for write operations and single-record lookups that must target
# the primary object structure directly.
WO_OS = "/os/mxwo"
SR_OS = "/os/mxsr"
# Job Plan OS naming varies by Maximo build — try mxjobplan first (legacy),
# fall back to mxapijobplan (MAS 9.x).
JP_OS_CANDIDATES = ("/os/mxjobplan", "/os/mxapijobplan")

# Status values treated as "open" (not COMP / CAN / CLOSE) for status=OPEN filter.
# Keep this list small so OSLC queries stay within typical DB timeouts on large WO tables.
_OPEN_WO_STATUSES = (
    "WAPPR",
    "APPR",
    "WMATL",
    "WSCH",
    "INPRG",
    "WPCOND",
)

# Approved pipeline excluding WAPPR; excludes terminal COMP/COMPLETE/CLOSE/CAN by omission.
_APPROVED_PENDING_STATUSES = (
    "APPR",
    "WMATL",
    "WSCH",
    "INPRG",
    "WPCOND",
)

DEFAULT_WO_FIELDS = (
    "wonum,description,status,priority,assetnum,siteid,worktype,"
    "reportdate,schedstart,actfinish,actlabhrs,reportedby,location,failurecode"
)


def _resolve_page_size(page_size: Optional[int]) -> int:
    from core.settings import get_settings

    settings = get_settings()
    if page_size is None:
        if getattr(settings, "VPN_SAFE_MODE", False):
            return max(1, min(int(getattr(settings, "DEFAULT_PAGE_SIZE", 20) or 20), 200))
        return 50
    return max(1, min(int(page_size), 200))


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


def _parse_dt_loose(val: Any) -> Optional[datetime]:
    """Parse ISO-ish date/time from API or query params."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return datetime.fromisoformat(s + "T00:00:00")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


async def _list_approved_pending_with_wopriority(
    site_id: Optional[str],
    asset_num: Optional[str],
    priority: int,
    date_from: Optional[str],
    date_to: Optional[str],
    page_size: int,
    page_num: int,
    start: float,
) -> Dict[str, Any]:
    """
    APPROVED_PENDING + wopriority + optional dates: combined OSLC where often fails
    (ReadError) on some Maximo builds; fetch by wopriority only then filter in Python.

    Uses query_object_structure() so the correct object structure is resolved
    automatically with fallback support.
    """
    # Build simple filters; status expansion happens after fetch in Python.
    simple_filters: Dict[str, Any] = {"priority": priority}
    if site_id:
        simple_filters["site_id"] = site_id
    if asset_num:
        simple_filters["asset_num"] = asset_num

    all_rows: List[Dict[str, Any]] = []
    page = 1
    api_total: Optional[int] = None

    while page < 500:
        result = await query_object_structure(
            entity="workorder",
            filters=simple_filters,
            page_size=500,
            page_num=page,
            order_by="-reportdate",
            collectioncount=1 if page == 1 else 0,
        )

        if "error" in result:
            return _error(result.get("detail", result["error"]), result["error"])

        if api_total is None:
            api_total = result.get("totalCount")

        chunk = result.get("data", [])
        if isinstance(chunk, dict):
            chunk = [chunk]
        all_rows.extend(chunk)

        if not chunk or len(chunk) < 500:
            break
        if api_total is not None and len(all_rows) >= api_total:
            break
        page += 1

    approved_set = set(_APPROVED_PENDING_STATUSES)
    df = _parse_dt_loose(date_from) if date_from else None
    dt = _parse_dt_loose(date_to) if date_to else None

    filtered: List[Dict[str, Any]] = []
    for wo in all_rows:
        if wo.get("status") not in approved_set:
            continue
        rd = _parse_dt_loose(wo.get("reportdate"))
        if df and rd and rd < df:
            continue
        if dt and rd and rd > dt:
            continue
        filtered.append(wo)

    filtered.sort(key=lambda w: (w.get("reportdate") or ""), reverse=True)
    total = len(filtered)
    start_idx = (page_num - 1) * page_size
    page_rows = filtered[start_idx : start_idx + page_size]
    duration_ms = int((time.monotonic() - start) * 1000)
    return _envelope(
        {"workorders": page_rows, "totalCount": total},
        cached=False,
        duration_ms=duration_ms,
        record_count=len(page_rows),
    )


@require_role("readonly")
async def list_workorders(
    site_id: Optional[str] = None,
    status: Optional[str] = None,
    asset_num: Optional[str] = None,
    priority: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List work orders with optional filters. Supports pagination.

    Thin wrapper over core.generic_oslc.query_object_structure().
    Object structure selection (mxwo / mxapiwodetail / zwoapi) is
    handled automatically by the generic engine with probe-based fallback.

    Args:
        site_id:   Filter by site ID
        status:    Single status, OPEN, APPROVED_PENDING, or a domain value
        asset_num: Filter by asset number
        priority:  Filter by priority (1=Emergency … 5=Low)
        date_from: Filter WOs reported on or after this ISO date
        date_to:   Filter WOs reported on or before this ISO date
        page_size: Records per page
        page_num:  Page number (1-based)

    Returns:
        Paginated list of work orders with totalCount.
    """
    start = time.monotonic()
    page_size = _resolve_page_size(page_size)

    # Special case: APPROVED_PENDING + priority requires multi-page Python filter
    # because combined OSLC where clauses fail on certain Maximo builds.
    if (
        status
        and status.strip().upper() == "APPROVED_PENDING"
        and priority is not None
    ):
        return await _list_approved_pending_with_wopriority(
            site_id, asset_num, priority, date_from, date_to, page_size, page_num, start
        )

    # --- Build where_extra for complex status expressions -----------------
    # The generic engine handles simple equality filters; OPEN / APPROVED_PENDING
    # expand to `status in [...]` which requires the pre-built fragment path.
    where_extra_parts: List[str] = []
    status_filter_handled = False  # True when status is included in where_extra

    if status:
        su = status.strip().upper()
        if su == "OPEN":
            inner = ",".join(f'"{s}"' for s in _OPEN_WO_STATUSES)
            where_extra_parts.append(f"status in [{inner}]")
            status_filter_handled = True
        elif su == "APPROVED_PENDING":
            inner = ",".join(f'"{s}"' for s in _APPROVED_PENDING_STATUSES)
            where_extra_parts.append(f"status in [{inner}]")
            status_filter_handled = True

    if date_from:
        where_extra_parts.append(f'reportdate>="{oslc_escape(date_from)}"')
    if date_to:
        where_extra_parts.append(f'reportdate<="{oslc_escape(date_to)}"')

    where_extra = " and ".join(where_extra_parts) if where_extra_parts else None

    # --- Simple equality filters (alias-mapped by generic engine) ----------
    simple_filters: Dict[str, Any] = {}
    if site_id:
        simple_filters["site_id"] = site_id
    if not status_filter_handled and status:
        simple_filters["status"] = status.strip()
    if asset_num:
        simple_filters["asset_num"] = asset_num
    if priority is not None:
        simple_filters["priority"] = priority  # registry alias: priority → wopriority

    cache_key = (
        f"maximo:workorders:{site_id}:{status}:{asset_num}:{priority}:"
        f"{date_from}:{date_to}:{page_size}:{page_num}"
    )
    cache = get_cache()

    async def fetch() -> Dict[str, Any]:
        return await query_object_structure(
            entity="workorder",
            filters=simple_filters,
            page_size=page_size,
            page_num=page_num,
            order_by="-reportdate",
            where_extra=where_extra,
            collectioncount=1,
        )

    try:
        result, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)

        if "error" in result:
            return _error(result.get("detail", result["error"]), result["error"])

        members = result.get("data", [])
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"workorders": members, "totalCount": result.get("totalCount", len(members))},
            cached=cached,
            duration_ms=duration_ms,
            record_count=len(members),
        )
    except Exception as exc:
        msg = str(exc) or repr(exc)
        return _error(f"Unexpected error: {msg}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_workorder(wonum: str, site_id: str) -> Dict[str, Any]:
    """
    Get complete details for a specific work order.

    Args:
        wonum:   Work order number
        site_id: Site ID

    Returns:
        Full work order record including assignments and labor.
    """
    if not wonum or not site_id:
        return _error("wonum and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:wo:{site_id}:{wonum}"
    cache = get_cache()

    async def fetch():
        # Single-condition WHERE only — compound clauses cause transport errors on this instance.
        # Fetch by wonum only, post-filter by siteid in Python.
        return await query_object_structure(
            entity="workorder",
            filters={"wonum": wonum},
            select=["wonum", "description", "status", "siteid", "assetnum", "location", "wopriority", "worktype", "reportdate", "reportedby", "actlabhrs", "targcompdate", "failurecode"],
            page_size=5,
        )

    try:
        result, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        if "error" in result:
            return _error(result.get("detail", result["error"]), result["error"])
        members = [
            m for m in result.get("data", [])
            if m.get("siteid", "").upper() == site_id.upper()
        ]
        if not members:
            return _error(f"Work order '{wonum}' not found in site '{site_id}'", "NOT_FOUND")
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(members[0], cached=cached, duration_ms=duration_ms)
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("technician")
async def create_workorder(
    description: str,
    asset_num: str,
    site_id: str,
    priority: int = 3,
    work_type: str = "CM",
    reported_by: Optional[str] = None,
    location: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a new work order in Maximo.

    Args:
        description: Work order description
        asset_num:   Asset requiring maintenance
        site_id:     Site ID
        priority:    Priority 1-5 (1=Emergency, 5=Low)
        work_type:   Work type code (CM=Corrective, PM=Preventive, EM=Emergency)
        reported_by: Person reporting the issue
        location:    Location code (auto-populated from asset if omitted)
        notes:       Additional work notes / long description

    Returns:
        Created work order with assigned wonum.
    """
    if not description or not asset_num or not site_id:
        return _error("description, asset_num, and site_id are required", "VALIDATION_ERROR")
    if not 1 <= priority <= 5:
        return _error("priority must be between 1 and 5", "VALIDATION_ERROR")

    start = time.monotonic()
    body: Dict[str, Any] = {
        "description": description[:100],
        "assetnum": asset_num,
        "siteid": site_id.upper(),
        "wopriority": priority,
        "worktype": work_type,
        "status": "WAPPR",
        "reportdate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }
    if reported_by:
        body["reportedby"] = reported_by
    if location:
        body["location"] = location
    if notes:
        body["description_longdescription"] = notes

    audit = get_audit_logger()
    try:
        client = await get_connected_client()
        result = await client.post(WO_OS, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate(f"maximo:workorders:{site_id}:*")
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("create_workorder", body, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("technician")
async def update_workorder(
    wonum: str,
    site_id: str,
    description: Optional[str] = None,
    priority: Optional[int] = None,
    location: Optional[str] = None,
    asset_num: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update fields on an existing work order.

    Args:
        wonum:       Work order number
        site_id:     Site ID
        description: New description
        priority:    New priority (1-5)
        location:    New location code
        asset_num:   New asset number
        notes:       Updated long description / notes

    Returns:
        Updated work order record.
    """
    if not wonum or not site_id:
        return _error("wonum and site_id are required", "VALIDATION_ERROR")

    body: Dict[str, Any] = {}
    if description is not None:
        body["description"] = description[:100]
    if priority is not None:
        body["wopriority"] = priority
    if location is not None:
        body["location"] = location
    if asset_num is not None:
        body["assetnum"] = asset_num
    if notes is not None:
        body["description_longdescription"] = notes

    if not body:
        return _error("No fields provided to update", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()
    try:
        client = await get_connected_client()
        params = client.build_oslc_query(where=f'wonum="{oslc_escape(wonum)}" and siteid="{oslc_escape(site_id)}"')
        existing = await client.get(WO_OS, params=params)
        members = existing.get("member", [])
        if not members:
            return _error(f"Work order '{wonum}' not found", "NOT_FOUND")

        href = members[0].get("href", WO_OS)
        result = await client.patch(href, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate(f"maximo:wo:{site_id}:{wonum}")
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("update_workorder", {"wonum": wonum, "site_id": site_id, **body}, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("supervisor")
async def approve_workorder(wonum: str, site_id: str) -> Dict[str, Any]:
    """
    Approve a work order by changing its status to APPR.

    Args:
        wonum:   Work order number
        site_id: Site ID

    Returns:
        Confirmation of status change.
    """
    return await _change_wo_status(wonum, site_id, "APPR", "approve_workorder")


@require_role("supervisor")
async def cancel_workorder(wonum: str, site_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """
    Cancel a work order by changing its status to CAN.

    Args:
        wonum:   Work order number
        site_id: Site ID
        reason:  Cancellation reason

    Returns:
        Confirmation of cancellation.
    """
    body: Dict[str, Any] = {}
    if reason:
        body["description_longdescription"] = reason
    return await _change_wo_status(wonum, site_id, "CAN", "cancel_workorder", extra_body=body)


@require_role("supervisor")
async def assign_technician(
    wonum: str,
    site_id: str,
    labor_code: str,
    craft: Optional[str] = None,
    start_date: Optional[str] = None,
    hours_planned: float = 8.0,
    hours: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Assign a technician to a work order.

    Args:
        wonum:         Work order number
        site_id:       Site ID
        labor_code:    Labor (person) code to assign
        craft:         Craft/skill code (optional; Maximo derives it from labor record if omitted)
        start_date:    Planned start date (ISO format)
        hours_planned: Estimated hours needed (alias: hours)
        hours:         Alias for hours_planned

    Returns:
        Updated work order with labor assignment.
    """
    if not all([wonum, site_id, labor_code]):
        return _error("wonum, site_id, and labor_code are required", "VALIDATION_ERROR")

    actual_hours = hours if hours is not None else hours_planned
    start_date = start_date or datetime.now().strftime("%Y-%m-%dT08:00:00+00:00")
    start = time.monotonic()
    audit = get_audit_logger()

    try:
        client = await get_connected_client()
        # Add labor assignment as a child record
        params = client.build_oslc_query(where=f'wonum="{oslc_escape(wonum)}" and siteid="{oslc_escape(site_id)}"')
        existing = await client.get(WO_OS, params=params)
        members = existing.get("member", [])
        if not members:
            return _error(f"Work order '{wonum}' not found", "NOT_FOUND")

        href = members[0].get("href", WO_OS)
        labor_entry: Dict[str, Any] = {
            "laborcode": labor_code,
            "laborhrs": actual_hours,
            "startdate": start_date,
        }
        if craft:
            labor_entry["craft"] = craft
        assignment_body: Dict[str, Any] = {"wplabor": [labor_entry]}
        result = await client.patch(href, body=assignment_body)
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate(f"maximo:wo:{site_id}:{wonum}")
        inputs = {"wonum": wonum, "site_id": site_id, "labor_code": labor_code, "craft": craft}
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("assign_technician", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("technician")
async def close_workorder(
    wonum: str,
    site_id: str,
    actual_hours: float = 0.0,
    failure_code: Optional[str] = None,
    resolution_notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Close a work order by setting status to COMP with actual hours and resolution.

    Args:
        wonum:            Work order number
        site_id:          Site ID
        actual_hours:     Actual hours worked
        failure_code:     Maximo failure code
        resolution_notes: Description of what was done

    Returns:
        Closed work order record.
    """
    extra_body: Dict[str, Any] = {"actlabhrs": actual_hours}
    if failure_code:
        extra_body["failurecode"] = failure_code
    if resolution_notes:
        extra_body["description_longdescription"] = resolution_notes
    return await _change_wo_status(wonum, site_id, "COMP", "close_workorder", extra_body=extra_body)


async def _change_wo_status(
    wonum: str,
    site_id: str,
    new_status: str,
    tool_name: str,
    extra_body: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Internal helper to change work order status."""
    if not wonum or not site_id:
        return _error("wonum and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(where=f'wonum="{oslc_escape(wonum)}" and siteid="{oslc_escape(site_id)}"')
        existing = await client.get(WO_OS, params=params)
        members = existing.get("member", [])
        if not members:
            return _error(f"Work order '{wonum}' not found", "NOT_FOUND")

        href = members[0].get("href", WO_OS)
        body: Dict[str, Any] = {"status": new_status, **(extra_body or {})}
        result = await client.patch(href, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate(f"maximo:wo:{site_id}:{wonum}")
        inputs = {"wonum": wonum, "site_id": site_id, "new_status": new_status}
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record(tool_name, inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("supervisor")
async def get_workorder_kpis(site_id: str, period_months: int = 3) -> Dict[str, Any]:
    """
    Compute work order KPIs for a site over the specified period.

    Computes: total WOs, avg completion time, overdue count,
    backlog count, priority breakdown, and top 5 assets by WO count.

    Args:
        site_id:       Site ID to analyse
        period_months: Analysis period in months (default: 3)

    Returns:
        KPI summary with charts-ready data.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=period_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00")

    try:
        # Single-condition WHERE only — drop date filter to avoid compound WHERE transport errors.
        # order_by="-reportdate" + page_size=50 fetches the 50 most recent WOs; Python filters by date.
        wo_result = await query_object_structure(
            entity="workorder",
            filters={"site_id": site_id},
            select=["wonum", "status", "siteid", "reportdate", "actlabhrs", "targcompdate", "wopriority", "assetnum", "actfinish", "schedfinish"],
            order_by="-reportdate",
            page_size=50,
        )
        if "error" in wo_result:
            return _error(wo_result.get("detail", wo_result["error"]), wo_result["error"])
        wos: List[Dict] = [
            w for w in wo_result.get("data", [])
            if w.get("reportdate", "") >= cutoff
        ]

        total = len(wos)
        completed = [w for w in wos if w.get("status") == "COMP"]
        backlog = [w for w in wos if w.get("status") not in ("COMP", "CAN", "CLOSE")]
        overdue = [
            w for w in backlog
            if w.get("schedfinish") and w["schedfinish"] < now_str
        ]

        # Avg completion time
        completion_times = []
        for wo in completed:
            try:
                start_dt = datetime.fromisoformat(wo["reportdate"].replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(wo["actfinish"].replace("Z", "+00:00"))
                completion_times.append((end_dt - start_dt).total_seconds() / 3600)
            except Exception:
                pass
        avg_completion_hrs = round(sum(completion_times) / len(completion_times), 2) if completion_times else 0

        # Priority breakdown
        priority_breakdown: Dict[str, int] = {}
        for wo in wos:
            p = str(wo.get("wopriority", wo.get("priority", "Unknown")))
            priority_breakdown[p] = priority_breakdown.get(p, 0) + 1

        # Top assets by WO count
        asset_wo_count: Dict[str, int] = {}
        for wo in wos:
            a = wo.get("assetnum", "UNKNOWN")
            asset_wo_count[a] = asset_wo_count.get(a, 0) + 1
        top_assets = sorted(asset_wo_count.items(), key=lambda x: x[1], reverse=True)[:5]

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "period_months": period_months,
                "total_workorders": total,
                "completed": len(completed),
                "backlog": len(backlog),
                "overdue": len(overdue),
                "avg_completion_hrs": avg_completion_hrs,
                "priority_breakdown": priority_breakdown,
                "top_assets_by_wo_count": [{"asset": a, "count": c} for a, c in top_assets],
            },
            duration_ms=duration_ms
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_service_requests(
    site_id: Optional[str] = None,
    status: Optional[str] = None,
    reported_by: Optional[str] = None,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List service requests (SRs) — the upstream intake records that typically
    convert to work orders. Single-condition WHERE; remaining filters applied
    in Python.

    Args:
        site_id:     Filter by site
        status:      SR status (NEW, QUEUED, INPROG, RESOLVED, CLOSED, ...)
        reported_by: Person who logged the SR
        page_size:   Records per page (default 50, max 200)
        page_num:    1-based page number
    """
    start = time.monotonic()
    page_size = _resolve_page_size(page_size)

    if status:
        where = f'status="{oslc_escape(status)}"'
    elif site_id:
        where = f'siteid="{oslc_escape(site_id)}"'
    else:
        where = None

    cache_key = f"maximo:sr_list:{site_id}:{status}:{reported_by}:{page_size}:{page_num}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=where,
            select="ticketid,description,status,siteid,reportedby,reportdate,assetnum,location,worklog",
            order_by="-reportdate",
            page_size=200,
            collectioncount=1,
        )
        return await client.get(SR_OS, params=params)

    try:
        try:
            data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                return _error(
                    "Service Request object structure not published in this Maximo instance (404 /os/mxsr).",
                    "NOT_FOUND",
                )
            raise

        members: List[Dict] = data.get("member", [])

        def _matches(sr: Dict) -> bool:
            if site_id and (sr.get("siteid") or "").upper() != site_id.upper():
                return False
            if status and (sr.get("status") or "").upper() != status.upper():
                return False
            if reported_by and (sr.get("reportedby") or "").upper() != reported_by.upper():
                return False
            return True

        filtered = [s for s in members if _matches(s)]
        total = len(filtered)
        start_idx = (page_num - 1) * page_size
        page_rows = filtered[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"service_requests": page_rows, "totalCount": total},
            cached=cached, duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_service_request(ticket_id: str, site_id: str) -> Dict[str, Any]:
    """
    Get full details for a specific service request including work log.

    Args:
        ticket_id: SR ticket id
        site_id:   Site ID
    """
    if not ticket_id or not site_id:
        return _error("ticket_id and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:sr:{site_id}:{ticket_id}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'ticketid="{oslc_escape(ticket_id)}"',
            select="ticketid,description,status,siteid,reportedby,reportdate,assetnum,location,worklog,description_longdescription",
            page_size=5,
        )
        return await client.get(SR_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        members = [
            m for m in data.get("member", [])
            if (m.get("siteid") or "").upper() == site_id.upper()
        ]
        if not members:
            return _error(f"Service request '{ticket_id}' not found in site '{site_id}'", "NOT_FOUND")
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(members[0], cached=cached, duration_ms=duration_ms)
    except (MaximoAPIError, MaximoAuthError) as exc:
        msg = str(exc)
        if "404" in msg or "not found" in msg.lower():
            return _error(
                "Service Request object structure not published in this Maximo instance (404 /os/mxsr).",
                "NOT_FOUND",
            )
        return _error(msg)
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_job_plans(
    site_id: Optional[str] = None,
    keyword: Optional[str] = None,
    active_only: bool = True,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List job plans (reusable work templates) used by planners and PM schedules.
    Single-condition WHERE; remaining filters applied in Python.

    Args:
        site_id:     Filter by ownership site (job plans may be site-scoped or global)
        keyword:     Case-insensitive substring against jpnum or description
        active_only: When True, omit job plans whose status is INACTIVE
        page_size:   Records per page (default 50, max 200)
        page_num:    1-based page number
    """
    start = time.monotonic()
    page_size = _resolve_page_size(page_size)

    where = f'siteid="{oslc_escape(site_id)}"' if site_id else None

    cache_key = f"maximo:jp_list:{site_id}:{keyword}:{active_only}:{page_size}:{page_num}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=where,
            select="jpnum,description,siteid,orgid,status,priority,jpduration,worktype",
            order_by="+jpnum",
            page_size=200,
            collectioncount=1,
        )
        last_exc: Optional[Exception] = None
        for endpoint in JP_OS_CANDIDATES:
            try:
                return await client.get(endpoint, params=params)
            except (MaximoAPIError, MaximoAuthError) as exc:
                msg = str(exc)
                if "404" in msg or "not found" in msg.lower():
                    last_exc = exc
                    continue
                raise
        raise last_exc if last_exc else MaximoAPIError("All Job Plan OS candidates failed")

    try:
        try:
            data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=300)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                tried = ", ".join(JP_OS_CANDIDATES)
                return _error(
                    f"Job Plan object structure not published in this Maximo instance (tried: {tried}).",
                    "NOT_FOUND",
                )
            raise

        members: List[Dict] = data.get("member", [])
        kw = (keyword or "").lower()

        def _matches(jp: Dict) -> bool:
            if active_only and (jp.get("status") or "").upper() == "INACTIVE":
                return False
            if kw and kw not in (jp.get("jpnum") or "").lower() and kw not in (jp.get("description") or "").lower():
                return False
            return True

        filtered = [jp for jp in members if _matches(jp)]
        total = len(filtered)
        start_idx = (page_num - 1) * page_size
        page_rows = filtered[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"job_plans": page_rows, "totalCount": total},
            cached=cached, duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_my_assigned_workorders(
    labor_code: Optional[str] = None,
    site_id: Optional[str] = None,
    open_only: bool = True,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List work orders assigned to a specific labor (technician). When
    ``labor_code`` is omitted, the value falls back to the current request
    identity's user id — so an LLM can ask "what's on my plate today" without
    knowing the labor code.

    The query targets the planned-labor relationship on mxwo and excludes
    terminal statuses (COMP/CLOSE/CAN) when ``open_only=True``.

    Args:
        labor_code: Labor (person) code to look up; defaults to the caller's identity
        site_id:    Optional site filter (post-filter)
        open_only:  When True, omit WOs with status COMP / CLOSE / CAN
        page_size:  Records per page (default 50, max 200)
        page_num:   1-based page number
    """
    start = time.monotonic()
    page_size = _resolve_page_size(page_size)

    if not labor_code:
        try:
            from core.identity import resolve_identity
            ident = resolve_identity()
            labor_code = getattr(ident, "user_id", None)
        except Exception:
            labor_code = None
    if not labor_code:
        return _error(
            "labor_code is required (or set X-MCP-User-Id / CURRENT_USER_ID for identity-based lookup)",
            "VALIDATION_ERROR",
        )

    cache_key = f"maximo:my_wo:{labor_code}:{site_id}:{open_only}:{page_size}:{page_num}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        # Single-condition WHERE traversing wplabor; siteid/status filtered in Python.
        params = client.build_oslc_query(
            where=f'wplabor.laborcode="{oslc_escape(labor_code)}"',
            select="wonum,description,status,wopriority,assetnum,siteid,worktype,reportdate,schedstart,targstartdate,targcompdate,location,wplabor",
            order_by="-reportdate",
            page_size=200,
            collectioncount=1,
        )
        return await client.get(WO_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        members: List[Dict] = data.get("member", [])

        terminal = {"COMP", "CLOSE", "CAN"}

        def _matches(wo: Dict) -> bool:
            if site_id and (wo.get("siteid") or "").upper() != site_id.upper():
                return False
            if open_only and (wo.get("status") or "").upper() in terminal:
                return False
            return True

        filtered = [wo for wo in members if _matches(wo)]
        total = len(filtered)
        start_idx = (page_num - 1) * page_size
        page_rows = filtered[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"labor_code": labor_code, "workorders": page_rows, "totalCount": total},
            cached=cached, duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_job_plan(jpnum: str, site_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Get full details for a single job plan including embedded child collections
    (tasks, planned labor, planned material, planned tools, specifications).

    Args:
        jpnum:   Job plan number
        site_id: Optional site filter (Python post-filter)
    """
    if not jpnum:
        return _error("jpnum is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:jp:{jpnum}:{site_id}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        # Note: child collection names use "job*" not "jp*" on this Maximo build.
        params = client.build_oslc_query(
            where=f'jpnum="{oslc_escape(jpnum)}"',
            select="jpnum,description,siteid,orgid,status,priority,jpduration,worktype,jobtask,joblabor,jobmaterial,jobtool,jobplanspec",
            page_size=5,
        )
        last_exc: Optional[Exception] = None
        for endpoint in JP_OS_CANDIDATES:
            try:
                return await client.get(endpoint, params=params)
            except (MaximoAPIError, MaximoAuthError) as exc:
                msg = str(exc)
                if "404" in msg or "not found" in msg.lower():
                    last_exc = exc
                    continue
                raise
        raise last_exc if last_exc else MaximoAPIError("All Job Plan OS candidates failed")

    try:
        try:
            data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=300)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                tried = ", ".join(JP_OS_CANDIDATES)
                return _error(
                    f"Job Plan object structure not published in this Maximo instance (tried: {tried}).",
                    "NOT_FOUND",
                )
            raise

        members: List[Dict] = data.get("member", [])
        if site_id:
            members = [m for m in members if (m.get("siteid") or "").upper() == site_id.upper()]
        if not members:
            return _error(f"Job plan '{jpnum}' not found{' in site ' + site_id if site_id else ''}.", "NOT_FOUND")

        jp = members[0]
        # Friendly summary so an LLM can read the structure at a glance.
        summary = {
            "jpnum": jp.get("jpnum"),
            "description": jp.get("description"),
            "siteid": jp.get("siteid"),
            "duration_hours": jp.get("jpduration"),
            "task_count": len(jp.get("jobtask") or []),
            "planned_labor_lines": len(jp.get("joblabor") or []),
            "planned_material_lines": len(jp.get("jobmaterial") or []),
            "planned_tool_lines": len(jp.get("jobtool") or []),
        }
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"summary": summary, "job_plan": jp},
            cached=cached, duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_workorder_costs(wonum: str, site_id: str) -> Dict[str, Any]:
    """
    Return the labor + material + service + tool actual cost breakdown for
    a single work order. Useful for chargeback, cost-center reports, and
    finance reconciliation.

    Args:
        wonum:   Work order number
        site_id: Site ID (Python post-filter)
    """
    if not wonum or not site_id:
        return _error("wonum and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:wo_costs:{site_id}:{wonum}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'wonum="{oslc_escape(wonum)}"',
            select="wonum,siteid,status,worktype,assetnum,location,actlabhrs,actlabcost,actmatcost,actservcost,acttoolcost,acttotalcost",
            page_size=5,
        )
        return await client.get(WO_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        members = [m for m in data.get("member", []) if (m.get("siteid") or "").upper() == site_id.upper()]
        if not members:
            return _error(f"Work order '{wonum}' not found in site '{site_id}'.", "NOT_FOUND")
        wo = members[0]

        def _f(name: str) -> float:
            return float(wo.get(name) or 0)

        labor = _f("actlabcost")
        material = _f("actmatcost")
        service = _f("actservcost")
        tool = _f("acttoolcost")
        total = _f("acttotalcost") or (labor + material + service + tool)

        breakdown = []
        for cat, amount in (
            ("labor", labor),
            ("material", material),
            ("service", service),
            ("tool", tool),
        ):
            breakdown.append(
                {
                    "category": cat,
                    "amount": round(amount, 2),
                    "share_pct": round((amount / total) * 100, 1) if total else 0,
                }
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "wonum": wonum,
                "site_id": site_id,
                "status": wo.get("status"),
                "assetnum": wo.get("assetnum"),
                "location": wo.get("location"),
                "actual_hours": _f("actlabhrs"),
                "total_cost": round(total, 2),
                "breakdown": breakdown,
            },
            cached=cached, duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_workorder_actuals_vs_planned(wonum: str, site_id: str) -> Dict[str, Any]:
    """
    Compare estimated vs actual labor hours and cost for a work order.
    Returns variance figures a planner can use to spot under/over-runs.

    Args:
        wonum:   Work order number
        site_id: Site ID (Python post-filter)
    """
    if not wonum or not site_id:
        return _error("wonum and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:wo_actuals:{site_id}:{wonum}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'wonum="{oslc_escape(wonum)}"',
            select="wonum,siteid,status,worktype,estlabhrs,actlabhrs,estlabcost,actlabcost,estmatcost,actmatcost,estservcost,actservcost,esttoolcost,acttoolcost,estatapprtotalcost,esttotalcost,acttotalcost",
            page_size=5,
        )
        return await client.get(WO_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        members = [
            m for m in data.get("member", [])
            if (m.get("siteid") or "").upper() == site_id.upper()
        ]
        if not members:
            return _error(f"Work order '{wonum}' not found in site '{site_id}'.", "NOT_FOUND")
        wo = members[0]

        def _f(name: str) -> float:
            return float(wo.get(name) or 0)

        est_hrs, act_hrs = _f("estlabhrs"), _f("actlabhrs")
        est_lab, act_lab = _f("estlabcost"), _f("actlabcost")
        est_mat, act_mat = _f("estmatcost"), _f("actmatcost")
        est_srv, act_srv = _f("estservcost"), _f("actservcost")
        est_tool, act_tool = _f("esttoolcost"), _f("acttoolcost")
        est_total = _f("esttotalcost") or (est_lab + est_mat + est_srv + est_tool)
        act_total = _f("acttotalcost") or (act_lab + act_mat + act_srv + act_tool)

        def _variance(est: float, act: float) -> Dict[str, Any]:
            return {
                "estimated": round(est, 2),
                "actual": round(act, 2),
                "variance_abs": round(act - est, 2),
                "variance_pct": round(((act - est) / est) * 100, 1) if est else None,
                "over_budget": act > est,
            }

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "wonum": wonum,
                "site_id": site_id,
                "status": wo.get("status"),
                "worktype": wo.get("worktype"),
                "labor_hours": _variance(est_hrs, act_hrs),
                "labor_cost": _variance(est_lab, act_lab),
                "material_cost": _variance(est_mat, act_mat),
                "service_cost": _variance(est_srv, act_srv),
                "tool_cost": _variance(est_tool, act_tool),
                "total_cost": _variance(est_total, act_total),
            },
            cached=cached, duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_schedule_calendar(
    site_id: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    group_by: str = "date",
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Return scheduled work orders within a date window grouped by date or
    target start day. Useful for a planner's week-at-a-glance view.

    Args:
        site_id:   Site to fetch
        date_from: ISO date — only include WOs with schedstart on/after this. Default: today
        date_to:   ISO date — only include WOs with schedstart on/before this. Default: today + 14 days
        group_by:  "date" (default) buckets by yyyy-mm-dd of schedstart; any other value returns the raw flat list
        page_size: Records per page (default 200, max 200)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")
    page_size = max(1, min(int(page_size or 200), 200))

    start = time.monotonic()
    now = datetime.now()
    if not date_from:
        date_from = now.strftime("%Y-%m-%d")
    if not date_to:
        date_to = (now + timedelta(days=14)).strftime("%Y-%m-%d")

    # Strip tzinfo from window bounds so we can compare against Maximo dates
    # (which may be naive or aware depending on the column).
    df_raw = _parse_dt_loose(date_from)
    dt_raw = _parse_dt_loose(date_to + "T23:59:59") if len(date_to) == 10 else _parse_dt_loose(date_to)
    df = df_raw.replace(tzinfo=None) if df_raw else None
    dt = dt_raw.replace(tzinfo=None) if dt_raw else None

    cache_key = f"maximo:schedule:{site_id}:{date_from}:{date_to}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="wonum,description,status,siteid,assetnum,wopriority,worktype,schedstart,schedfinish,targstartdate,targcompdate,lead",
            order_by="-reportdate",
            page_size=page_size,
        )
        return await client.get(WO_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        members: List[Dict] = data.get("member", [])

        terminal = {"COMP", "CLOSE", "CAN"}
        in_window: List[Dict] = []
        for w in members:
            if (w.get("status") or "").upper() in terminal:
                continue
            ss_raw = _parse_dt_loose(w.get("schedstart") or w.get("targstartdate"))
            if ss_raw is None:
                continue
            ss = ss_raw.replace(tzinfo=None)  # normalise for comparison
            if df and ss < df:
                continue
            if dt and ss > dt:
                continue
            in_window.append(w)

        if group_by == "date":
            buckets: Dict[str, List[Dict]] = {}
            for w in in_window:
                ss_b = _parse_dt_loose(w.get("schedstart") or w.get("targstartdate"))
                key = ss_b.strftime("%Y-%m-%d") if ss_b else "(no_schedstart)"
                buckets.setdefault(key, []).append(w)
            grouped = [
                {"date": d, "count": len(rows), "workorders": rows}
                for d, rows in sorted(buckets.items())
            ]
            payload = {
                "site_id": site_id,
                "date_from": date_from,
                "date_to": date_to,
                "total_scheduled": len(in_window),
                "by_date": grouped,
            }
        else:
            payload = {
                "site_id": site_id,
                "date_from": date_from,
                "date_to": date_to,
                "total_scheduled": len(in_window),
                "workorders": in_window,
            }

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(payload, cached=cached, duration_ms=duration_ms, record_count=len(in_window))
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def estimate_workorder_cost(jpnum: str, site_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Estimate the labor + material + tool cost of executing a job plan.
    Sums numeric child-collection fields on the Job Plan record so a
    planner sees expected cost before issuing a WO.

    Args:
        jpnum:   Job plan number
        site_id: Optional site filter (Python post-filter)
    """
    jp_resp = await get_job_plan(jpnum=jpnum, site_id=site_id)
    if not jp_resp.get("success"):
        return jp_resp
    jp = jp_resp["data"]["job_plan"]

    def _to_f(v: Any) -> float:
        try:
            return float(v or 0)
        except Exception:
            return 0.0

    labor_hours = labor_cost = 0.0
    for line in jp.get("joblabor") or []:
        labor_hours += _to_f(line.get("laborhrs"))
        labor_cost += _to_f(line.get("laborcost") or line.get("linecost") or line.get("estunitcost"))

    material_cost = 0.0
    material_qty = 0.0
    for line in jp.get("jobmaterial") or []:
        material_qty += _to_f(line.get("itemqty") or line.get("quantity"))
        material_cost += _to_f(line.get("linecost") or line.get("unitcost") or line.get("estunitcost"))

    tool_cost = tool_hours = 0.0
    for line in jp.get("jobtool") or []:
        tool_hours += _to_f(line.get("toolhrs"))
        tool_cost += _to_f(line.get("toolrate") or line.get("linecost"))

    grand_total = round(labor_cost + material_cost + tool_cost, 2)

    return {
        "success": True,
        "data": {
            "jpnum": jp.get("jpnum"),
            "description": jp.get("description"),
            "site_id": jp.get("siteid"),
            "duration_hours": jp.get("jpduration"),
            "labor": {
                "lines": len(jp.get("joblabor") or []),
                "hours": round(labor_hours, 2),
                "cost": round(labor_cost, 2),
            },
            "material": {
                "lines": len(jp.get("jobmaterial") or []),
                "qty": round(material_qty, 2),
                "cost": round(material_cost, 2),
            },
            "tool": {
                "lines": len(jp.get("jobtool") or []),
                "hours": round(tool_hours, 2),
                "cost": round(tool_cost, 2),
            },
            "estimated_total_cost": grand_total,
            "task_count": len(jp.get("jobtask") or []),
        },
        "metadata": jp_resp.get("metadata", {}),
    }


@require_role("readonly")
async def get_workorder_tasks(wonum: str, site_id: str) -> Dict[str, Any]:
    """
    List the task breakdown of a parent work order. Tasks are themselves
    work-order rows whose ``parent`` field references the given ``wonum``.

    Args:
        wonum:   Parent work order number
        site_id: Site ID (used as a Python post-filter)
    """
    if not wonum or not site_id:
        return _error("wonum and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:wo_tasks:{site_id}:{wonum}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'parent="{oslc_escape(wonum)}"',
            select="wonum,parent,taskid,description,status,siteid,assetnum,location,wopriority,schedstart,targcompdate,actfinish,actlabhrs",
            order_by="+taskid",
            page_size=200,
        )
        return await client.get(WO_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        members: List[Dict] = data.get("member", [])
        tasks = [t for t in members if (t.get("siteid") or "").upper() == site_id.upper()]
        tasks.sort(key=lambda t: (t.get("taskid") or 0))
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"parent_wonum": wonum, "site_id": site_id, "tasks": tasks},
            cached=cached, duration_ms=duration_ms, record_count=len(tasks),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
