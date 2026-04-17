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
