"""
tools/pm_scheduling.py — Preventive maintenance scheduling tools for IBM Maximo.
"""

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.audit import get_audit_logger
from core.cache import get_cache
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role

_PM_OS_CANDIDATES = ("/os/mxapipm", "/os/mxpm")
WO_OS = "/os/mxwo"

# Resolved at first successful call; avoids repeated 404 probes.
_pm_os_resolved: Optional[str] = None


async def _get_pm_os() -> str:
    """Return the correct PM object structure path for this Maximo instance."""
    global _pm_os_resolved
    if _pm_os_resolved:
        return _pm_os_resolved
    from core.maximo_client import MaximoAPIError
    client = await get_connected_client()
    for candidate in _PM_OS_CANDIDATES:
        try:
            await client.get(candidate, params={"lean": "1", "oslc.pageSize": 1})
            _pm_os_resolved = candidate
            return _pm_os_resolved
        except MaximoAPIError as exc:
            if getattr(exc, "status_code", 0) == 404:
                continue
            raise
    # Fallback to the standard name; let the caller surface any real error.
    _pm_os_resolved = _PM_OS_CANDIDATES[-1]
    return _pm_os_resolved


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


@require_role("readonly")
async def list_pm_schedules(
    site_id: str,
    asset_num: Optional[str] = None,
    active_only: bool = True,
    page_size: int = 200,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List preventive maintenance schedules for a site.

    Args:
        site_id:     Site ID to list PMs for
        asset_num:   Optional asset filter
        active_only: Only return active PMs (status=ACTIVE)
        page_size:   Records per page
        page_num:    1-based page number

    Returns:
        List of PM records with frequency, next due date, and job plan.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    where_parts = [f'siteid="{oslc_escape(site_id)}"']
    if asset_num:
        where_parts.append(f'assetnum="{oslc_escape(asset_num)}"')
    if active_only:
        where_parts.append('status="ACTIVE"')
    where = " and ".join(where_parts)

    cache_key = f"maximo:pm:{site_id}:{asset_num}:{active_only}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        pm_os = await _get_pm_os()
        params = client.build_oslc_query(
            where=where,
            select="pmnum,description,assetnum,siteid,status,frequency,frequnit,nextduedate,jpnum,lastcompdate",
            order_by="+nextduedate",
            page_size=page_size,
            page_num=page_num,
            collectioncount=1,
        )
        return await client.get(pm_os, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=600)
        members = data.get("member", [])
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"pm_schedules": members, "totalCount": data.get("totalCount", len(members))},
            cached=cached, duration_ms=duration_ms, record_count=len(members)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("supervisor")
