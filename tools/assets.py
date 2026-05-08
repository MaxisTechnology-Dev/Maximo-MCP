"""
tools/assets.py — Asset CRUD, lifecycle, health, and search tools for IBM Maximo.

list_assets() is a thin wrapper over core.generic_oslc.query_object_structure().
All other functions (get, create, update, retire, history, downtime, search)
retain direct client access because they perform write operations or specialised
query logic not covered by the generic read engine.

All functions return the standard success/data/metadata envelope.
"""

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.audit import get_audit_logger
from core.cache import get_cache
from core.generic_oslc import query_object_structure
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape, safe_field_name
from core.rbac import require_role
from core.settings import get_settings

# Kept for write operations and single-record lookups.
ASSET_OS = "/os/mxasset"
# Reliability OS endpoints — MAS 9.x naming. Legacy fallbacks tried first.
FAILURELIST_OS_CANDIDATES = ("/os/mxfailure", "/os/mxapifailurelist")
ASSETMETER_OS_CANDIDATES = ("/os/mxassetmeter", "/os/mxapiassetmeter")
DEFAULT_ASSET_FIELDS = (
    "assetnum,description,siteid,status,assettype,serialnum,location,"
    "purchaseprice,installdate,changedate,manufacturer,vendor,parent"
)


def _resolve_page_size(page_size: Optional[int]) -> int:
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


# ── Tool Functions ─────────────────────────────────────────────────────────────

