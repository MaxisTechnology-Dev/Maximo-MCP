"""
tools/labor.py — Labor, crew, and person tracking tools for IBM Maximo.
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.cache import get_cache
from core.generic_oslc import query_object_structure
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role

LABOR_OS = "/os/mxlabor"
PERSON_OS = "/os/mxperson"
CREW_OS = "/os/mxcrew"
CRAFT_OS_CANDIDATES = ("/os/mxcraft", "/os/mxapicraft")
ASSIGNMENT_OS_CANDIDATES = ("/os/mxassignment", "/os/mxapiassignment")


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


@require_role("readonly")
async def list_labor(
    site_id: str,
    craft: Optional[str] = None,
    status: Optional[str] = "ACTIVE",
) -> Dict[str, Any]:
    """
    List available labor resources for a site.

    Args:
        site_id: Site ID
        craft:   Optional craft/skill filter (e.g., "ELECTRICIAN", "MECHANIC")
        status:  Labor status filter (default: ACTIVE)

    Returns:
        List of labor records with person ID, craft, and skill level.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    where_parts = [f'siteid="{oslc_escape(site_id)}"']
    if craft:
        where_parts.append(f'craft="{oslc_escape(craft)}"')
    # NOTE: The standard mxlabor object structure does not expose a top-level `status`
    # attribute reliably across all Maximo versions, so we skip the OSLC filter and
    # apply it in Python after fetching.

    cache_key = f"maximo:labor:{site_id}:{craft}:{status}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=" and ".join(where_parts),
            select="laborcode,personid,siteid,craft,skilllevel,status",
            order_by="+laborcode",
            page_size=5,
        )
        try:
            return await client.get(LABOR_OS, params=params)
        except (MaximoAPIError, MaximoAuthError):
            params["oslc.pageSize"] = 1
            return await client.get(LABOR_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=600)
        members = data.get("member", [])
        # Apply status filter in Python — avoids BMXAA8781E on builds where
        # mxlabor does not expose a top-level status attribute via OSLC.
        if status:
            members = [m for m in members if str(m.get("status", "")).upper() == status.upper()]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"labor": members, "totalCount": len(members)},
            cached=cached, duration_ms=duration_ms, record_count=len(members)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_labor_utilization(
    site_id: str,
    labor_code: Optional[str] = None,
    period_days: int = 30,
) -> Dict[str, Any]:
    """
    Calculate labor utilization metrics (hours worked vs. available).

    Args:
        site_id:     Site ID
        labor_code:  Optional specific labor code; if omitted analyses all
        period_days: Analysis window in days

    Returns:
        Utilization percentages, total hours, and WO breakdown per technician.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - __import__("datetime").timedelta(days=period_days)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        # Single-condition WHERE only (siteid) — compound WHERE causes transport errors.
        # Date and labor_code filters applied in Python after fetch.
        # wplabor dot-notation fields (regularhrs, startdate) do not exist on this Maximo version.
        # Request the wplabor child table without field qualification; Maximo returns all its fields.
        wo_result = await query_object_structure(
            entity="workorder",
            filters={"site_id": site_id},
            select=["wonum", "status", "siteid", "reportdate", "actlabhrs", "wplabor"],
            order_by="-reportdate",
            page_size=20,
        )
        if "error" in wo_result:
            wos: List[Dict] = []
        else:
            wos_raw: List[Dict] = wo_result.get("data", [])
            # Python post-filter by date
            wos = [w for w in wos_raw if w.get("reportdate", "") >= cutoff]

        # Tally hours per labor code; filter by labor_code in Python if specified
        labor_hours: Dict[str, float] = {}
        labor_wo_count: Dict[str, int] = {}
        for wo in wos:
            for assignment in (wo.get("wplabor") or []):
                lc = assignment.get("laborcode", "UNKNOWN")
                if labor_code and lc.upper() != labor_code.upper():
                    continue
                hrs = float(assignment.get("laborhrs", 0) or 0)
                labor_hours[lc] = labor_hours.get(lc, 0) + hrs
                labor_wo_count[lc] = labor_wo_count.get(lc, 0) + 1

        # 8 hours/day working capacity
        available_hrs = period_days * 8
        utilization = []
        for lc, hrs in sorted(labor_hours.items(), key=lambda x: x[1], reverse=True):
            utilization.append({
                "labor_code": lc,
                "hours_worked": round(hrs, 2),
                "wo_count": labor_wo_count.get(lc, 0),
                "available_hours": available_hrs,
                "utilization_pct": round((hrs / available_hrs) * 100, 1) if available_hrs else 0,
            })

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"site_id": site_id, "period_days": period_days, "utilization": utilization},
            duration_ms=duration_ms, record_count=len(utilization)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_crews(site_id: str) -> Dict[str, Any]:
    """
    List maintenance crews and their members for a site.

    Args:
        site_id: Site ID

    Returns:
        Crew records with member list and crew type.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:crews:{site_id}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="crewid,description,crewtype,siteid,crewmember",
            page_size=100,
        )
        return await client.get(CREW_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=600)
        members = data.get("member", [])
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"crews": members, "totalCount": len(members)},
            cached=cached, duration_ms=duration_ms, record_count=len(members)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        # Some instances do not publish crew OSLC object structures.
        if isinstance(exc, MaximoAPIError) and getattr(exc, "status_code", 0) == 404:
            duration_ms = int((time.monotonic() - start) * 1000)
            return _envelope(
                {
                    "crews": [],
                    "totalCount": 0,
                    "not_available": True,
                    "message": "Crew object structure not published in this Maximo instance (404 /os/mxcrew).",
                },
                duration_ms=duration_ms,
                record_count=0,
            )
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


