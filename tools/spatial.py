"""
tools/spatial.py — Wave 9 spatial / GIS tools for IBM Maximo.

Two tools:
    find_assets_near_location  — radius search around a lat/lon for field dispatch
    get_route_for_technician   — ordered WO list optimised by distance (greedy NN)

Both tools assume the Maximo has the **Maximo Spatial add-on** or that
operators have populated coordinate fields on their assets / locations.
When neither is true (the common case on plain Maximo 7.6.x / Manage
without GIS), every tool returns `data_unavailable=True` with a
user-friendly note explaining what's missing and what an admin can do —
never a cryptic 400 / 404 error.

`get_route_for_technician` degrades gracefully: even with no coordinates
it still returns the technician's open WOs in priority order so the
output is useful, just not geographically optimised.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role


ASSET_OS = "/os/mxasset"
LOC_OS = "/os/mxoperloc"
WO_OS = "/os/mxwo"

# Possible coordinate-field naming conventions across Maximo builds.
# We try them in order. The first conv that returns at least one populated
# row wins. None of them are guaranteed to exist on the customer's deployment.
ASSET_COORD_VARIANTS = (
    ("latitudey", "longitudex"),  # IBM Maximo Spatial standard
    ("latitude", "longitude"),    # Some customised installs
    ("ycoord", "xcoord"),         # Older spatial add-on
)


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points in kilometres."""
    R = 6371.0  # Earth radius in km
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _coerce_float(v: Any) -> Optional[float]:
    """Tolerate ints, strings, and Maximo-shape values."""
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


async def _fetch_assets_with_coords(
    client,
    site_id: Optional[str],
    page_size: int = 200,
) -> Tuple[List[Dict[str, Any]], Optional[Tuple[str, str]]]:
    """
    Try each coordinate-field naming convention in order. Return
    (assets_with_coords, (lat_field, lon_field)) for the first variant
    that yields populated coordinate values. Returns ([], None) when no
    variant produces coordinate data — caller surfaces data_unavailable.
    """
    for lat_field, lon_field in ASSET_COORD_VARIANTS:
        select = f"assetnum,description,siteid,location,status,priority,{lat_field},{lon_field}"
        where = f'siteid="{oslc_escape(site_id)}"' if site_id else None
        try:
            params = client.build_oslc_query(
                where=where,
                select=select,
                page_size=page_size,
            )
            data = await client.get(ASSET_OS, params=params)
        except (MaximoAPIError, MaximoAuthError):
            continue

        rows = data.get("member", [])
        with_coords: List[Dict[str, Any]] = []
        for r in rows:
            lat = _coerce_float(r.get(lat_field))
            lon = _coerce_float(r.get(lon_field))
            if lat is None or lon is None:
                continue
            with_coords.append(
                {
                    "assetnum": r.get("assetnum"),
                    "description": r.get("description"),
                    "siteid": r.get("siteid"),
                    "location": r.get("location"),
                    "status": r.get("status"),
                    "priority": r.get("priority"),
                    "latitude": lat,
                    "longitude": lon,
                    "_coord_variant": f"{lat_field}/{lon_field}",
                }
            )
        if with_coords:
            return with_coords, (lat_field, lon_field)

    return [], None