@require_role("readonly")
async def list_assets(
    site_id: Optional[str] = None,
    status: Optional[str] = None,
    asset_type: Optional[str] = None,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List assets with optional filters. Results are paginated and cached.

    Thin wrapper over core.generic_oslc.query_object_structure().
    Object structure selection (mxasset / mxapiasset) is handled
    automatically by the engine with probe-based fallback.

    Args:
        site_id:    Filter by site ID (e.g., "BEDFORD")
        status:     Filter by status (e.g., "OPERATING", "DECOMMISSIONED")
        asset_type: Filter by asset type
        page_size:  Records per page (max 200)
        page_num:   1-based page number

    Returns:
        Paginated list of asset records with totalCount.
    """
    start = time.monotonic()
    settings = get_settings()
    page_size = _resolve_page_size(page_size)

    filters: Dict[str, Any] = {}
    if site_id:
        filters["site_id"] = site_id        # alias → siteid
    if status:
        filters["status"] = status
    if asset_type:
        filters["asset_type"] = asset_type  # alias → assettype

    cache_key = f"maximo:assets:{site_id}:{status}:{asset_type}:{page_size}:{page_num}"
    cache = get_cache()

    async def fetch() -> Dict[str, Any]:
        return await query_object_structure(
            entity="asset",
            filters=filters,
            page_size=page_size,
            page_num=page_num,
            order_by="+assetnum",
            collectioncount=1 if page_num == 1 else 0,
        )

    try:
        result, cached = await cache.get_or_fetch(
            cache_key, fetch, ttl=settings.CACHE_TTL_SECONDS
        )

        if "error" in result:
            return _error(result.get("detail", result["error"]), result["error"])

        members = result.get("data", [])
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"assets": members, "totalCount": result.get("totalCount", len(members))},
            cached=cached,
            duration_ms=duration_ms,
            record_count=len(members),
        )
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_asset(asset_num: str, site_id: str) -> Dict[str, Any]:
    """
    Get full asset details for a specific asset number and site.

    Args:
        asset_num: Asset number (e.g., "PUMP-001")
        site_id:   Site ID (e.g., "BEDFORD")

    Returns:
        Complete asset record with all attributes.
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:asset:{site_id}:{asset_num}"
    cache = get_cache()

    async def fetch():
        # Single-condition WHERE only: this Maximo instance rejects compound WHERE.
        # Use the actual Maximo field name "assetnum" (not the alias "asset_num").
        # Post-filter by siteid in Python.
        return await query_object_structure(
            entity="asset",
            filters={"assetnum": asset_num},
            page_size=5,
        )

    try:
        result, cached = await cache.get_or_fetch(cache_key, fetch, ttl=300)
        if "error" in result:
            return _error(result.get("detail", result["error"]), result["error"])
        members = [
            m for m in result.get("data", [])
            if m.get("siteid", "").upper() == site_id.upper()
        ]
        if not members:
            return _error(f"Asset '{asset_num}' not found in site '{site_id}'", "NOT_FOUND")
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(members[0], cached=cached, duration_ms=duration_ms)
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("admin")
async def create_asset(
    asset_num: str,
    description: str,
    site_id: str,
    location: Optional[str] = None,
    asset_type: Optional[str] = None,
    serial_num: Optional[str] = None,
    purchase_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Create a new asset record in Maximo.

    Args:
        asset_num:      Unique asset number
        description:    Asset description (max 100 chars)
        site_id:        Site ID where asset resides
        location:       Location code
        asset_type:     Asset type classification
        serial_num:     Manufacturer serial number
        purchase_price: Purchase price in base currency

    Returns:
        Created asset record.
    """
    if not asset_num or not description or not site_id:
        return _error("asset_num, description, and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    body: Dict[str, Any] = {
        "assetnum": asset_num.upper(),
        "description": description[:100],
        "siteid": site_id.upper(),
    }
    if location:
        body["location"] = location
    if asset_type:
        body["assettype"] = asset_type
    if serial_num:
        body["serialnum"] = serial_num
    if purchase_price is not None:
        body["purchaseprice"] = purchase_price

    audit = get_audit_logger()
    try:
        client = await get_connected_client()
        result = await client.post(ASSET_OS, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        # Invalidate asset list cache
        await get_cache().invalidate(f"maximo:assets:{site_id}:*")

        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("create_asset", body, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("supervisor")
async def update_asset(
    asset_num: str,
    site_id: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    asset_type: Optional[str] = None,
    serial_num: Optional[str] = None,
    purchase_price: Optional[float] = None,
    manufacturer: Optional[str] = None,
    vendor: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update specific fields on an existing asset. Only provided fields are changed.

    Args:
        asset_num:    Asset number to update
        site_id:      Site ID of the asset
        description:  New description
        location:     New location code
        asset_type:   New asset type
        serial_num:   New serial number
        purchase_price: Updated purchase price
        manufacturer: Manufacturer name
        vendor:       Vendor/supplier name

    Returns:
        Updated asset record.
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    # Build update body from only provided fields
    body: Dict[str, Any] = {}
    if description is not None:
        body["description"] = description[:100]
    if location is not None:
        body["location"] = location
    if asset_type is not None:
        body["assettype"] = asset_type
    if serial_num is not None:
        body["serialnum"] = serial_num
    if purchase_price is not None:
        body["purchaseprice"] = purchase_price
    if manufacturer is not None:
        body["manufacturer"] = manufacturer
    if vendor is not None:
        body["vendor"] = vendor

    if not body:
        return _error("No fields provided to update", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()
    try:
        client = await get_connected_client()
        # Get the asset href for PATCH
        params = client.build_oslc_query(where=f'assetnum="{oslc_escape(asset_num)}" and siteid="{oslc_escape(site_id)}"')
        existing = await client.get(ASSET_OS, params=params)
        members = existing.get("member", [])
        if not members:
            return _error(f"Asset '{asset_num}' not found in site '{site_id}'", "NOT_FOUND")

        href = members[0].get("href", f"{ASSET_OS}/{members[0].get('_id', asset_num)}")
        result = await client.patch(href, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate(f"maximo:asset:{site_id}:{asset_num}")
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("update_asset", {"asset_num": asset_num, "site_id": site_id, **body}, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("manager")
async def retire_asset(
    asset_num: str,
    site_id: str,
    retirement_date: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retire an asset by changing its status to DECOMMISSIONED.

    Args:
        asset_num:       Asset number to retire
        site_id:         Site ID of the asset
        retirement_date: ISO date string (defaults to today)
        reason:          Reason for retirement

    Returns:
        Updated asset record with DECOMMISSIONED status.
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    retirement_date = retirement_date or datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00")
    body = {
        "status": "DECOMMISSIONED",
        "changedate": retirement_date,
    }
    if reason:
        body["description"] = reason

    start = time.monotonic()
    audit = get_audit_logger()
    try:
        client = await get_connected_client()
        params = client.build_oslc_query(where=f'assetnum="{oslc_escape(asset_num)}" and siteid="{oslc_escape(site_id)}"')
        existing = await client.get(ASSET_OS, params=params)
        members = existing.get("member", [])
        if not members:
            return _error(f"Asset '{asset_num}' not found", "NOT_FOUND")

        href = members[0].get("href", ASSET_OS)
        result = await client.patch(href, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate(f"maximo:asset:{site_id}:{asset_num}")
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("retire_asset", {"asset_num": asset_num, "site_id": site_id, "reason": reason}, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_asset_history(
    asset_num: str,
    site_id: str,
    lookback_days: int = 365,
) -> Dict[str, Any]:
    """
    Retrieve work order and failure history for an asset.

    Args:
        asset_num:     Asset number
        site_id:       Site ID
        lookback_days: How many days back to look (default: 365)

    Returns:
        List of work orders and failure records for the asset.
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        # Single-condition WHERE only — compound clauses cause transport errors on this instance.
        # Fetch by assetnum only, post-filter by siteid and date in Python.
        result = await query_object_structure(
            entity="workorder",
            filters={"asset_num": asset_num},
            select=["wonum", "description", "status", "siteid", "reportdate", "worktype", "actlabhrs", "failurecode"],
            order_by="-reportdate",
            page_size=5,
        )
        if "error" in result:
            return _error(result.get("detail", result["error"]), result["error"])
        members = [
            m for m in result.get("data", [])
            if m.get("siteid", "").upper() == site_id.upper()
            and m.get("reportdate", "") >= cutoff_date
        ]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"asset_num": asset_num, "site_id": site_id, "lookback_days": lookback_days, "work_orders": members},
            duration_ms=duration_ms, record_count=len(members)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_asset_downtime_stats(
    asset_num: str,
    site_id: str,
    period_months: int = 12,
) -> Dict[str, Any]:
    """
    Calculate MTTR and MTBF for an asset based on work order history.

    Args:
        asset_num:      Asset number
        site_id:        Site ID
        period_months:  Analysis period in months (default: 12)

    Returns:
        Dict with mttr_hours, mtbf_hours, total_failures, total_downtime_hours,
        availability_pct, and trend data.
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    lookback_days = period_months * 30
    history = await get_asset_history(asset_num, site_id, lookback_days=lookback_days)

    if not history["success"]:
        return history

    work_orders = history["data"]["work_orders"]
    # Filter corrective/breakdown work orders
    failures = [
        wo for wo in work_orders
        if wo.get("worktype") in ("CM", "EM", "CORRECTIVE", "EMERGENCY")
        and wo.get("actfinish")
    ]

    total_failures = len(failures)
    total_hours = sum(float(wo.get("actlabhrs", 0) or 0) for wo in failures)
    period_hours = lookback_days * 24

    mttr = round(total_hours / total_failures, 2) if total_failures else 0
    mtbf = round((period_hours - total_hours) / total_failures, 2) if total_failures else period_hours
    availability = round(((period_hours - total_hours) / period_hours) * 100, 2) if period_hours else 100

    duration_ms = int((time.monotonic() - start) * 1000)
    return _envelope(
        {
            "asset_num": asset_num,
            "site_id": site_id,
            "period_months": period_months,
            "total_failures": total_failures,
            "total_downtime_hours": round(total_hours, 2),
            "mttr_hours": mttr,
            "mtbf_hours": mtbf,
            "availability_pct": availability,
            "analysis_period_hours": period_hours,
        },
        duration_ms=duration_ms
    )


@require_role("readonly")
async def search_assets(
    keyword: str,
    site_id: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    Search assets by keyword across description and serial number fields.

    Args:
        keyword:   Search term (applied to description and serialnum)
        site_id:   Optional site filter
        filters:   Additional field filters as dict {field: value}
        page_size: Records per page
        page_num:  1-based page number

    Returns:
        Matching asset records.
    """
    if not keyword:
        return _error("keyword is required", "VALIDATION_ERROR")
    page_size = _resolve_page_size(page_size)

    start = time.monotonic()
    # OSLC `like` is not supported on this Maximo build (BMXAA8744E).
    # Strategy: fetch by siteid-only (single condition) then Python keyword match.
    simple_filters: Dict[str, Any] = {}
    if site_id:
        simple_filters["site_id"] = site_id  # alias → siteid

    try:
        # Fetch a larger sample (up to 50) so keyword matching has enough records to search.
        # The user's page_size controls how many results to return after filtering.
        fetch_size = max(page_size * 5, 50)
        result = await query_object_structure(
            entity="asset",
            filters=simple_filters,
            select=["assetnum", "description", "status", "siteid", "location", "assettype", "serialnum"],
            order_by="+assetnum",
            page_size=min(fetch_size, 50),
            page_num=page_num,
            collectioncount=1,
        )
        if "error" in result:
            return _error(result.get("detail", result["error"]), result["error"])
        # Python keyword filter (case-insensitive substring match on description or assetnum)
        kw_lower = keyword.lower()
        members = [
            m for m in result.get("data", [])
            if kw_lower in (m.get("description") or "").lower()
            or kw_lower in (m.get("assetnum") or "").lower()
        ][:page_size]
        # Apply any extra field filters in Python
        if filters:
            for field, value in filters.items():
                try:
                    safe_field_name(field)
                except ValueError:
                    continue
                members = [m for m in members if str(m.get(field, "")).upper() == str(value).upper()]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"assets": members, "totalCount": result.get("totalCount", len(members)), "keyword": keyword},
            duration_ms=duration_ms, record_count=len(members)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