async def generate_pm_workorders(
    site_id: str,
    date_range_days: int = 30,
) -> Dict[str, Any]:
    """
    Trigger generation of PM work orders for the next N days.
    Calls the Maximo PM generation action endpoint.

    Args:
        site_id:         Site to generate PMs for
        date_range_days: Generate PMs due within this many days (default: 30)

    Returns:
        Count of work orders generated and their wonums.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")
    if date_range_days < 1 or date_range_days > 365:
        return _error("date_range_days must be between 1 and 365", "VALIDATION_ERROR")

    start = time.monotonic()
    target_date = (datetime.now() + timedelta(days=date_range_days)).strftime("%Y-%m-%dT23:59:59+00:00")
    audit = get_audit_logger()

    try:
        client = await get_connected_client()
        # Maximo PM generation via action=wsmethod:generatePMWO
        body = {
            "siteid": site_id,
            "targetdate": target_date,
        }
        pm_os = await _get_pm_os()
        result = await client.post(
            f"{pm_os}?action=wsmethod:generatePMWO",
            body=body
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate(f"maximo:pm:{site_id}:*")
        inputs = {"site_id": site_id, "date_range_days": date_range_days}
        envelope = _envelope(
            {"message": f"PM generation triggered for site {site_id}", "target_date": target_date, "response": result},
            duration_ms=duration_ms
        )
        await audit.record("generate_pm_workorders", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_pm_forecast(
    site_id: str,
    months_ahead: int = 3,
) -> Dict[str, Any]:
    """
    Forecast upcoming PMs with estimated labor and material costs.

    Args:
        site_id:      Site ID to forecast
        months_ahead: How many months to forecast (default: 3)

    Returns:
        Month-by-month forecast with PM count, estimated hours, and cost.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    now = datetime.now()
    forecast_end = (now + timedelta(days=months_ahead * 30)).strftime("%Y-%m-%dT23:59:59+00:00")

    try:
        client = await get_connected_client()
        pm_os = await _get_pm_os()
        # Get PMs due within forecast window
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}" and status="ACTIVE" and nextduedate<="{forecast_end}"',
            select="pmnum,description,assetnum,nextduedate,frequency,frequnit,estdur,estlabhrs",
            order_by="+nextduedate",
            page_size=500,
        )
        data = await client.get(pm_os, params=params)
        pms: List[Dict] = data.get("member", [])

        # Group by month
        monthly: Dict[str, Dict] = {}
        for pm in pms:
            due = pm.get("nextduedate", "")
            if not due:
                continue
            try:
                due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                month_key = due_dt.strftime("%Y-%m")
            except Exception:
                continue

            if month_key not in monthly:
                monthly[month_key] = {"month": month_key, "pm_count": 0, "est_labor_hrs": 0.0, "pm_list": []}

            monthly[month_key]["pm_count"] += 1
            monthly[month_key]["est_labor_hrs"] += float(pm.get("estlabhrs", 0) or 0)
            monthly[month_key]["pm_list"].append({
                "pmnum": pm.get("pmnum"),
                "description": pm.get("description"),
                "assetnum": pm.get("assetnum"),
                "nextduedate": due,
            })

        forecast = sorted(monthly.values(), key=lambda x: x["month"])
        total_pms = sum(m["pm_count"] for m in forecast)
        total_hrs = round(sum(m["est_labor_hrs"] for m in forecast), 2)

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "months_ahead": months_ahead,
                "total_scheduled_pms": total_pms,
                "total_estimated_labor_hrs": total_hrs,
                "monthly_forecast": forecast,
            },
            duration_ms=duration_ms
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("supervisor")
async def update_pm_frequency(
    pm_num: str,
    site_id: str,
    frequency: int,
    frequency_unit: str = "DAYS",
) -> Dict[str, Any]:
    """
    Update the frequency interval for a PM schedule.

    Args:
        pm_num:         PM record number
        site_id:        Site ID
        frequency:      Numeric frequency value (e.g., 30)
        frequency_unit: Unit: DAYS, WEEKS, MONTHS, HOURS, METERS

    Returns:
        Updated PM record.
    """
    if not pm_num or not site_id:
        return _error("pm_num and site_id are required", "VALIDATION_ERROR")
    if frequency < 1:
        return _error("frequency must be >= 1", "VALIDATION_ERROR")
    valid_units = {"DAYS", "WEEKS", "MONTHS", "HOURS", "METERS"}
    if frequency_unit.upper() not in valid_units:
        return _error(f"frequency_unit must be one of {valid_units}", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()

    try:
        client = await get_connected_client()
        pm_os = await _get_pm_os()
        params = client.build_oslc_query(where=f'pmnum="{oslc_escape(pm_num)}" and siteid="{oslc_escape(site_id)}"')
        existing = await client.get(pm_os, params=params)
        members = existing.get("member", [])
        if not members:
            return _error(f"PM '{pm_num}' not found in site '{site_id}'", "NOT_FOUND")

        href = members[0].get("href", pm_os)
        body = {"frequency": frequency, "frequnit": frequency_unit.upper()}
        result = await client.patch(href, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate(f"maximo:pm:{site_id}:*")
        inputs = {"pm_num": pm_num, "site_id": site_id, "frequency": frequency, "frequency_unit": frequency_unit}
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("update_pm_frequency", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