# ══════════════════════════════════════════════════════════════════════════════
# 1. find_assets_near_location
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def find_assets_near_location(
    latitude: float,
    longitude: float,
    radius_km: float = 10.0,
    site_id: Optional[str] = None,
    max_results: int = 50,
) -> Dict[str, Any]:
    """
    Find assets within `radius_km` of a lat/lon — for field dispatch,
    incident response, geofence alerting.

    Coordinate-field detection: the tool tries `latitudey/longitudex`,
    then `latitude/longitude`, then `ycoord/xcoord` and uses the first
    convention that returns populated coordinate values on this Maximo.
    When none yield coordinates the tool returns a friendly
    `data_unavailable=True` response explaining that the customer needs
    Maximo Spatial or populated lat/lon columns on their assets.

    Args:
        latitude:    Centre latitude in decimal degrees
        longitude:   Centre longitude in decimal degrees
        radius_km:   Search radius (default 10 km)
        site_id:     Optional site filter (otherwise scans all sites)
        max_results: Cap returned rows (default 50)
    """
    if not (-90 <= latitude <= 90):
        return _error("latitude must be between -90 and 90", "VALIDATION_ERROR")
    if not (-180 <= longitude <= 180):
        return _error("longitude must be between -180 and 180", "VALIDATION_ERROR")
    if radius_km <= 0:
        return _error("radius_km must be > 0", "VALIDATION_ERROR")

    start = time.monotonic()
    try:
        client = await get_connected_client()
        assets_with_coords, variant = await _fetch_assets_with_coords(client, site_id)

        if not assets_with_coords:
            return _envelope(
                {
                    "centre": {"latitude": latitude, "longitude": longitude},
                    "radius_km": radius_km,
                    "site_id": site_id,
                    "data_unavailable": True,
                    "data_unavailable_note": (
                        "No assets at this site have coordinates populated in any of the "
                        f"common Maximo Spatial column conventions ({', '.join(f'{a}/{b}' for a, b in ASSET_COORD_VARIANTS)}). "
                        "This tool requires either the IBM Maximo Spatial add-on or assets "
                        "with manually-populated latitude/longitude columns. "
                        "Ask your Maximo admin to either enable Spatial or backfill coordinates "
                        "into the asset records."
                    ),
                    "matching_assets": [],
                },
                duration_ms=int((time.monotonic() - start) * 1000),
                record_count=0,
            )

        # Compute distance + filter
        scored = []
        for a in assets_with_coords:
            d = _haversine_km(latitude, longitude, a["latitude"], a["longitude"])
            if d <= radius_km:
                scored.append({**a, "distance_km": round(d, 3)})
        scored.sort(key=lambda x: x["distance_km"])
        scored = scored[:max_results]

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "centre": {"latitude": latitude, "longitude": longitude},
                "radius_km": radius_km,
                "site_id": site_id,
                "coordinate_variant_used": f"{variant[0]}/{variant[1]}" if variant else None,
                "total_candidate_assets": len(assets_with_coords),
                "matching_count": len(scored),
                "matching_assets": scored,
            },
            duration_ms=duration_ms, record_count=len(scored),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 2. get_route_for_technician
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def get_route_for_technician(
    labor_code: str,
    site_id: str,
    max_workorders: int = 20,
) -> Dict[str, Any]:
    """
    Build a daily-route order for a technician's open work orders. When
    the assets have coordinates populated, runs a greedy nearest-neighbour
    tour. When they don't (the common case), falls back to priority +
    target-start ordering and clearly flags `geographic_optimisation:
    false` so the caller / LLM knows the route is logical, not literally
    geographic.

    Args:
        labor_code:     The technician's labor code
        site_id:        Site to constrain the route to
        max_workorders: Cap (default 20)
    """
    if not labor_code or not site_id:
        return _error("labor_code and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    try:
        client = await get_connected_client()

        # Step 1 — fetch open WOs assigned to this technician via wplabor child.
        # Reuse the same OSLC traversal pattern as `get_my_assigned_workorders`.
        wo_params = client.build_oslc_query(
            where=f'wplabor.laborcode="{oslc_escape(labor_code)}"',
            select="wonum,description,siteid,assetnum,location,status,wopriority,worktype,targstartdate,targcompdate",
            order_by="-reportdate",
            page_size=200,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        site_u = site_id.upper()
        terminal = {"COMP", "CLOSE", "CAN"}
        my_wos = [
            w for w in wo_data.get("member", [])
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("status") or "").upper() not in terminal
        ]
        my_wos = my_wos[:max_workorders]

        if not my_wos:
            return _envelope(
                {
                    "labor_code": labor_code,
                    "site_id": site_id,
                    "route_length": 0,
                    "geographic_optimisation": False,
                    "message": (
                        f"No open work orders are currently assigned to '{labor_code}' at site "
                        f"'{site_id}'. Either the technician has no active assignments, or the "
                        "wplabor child collection on mxwo isn't populated. Use "
                        "`get_my_assigned_workorders` directly to confirm."
                    ),
                    "route": [],
                },
                duration_ms=int((time.monotonic() - start) * 1000),
                record_count=0,
            )

        # Step 2 — try to enrich with coordinates from the asset records.
        asset_nums = [
            (w.get("assetnum") or "").strip() for w in my_wos
            if (w.get("assetnum") or "").strip()
        ]
        coord_lookup: Dict[str, Tuple[float, float]] = {}
        coord_variant_used: Optional[str] = None
        if asset_nums:
            assets_with_coords, variant = await _fetch_assets_with_coords(client, site_id)
            if assets_with_coords:
                coord_variant_used = f"{variant[0]}/{variant[1]}" if variant else None
                relevant = {
                    (a.get("assetnum") or "").upper(): (a["latitude"], a["longitude"])
                    for a in assets_with_coords
                }
                for an in asset_nums:
                    if an.upper() in relevant:
                        coord_lookup[an.upper()] = relevant[an.upper()]

        geographic = bool(coord_lookup)

        # Step 3 — order the WOs. Priority + target start when no coords;
        # greedy nearest-neighbour TSP when we have coords.
        if geographic:
            # Greedy NN starting from the highest-priority WO that has coords
            with_coords = [
                w for w in my_wos
                if (w.get("assetnum") or "").upper() in coord_lookup
            ]
            without_coords = [
                w for w in my_wos
                if (w.get("assetnum") or "").upper() not in coord_lookup
            ]
            # Start: highest priority (lowest numeric) with coords
            with_coords.sort(key=lambda w: (
                int(w.get("wopriority") or 99),
                w.get("targstartdate") or "9999",
            ))
            ordered: List[Dict] = []
            remaining = with_coords[:]
            current = remaining.pop(0)
            ordered.append({**current, "leg_distance_km": 0.0, "leg_index": 1})
            while remaining:
                cur_an = (current.get("assetnum") or "").upper()
                cur_lat, cur_lon = coord_lookup[cur_an]

                def _d(w: Dict, cl=cur_lat, cn=cur_lon) -> float:
                    an = (w.get("assetnum") or "").upper()
                    lat, lon = coord_lookup[an]
                    return _haversine_km(cl, cn, lat, lon)

                remaining.sort(key=_d)
                next_wo = remaining.pop(0)
                d = _d(next_wo)
                ordered.append({**next_wo, "leg_distance_km": round(d, 3), "leg_index": len(ordered) + 1})
                current = next_wo
            # Append no-coord WOs at the tail (still useful info, just not routed)
            for w in without_coords:
                ordered.append({**w, "leg_distance_km": None, "leg_index": len(ordered) + 1})

            total_km = round(sum((o.get("leg_distance_km") or 0) for o in ordered), 2)
            return _envelope(
                {
                    "labor_code": labor_code,
                    "site_id": site_id,
                    "route_length": len(ordered),
                    "geographic_optimisation": True,
                    "coordinate_variant_used": coord_variant_used,
                    "with_coordinates": len(with_coords),
                    "without_coordinates": len(without_coords),
                    "total_route_km": total_km,
                    "route": ordered,
                },
                duration_ms=int((time.monotonic() - start) * 1000),
                record_count=len(ordered),
            )

        # No coords — return a logical (not geographic) ordering
        priority_ordered = sorted(
            my_wos,
            key=lambda w: (int(w.get("wopriority") or 99), w.get("targstartdate") or "9999"),
        )
        ordered = [
            {**w, "leg_index": i + 1, "leg_distance_km": None}
            for i, w in enumerate(priority_ordered)
        ]

        return _envelope(
            {
                "labor_code": labor_code,
                "site_id": site_id,
                "route_length": len(ordered),
                "geographic_optimisation": False,
                "data_unavailable_note": (
                    "Geographic route optimisation isn't available — none of the assets "
                    f"linked to {labor_code}'s open work orders have coordinates populated "
                    "(tried column conventions: "
                    f"{', '.join(f'{a}/{b}' for a, b in ASSET_COORD_VARIANTS)}). "
                    "The route returned below is ordered by WO priority + target start date, "
                    "which is still useful as a daily plan. To unlock real distance-based "
                    "ordering, install the Maximo Spatial add-on or manually populate "
                    "latitude/longitude on the assets a technician services."
                ),
                "route": ordered,
            },
            duration_ms=int((time.monotonic() - start) * 1000),
            record_count=len(ordered),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