async def _try_candidates(client, candidates, params):
    """Try each OSLC endpoint candidate; return (result, used_endpoint) on first success."""
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
async def get_failure_class_hierarchy(
    parent: Optional[str] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    List Maximo failure classes (problem/cause/remedy taxonomy).
    Pass `parent` to drill into one branch; omit it for top-level classes.

    Args:
        parent:    Failure class code to expand. None returns root nodes.
        page_size: Max records per call (default 200, max 200)
    """
    page_size = max(1, min(int(page_size or 200), 200))
    start = time.monotonic()

    try:
        client = await get_connected_client()
        if parent:
            where = f'parent="{oslc_escape(parent)}"'
        else:
            where = None
        params = client.build_oslc_query(
            where=where,
            select="failurelist,description,parent,type",
            order_by="+failurelist",
            page_size=page_size,
        )
        try:
            result, used = await _try_candidates(client, FAILURELIST_OS_CANDIDATES, params)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                tried = ", ".join(FAILURELIST_OS_CANDIDATES)
                return _error(
                    f"Failure class object structure not published in this Maximo instance (tried: {tried}).",
                    "NOT_FOUND",
                )
            raise

        members: List[Dict] = result.get("member", [])
        if parent is None:
            # parent may be int, string, or missing — coerce before strip().
            members = [m for m in members if not str(m.get("parent") or "").strip()]

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"parent": parent, "endpoint": used, "classes": members},
            duration_ms=duration_ms, record_count=len(members),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_meter_readings(
    asset_num: str,
    site_id: str,
    period_days: int = 90,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Return meter readings recorded against an asset within the look-back
    window. Useful for condition-based maintenance, runtime tracking, and
    trend visualisation.

    Args:
        asset_num:   Asset number to fetch readings for
        site_id:     Site ID (Python post-filter)
        period_days: Look-back window in days (default 90)
        page_size:   Max records per call (default 200, max 200)
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")
    page_size = max(1, min(int(page_size or 200), 200))

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=period_days)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        # No order_by — `readingdate` is not orderable on some Maximo resource definitions
        # (BMXAA9105E). We sort by readingdate in Python after fetch.
        params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="assetnum,siteid,metername,reading,readingdate,readingtype,inspector",
            page_size=page_size,
        )
        try:
            result, used = await _try_candidates(client, ASSETMETER_OS_CANDIDATES, params)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                tried = ", ".join(ASSETMETER_OS_CANDIDATES)
                return _error(
                    f"Asset meter object structure not published in this Maximo instance (tried: {tried}).",
                    "NOT_FOUND",
                )
            raise

        members: List[Dict] = result.get("member", [])
        site_u = site_id.upper()
        readings = [
            r for r in members
            if (r.get("siteid") or "").upper() == site_u
            and (r.get("readingdate") or "") >= cutoff
        ]
        # Sort newest-first in Python (server-side orderBy not supported on this resource).
        readings.sort(key=lambda r: r.get("readingdate") or "", reverse=True)

        by_meter: Dict[str, List[Dict[str, Any]]] = {}
        for r in readings:
            m = r.get("metername") or "(unknown)"
            by_meter.setdefault(m, []).append(r)

        meters_summary = []
        for name, rs in sorted(by_meter.items()):
            try:
                latest_val = float(rs[0].get("reading") or 0)
                oldest_val = float(rs[-1].get("reading") or 0)
                delta = round(latest_val - oldest_val, 3)
            except Exception:
                latest_val = oldest_val = delta = None
            meters_summary.append(
                {
                    "metername": name,
                    "reading_count": len(rs),
                    "latest_reading": latest_val,
                    "latest_reading_date": rs[0].get("readingdate"),
                    "oldest_reading_in_window": oldest_val,
                    "delta_in_window": delta,
                }
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "asset_num": asset_num,
                "site_id": site_id,
                "period_days": period_days,
                "endpoint": used,
                "meters_summary": meters_summary,
                "readings": readings,
            },
            duration_ms=duration_ms, record_count=len(readings),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_asset_criticality_matrix(
    site_id: str,
    top_n: int = 20,
) -> Dict[str, Any]:
    """
    Build an asset criticality matrix from the `priority` field on each
    asset record (1=highest, 5=lowest by Maximo convention). Returns a
    bucket count by priority and a list of the highest-criticality assets.

    Args:
        site_id: Site to analyse
        top_n:   Number of top-priority assets to surface (default 20)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    try:
        client = await get_connected_client()
        # No order_by — on some Maximo builds, sorting by a sparse-NULL field like
        # `priority` causes the column to be stripped from the response entirely.
        # We sort in Python after fetch.
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,siteid,status,priority,assettype,location",
            page_size=200,
        )
        data = await client.get(ASSET_OS, params=params)
        members: List[Dict] = data.get("member", [])

        site_u = site_id.upper()
        in_scope = [a for a in members if (a.get("siteid") or "").upper() == site_u]

        buckets: Dict[str, int] = {}
        for a in in_scope:
            p = a.get("priority")
            key = str(int(p)) if isinstance(p, (int, float)) and p is not None else "UNSET"
            buckets[key] = buckets.get(key, 0) + 1

        def _sort_key(a: Dict) -> float:
            p = a.get("priority")
            if isinstance(p, (int, float)) and p > 0:
                return float(p)
            return 999.0

        top = sorted(in_scope, key=_sort_key)[:top_n]
        top_assets = [
            {
                "assetnum": a.get("assetnum"),
                "description": a.get("description"),
                "priority": a.get("priority"),
                "status": a.get("status"),
                "assettype": a.get("assettype"),
                "location": a.get("location"),
            }
            for a in top
        ]

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "total_assets": len(in_scope),
                "priority_buckets": dict(sorted(buckets.items())),
                "top_priority_assets": top_assets,
            },
            duration_ms=duration_ms, record_count=len(in_scope),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_warranty_status(
    site_id: str,
    asset_num: Optional[str] = None,
    expiring_within_days: int = 90,
) -> Dict[str, Any]:
    """
    Report warranty status for assets at a site. Each asset is bucketed
    into one of: ACTIVE, EXPIRING_SOON, EXPIRED, or UNKNOWN (no warranty
    date populated). Useful for capturing claim-recovery opportunities
    before a covered failure becomes self-funded.

    Args:
        site_id:              Site to analyse
        asset_num:            Optional single-asset filter (Python post-filter)
        expiring_within_days: Window for the EXPIRING_SOON bucket (default 90)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        # Single-condition WHERE on siteid; asset_num post-filtered. Some Maximo
        # builds don't expose `warrantyexpdate` — we still return the asset row
        # with bucket=UNKNOWN so callers can spot data gaps.
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,siteid,status,assettype,installdate,warrantyexpdate,vendor,manufacturer,purchaseprice",
            page_size=200,
        )
        data = await client.get(ASSET_OS, params=params)
        site_u = site_id.upper()
        scope = [a for a in data.get("member", []) if (a.get("siteid") or "").upper() == site_u]
        if asset_num:
            au = asset_num.upper()
            scope = [a for a in scope if (a.get("assetnum") or "").upper() == au]

        now = datetime.now()
        expiring_cutoff = now + timedelta(days=expiring_within_days)
        buckets: Dict[str, int] = {"ACTIVE": 0, "EXPIRING_SOON": 0, "EXPIRED": 0, "UNKNOWN": 0}
        annotated: List[Dict[str, Any]] = []

        def _parse(s: Any) -> Optional[datetime]:
            if not s:
                return None
            try:
                if isinstance(s, str):
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    return datetime.fromisoformat(s).replace(tzinfo=None)
            except ValueError:
                return None
            return None

        for a in scope:
            warranty_dt = _parse(a.get("warrantyexpdate"))
            install_dt = _parse(a.get("installdate"))
            if warranty_dt is None:
                bucket = "UNKNOWN"
                days_until = None
            elif warranty_dt < now:
                bucket = "EXPIRED"
                days_until = (warranty_dt - now).days
            elif warranty_dt <= expiring_cutoff:
                bucket = "EXPIRING_SOON"
                days_until = (warranty_dt - now).days
            else:
                bucket = "ACTIVE"
                days_until = (warranty_dt - now).days
            buckets[bucket] += 1
            annotated.append(
                {
                    "assetnum": a.get("assetnum"),
                    "description": a.get("description"),
                    "status": a.get("status"),
                    "assettype": a.get("assettype"),
                    "vendor": a.get("vendor"),
                    "manufacturer": a.get("manufacturer"),
                    "purchase_price": a.get("purchaseprice"),
                    "install_date": install_dt.strftime("%Y-%m-%d") if install_dt else None,
                    "warranty_expiry": warranty_dt.strftime("%Y-%m-%d") if warranty_dt else None,
                    "days_until_expiry": days_until,
                    "bucket": bucket,
                }
            )

        # Sort: expiring soon first (most urgent), then expired, active, unknown
        bucket_order = {"EXPIRING_SOON": 0, "EXPIRED": 1, "ACTIVE": 2, "UNKNOWN": 3}
        annotated.sort(
            key=lambda x: (
                bucket_order.get(x["bucket"], 9),
                x.get("days_until_expiry") if x.get("days_until_expiry") is not None else 999_999,
            )
        )

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "asset_num_filter": asset_num,
                "expiring_within_days": expiring_within_days,
                "total_assets": len(annotated),
                "buckets": buckets,
                "data_unavailable": (
                    "warrantyexpdate field not populated on any asset — "
                    "check that Maximo MXASSET.WARRANTYEXPDATE is published in the OSLC object structure."
                    if buckets["UNKNOWN"] == len(annotated) and annotated else None
                ),
                "assets": annotated,
            },
            duration_ms=duration_ms, record_count=len(annotated),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