async def _try_candidates(client, candidates, params):
    last_exc: Optional[Exception] = None
    for endpoint in candidates:
        try:
            return await client.get(endpoint, params=params), endpoint
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                last_exc = exc
                continue
            raise
    raise last_exc if last_exc else MaximoAPIError("No candidate endpoint available")


@require_role("readonly")
async def list_crafts(
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List craft records (skill / trade reference data). Inputs for
    `find_available_technician` and `list_labor` come from this catalog.

    Args:
        page_size: Records per page (default 50, max 200)
        page_num:  1-based page number
    """
    page_size = max(1, min(int(page_size or 50), 200))
    start = time.monotonic()

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            select="craft,description,orgid,outsidecraft",
            order_by="+craft",
            page_size=200,
        )
        try:
            result, used = await _try_candidates(client, CRAFT_OS_CANDIDATES, params)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                tried = ", ".join(CRAFT_OS_CANDIDATES)
                return _error(
                    f"Craft object structure not published in this Maximo instance (tried: {tried}).",
                    "NOT_FOUND",
                )
            raise

        members: List[Dict] = result.get("member", [])
        start_idx = (page_num - 1) * page_size
        page_rows = members[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"endpoint": used, "crafts": page_rows, "totalCount": len(members)},
            duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def find_available_technician(
    site_id: str,
    craft: Optional[str] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Return technicians at a site (optionally filtered by craft) ordered
    by their current open-assignment count — least-busy first. The count
    column is best-effort: it queries the assignment object structure and
    falls back to 0 if assignments are not exposed.

    Args:
        site_id:   Site to filter on
        craft:     Optional craft code filter
        page_size: Records per page (default 50, max 200)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")
    page_size = max(1, min(int(page_size or 50), 200))

    start = time.monotonic()

    try:
        client = await get_connected_client()
        # Step 1 — fetch labor. Note: `siteid` is NOT queryable on mxlabor on
        # some Maximo builds (BMXAA8781E "property siteid is not found"); labor
        # records are org-scoped rather than site-scoped. So we filter by
        # `status="ACTIVE"` (or `craft` when given) and let the assignment
        # cross-reference do the site narrowing.
        if craft:
            where = f'craft="{oslc_escape(craft)}"'
        else:
            where = 'status="ACTIVE"'
        labor_params = client.build_oslc_query(
            where=where,
            select="laborcode,personid,siteid,craft,status,statusdate",
            page_size=200,
        )
        labor_data = await client.get(LABOR_OS, params=labor_params)
        site_u = site_id.upper()
        # Post-filter: keep only ACTIVE labor; if siteid is present on the
        # record, narrow to the requested site (otherwise keep — many Maximo
        # builds omit siteid from the labor response).
        labor_rows = []
        for r in labor_data.get("member", []):
            if (r.get("status") or "").upper() != "ACTIVE":
                continue
            row_site = (r.get("siteid") or "").upper()
            if row_site and row_site != site_u:
                continue
            if craft and (r.get("craft") or "").upper() != craft.upper():
                continue
            labor_rows.append(r)

        # Step 2 — best-effort assignment lookup.
        assignment_counts: Dict[str, int] = {}
        try:
            asg_params = client.build_oslc_query(
                where=f'siteid="{oslc_escape(site_id)}"',
                select="laborcode,siteid,status,wonum,scheddate",
                page_size=200,
            )
            asg_result, _ = await _try_candidates(client, ASSIGNMENT_OS_CANDIDATES, asg_params)
            terminal = {"COMP", "CLOSE", "CAN", "CANCELLED"}
            for a in asg_result.get("member", []):
                if (a.get("siteid") or "").upper() != site_u:
                    continue
                if (a.get("status") or "").upper() in terminal:
                    continue
                code = (a.get("laborcode") or "").upper()
                if not code:
                    continue
                assignment_counts[code] = assignment_counts.get(code, 0) + 1
        except (MaximoAPIError, MaximoAuthError):
            assignment_counts = {}

        ranked = []
        for r in labor_rows:
            code = (r.get("laborcode") or "").upper()
            ranked.append(
                {
                    "laborcode": r.get("laborcode"),
                    "personid": r.get("personid"),
                    "craft": r.get("craft"),
                    "status": r.get("status"),
                    "open_assignments": assignment_counts.get(code, 0),
                }
            )
        ranked.sort(key=lambda r: (r["open_assignments"], r.get("laborcode") or ""))

        page_rows = ranked[:page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "craft_filter": craft,
                "total_active_labor": len(ranked),
                "assignment_counts_available": bool(assignment_counts),
                "technicians": page_rows,
            },
            duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
