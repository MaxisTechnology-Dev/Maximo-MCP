"""
tools/locations.py — Location hierarchy and operational location tools for IBM Maximo.
"""

import time
from typing import Any, Dict, List, Optional

from core.cache import get_cache
from core.generic_oslc import query_object_structure
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role

LOC_OS = "/os/mxoperloc"


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


@require_role("readonly")
async def list_locations(
    site_id: str,
    parent_location: Optional[str] = None,
    location_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    List operational locations for a site with optional hierarchy filtering.

    Args:
        site_id:          Site ID
        parent_location:  List only children of this location (optional)
        location_type:    Filter by type (OPERATING, COURIER, STOREROOM, etc.)

    Returns:
        List of location records with hierarchy information.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    where_parts = [f'siteid="{oslc_escape(site_id)}"']
    if parent_location:
        where_parts.append(f'parent="{oslc_escape(parent_location)}"')
    if location_type:
        where_parts.append(f'type="{oslc_escape(location_type)}"')

    cache_key = f"maximo:locations:{site_id}:{parent_location}:{location_type}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=" and ".join(where_parts),
            select="location,description,siteid,type,parent,status,disabled",
            order_by="+location",
            page_size=5,
        )
        try:
            return await client.get(LOC_OS, params=params)
        except (MaximoAPIError, MaximoAuthError):
            params["oslc.pageSize"] = 1
            return await client.get(LOC_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=1800)
        members = data.get("member", [])
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"locations": members, "totalCount": data.get("totalCount", len(members))},
            cached=cached, duration_ms=duration_ms, record_count=len(members)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_location(location: str, site_id: str) -> Dict[str, Any]:
    """
    Get full details for a specific location including its assets.

    Args:
        location: Location code
        site_id:  Site ID

    Returns:
        Location details with asset list.
    """
    if not location or not site_id:
        return _error("location and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:location:{site_id}:{location}"
    cache = get_cache()

    async def fetch():
        # Single-condition WHERE only — compound clauses cause transport errors on this instance.
        # Fetch by location code only, post-filter by siteid in Python.
        return await query_object_structure(
            entity="location",
            filters={"location": location},
            select=["location", "description", "siteid", "type", "parent", "status", "locpriority", "failurecode"],
            page_size=5,
        )

    try:
        result, cached = await cache.get_or_fetch(cache_key, fetch, ttl=600)
        if "error" in result:
            return _error(result.get("detail", result["error"]), result["error"])
        members = [
            m for m in result.get("data", [])
            if m.get("siteid", "").upper() == site_id.upper()
        ]
        if not members:
            return _error(f"Location '{location}' not found in site '{site_id}'", "NOT_FOUND")
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(members[0], cached=cached, duration_ms=duration_ms)
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_location_hierarchy(site_id: str, root_location: Optional[str] = None) -> Dict[str, Any]:
    """
    Build a hierarchical tree of all locations for a site.

    Args:
        site_id:       Site ID
        root_location: Start from this location as root (optional)

    Returns:
        Nested location tree structure.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        # Single-condition WHERE only (siteid) — compound WHERE causes transport errors.
        # root_location filtering is applied in Python after fetch.
        loc_result = await query_object_structure(
            entity="location",
            filters={"site_id": site_id},
            select=["location", "description", "siteid", "parent", "type", "status"],
            page_size=50,
        )
        if "error" in loc_result:
            return _error(loc_result.get("detail", loc_result["error"]), loc_result["error"])
        all_locations: List[Dict] = loc_result.get("data", [])
        if root_location:
            rl_upper = root_location.upper()
            all_locations = [
                lc for lc in all_locations
                if lc.get("location", "").upper() == rl_upper
                or lc.get("parent", "").upper() == rl_upper
            ]

        # Build tree
        loc_map: Dict[str, Dict] = {}
        for loc in all_locations:
            lc = loc.get("location", "")
            loc_map[lc] = {**loc, "children": []}

        roots = []
        for lc, loc_node in loc_map.items():
            parent = loc_node.get("parent")
            if parent and parent in loc_map:
                loc_map[parent]["children"].append(loc_node)
            else:
                if not root_location or lc == root_location:
                    roots.append(loc_node)

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"site_id": site_id, "location_tree": roots, "total_locations": len(all_locations)},
            duration_ms=duration_ms
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
