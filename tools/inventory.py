"""
tools/inventory.py — Inventory, storeroom, and material management tools for IBM Maximo.
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.audit import get_audit_logger
from core.cache import get_cache
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role

INV_OS = "/os/mxinventory"
ITEM_OS = "/os/mxitem"
TRANSFER_OS = "/os/mxinvtrans"
MATREC_OS = "/os/mxmatrectrans"
LOC_OS = "/os/mxoperloc"


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


@require_role("readonly")
async def check_stock_level(
    item_num: str,
    storeroom: str,
    site_id: str,
) -> Dict[str, Any]:
    """
    Check current stock level, reorder point, and economic order quantity for an item.

    Args:
        item_num:  Item number to check
        storeroom: Storeroom code
        site_id:   Site ID

    Returns:
        Stock details including curbal, minlevel, reorderpoint, and stdcost.
    """
    if not all([item_num, storeroom, site_id]):
        return _error("item_num, storeroom, and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:stock:{site_id}:{storeroom}:{item_num}"
    cache = get_cache()

    async def fetch():
        # Single-condition WHERE only — compound WHERE causes transport errors on this instance.
        # Fetch by itemnum only; storeroom and siteid post-filtered in Python.
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'itemnum="{oslc_escape(item_num)}"',
            select="itemnum,location,siteid,storeloc,curbal,minlevel,reorderpoint,unitcost,binnum",
            page_size=10,
        )
        return await client.get(INV_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=120)
        members = [
            m for m in data.get("member", [])
            if (m.get("storeloc") or m.get("location", "")).upper() == storeroom.upper()
            and m.get("siteid", "").upper() == site_id.upper()
        ]
        if not members:
            return _error(f"Item '{item_num}' not found in storeroom '{storeroom}'", "NOT_FOUND")
        item = members[0]
        cur_bal = float(item.get("curbal", 0) or 0)
        reorder_pt = float(item.get("reorderpoint", 0) or 0)
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {**item, "below_reorder_point": cur_bal <= reorder_pt},
            cached=cached, duration_ms=duration_ms
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_low_stock_items(
    site_id: str,
    storeroom: Optional[str] = None,
) -> Dict[str, Any]:
    """
    List all inventory items that are at or below their reorder point.

    Args:
        site_id:   Site ID to check
        storeroom: Optional storeroom filter

    Returns:
        List of items requiring reorder with current vs. minimum levels.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    where_parts = [f'siteid="{oslc_escape(site_id)}"']
    if storeroom:
        where_parts.append(f'storeloc="{oslc_escape(storeroom)}"')

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=" and ".join(where_parts),
            select="itemnum,description,storeloc,siteid,curbal,minlevel,reorderpoint,orderqty,stdcost",
            page_size=500,
        )
        data = await client.get(INV_OS, params=params)
        all_items: List[Dict] = data.get("member", [])

        low_stock = []
        for item in all_items:
            cur_bal = float(item.get("curbal", 0) or 0)
            reorder_pt = float(item.get("reorderpoint", 0) or 0)
            if cur_bal <= reorder_pt:
                low_stock.append({
                    **item,
                    "shortage": round(reorder_pt - cur_bal, 2),
                    "suggested_order_qty": float(item.get("orderqty", 0) or 0),
                })

        low_stock.sort(key=lambda x: x.get("shortage", 0), reverse=True)
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"low_stock_items": low_stock, "site_id": site_id},
            duration_ms=duration_ms, record_count=len(low_stock)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("technician")
async def create_material_request(
    # New "simple" signature (preferred)
    item_num: Optional[str] = None,
    quantity: Optional[float] = None,
    location: Optional[str] = None,
    site_id: Optional[str] = None,
    notes: Optional[str] = None,
    # Backward compatible "batch" signature
    items: Optional[List[Dict[str, Any]]] = None,
    destination_storeroom: Optional[str] = None,
    needed_by_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a material receipt transaction (MATRECTRANS) for requested items.

    Args:
        item_num:              Single item number (simple mode)
        quantity:              Quantity for item_num (simple mode)
        location:              Destination storeroom/location (simple mode)
        site_id:               Site ID
        notes:                 Optional notes/long description
        items:                 (legacy) List of {itemnum, quantity, from_storeroom} dicts
        destination_storeroom: (legacy) Target storeroom for materials
        needed_by_date:        ISO date when materials are needed

    Returns:
        Created material request record.

    Example items:
        [{"itemnum": "BOLT-M10", "quantity": 50, "from_storeroom": "CENTRAL"}]
    """
    # Normalize inputs
    if item_num and items:
        return _error("Provide either item_num or items, not both", "VALIDATION_ERROR")

    if item_num:
        if site_id is None:
            return _error("site_id is required", "VALIDATION_ERROR")
        if quantity is None or float(quantity) <= 0:
            return _error("quantity must be > 0", "VALIDATION_ERROR")
        if not location:
            return _error("location is required (destination storeroom)", "VALIDATION_ERROR")
        destination_storeroom = location
        items = [{"itemnum": item_num, "quantity": float(quantity)}]

    if not items or not destination_storeroom or not site_id:
        return _error("items (or item_num), destination_storeroom/location, and site_id are required", "VALIDATION_ERROR")
    if not isinstance(items, list) or len(items) == 0:
        return _error("items must be a non-empty list", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()
    body: Dict[str, Any] = {
        "siteid": site_id,
        "tostoreloc": destination_storeroom,
        "transdate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "matrectrans": [
            {
                "itemnum": item["itemnum"],
                "quantity": float(item["quantity"]),
                "fromstoreloc": item.get("from_storeroom", destination_storeroom),
            }
            for item in items
        ],
    }
    if needed_by_date:
        body["requiredate"] = needed_by_date
    if notes:
        body["description_longdescription"] = notes

    try:
        client = await get_connected_client()
        result = await client.post(MATREC_OS, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        inputs = {"destination_storeroom": destination_storeroom, "site_id": site_id, "item_count": len(items)}
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("create_material_request", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("supervisor")
async def transfer_inventory(
    item_num: str,
    from_storeroom: str,
    to_storeroom: str,
    quantity: float,
    site_id: str,
) -> Dict[str, Any]:
    """
    Transfer inventory between storerooms.

    Args:
        item_num:       Item to transfer
        from_storeroom: Source storeroom
        to_storeroom:   Destination storeroom
        quantity:       Quantity to transfer
        site_id:        Site ID

    Returns:
        Transfer transaction record.
    """
    if not all([item_num, from_storeroom, to_storeroom, site_id]):
        return _error("item_num, from_storeroom, to_storeroom, and site_id are required", "VALIDATION_ERROR")
    if quantity <= 0:
        return _error("quantity must be > 0", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()
    body = {
        "itemnum": item_num,
        "fromstoreloc": from_storeroom,
        "tostoreloc": to_storeroom,
        "quantity": quantity,
        "siteid": site_id,
        "transdate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }

    try:
        client = await get_connected_client()
        result = await client.post(TRANSFER_OS, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        inputs = {"item_num": item_num, "from_storeroom": from_storeroom, "to_storeroom": to_storeroom, "quantity": quantity}
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("transfer_inventory", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_items(
    keyword: Optional[str] = None,
    item_type: Optional[str] = None,
    commodity_group: Optional[str] = None,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List item-master records (catalog of stocked + non-stocked items).

    Single-condition WHERE only — additional filters are applied in Python
    after fetch to remain compatible with Maximo instances that reject
    compound OSLC clauses.

    Args:
        keyword:         Case-insensitive substring match against itemnum or description
        item_type:       Item type code (ITEM, SERVICE, TOOL, ...)
        commodity_group: Commodity group code
        page_size:       Records per page (default 50, max 200)
        page_num:        1-based page number
    """
    start = time.monotonic()
    page_size = max(1, min(int(page_size or 50), 200))

    # Pick exactly one server-side filter; remainder go to Python post-filter.
    if item_type:
        where = f'itemtype="{oslc_escape(item_type)}"'
    elif commodity_group:
        where = f'commoditygroup="{oslc_escape(commodity_group)}"'
    else:
        where = None

    cache_key = f"maximo:items:{keyword}:{item_type}:{commodity_group}:{page_size}:{page_num}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=where,
            select="itemnum,description,itemtype,commodity,commoditygroup,issueunit,orderunit,status,inspectionrequired,lottype",
            order_by="+itemnum",
            page_size=200,
            collectioncount=1,
        )
        return await client.get(ITEM_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=300)
        members: List[Dict] = data.get("member", [])
        kw = (keyword or "").lower()

        def _matches(it: Dict) -> bool:
            if kw and kw not in (it.get("itemnum") or "").lower() and kw not in (it.get("description") or "").lower():
                return False
            if item_type and (it.get("itemtype") or "").upper() != item_type.upper():
                return False
            if commodity_group and (it.get("commoditygroup") or "").upper() != commodity_group.upper():
                return False
            return True

        filtered = [it for it in members if _matches(it)]
        total = len(filtered)
        start_idx = (page_num - 1) * page_size
        page_rows = filtered[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"items": page_rows, "totalCount": total},
            cached=cached, duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_item(item_num: str) -> Dict[str, Any]:
    """
    Get full item-master details for a specific item number.

    Args:
        item_num: Item number to fetch
    """
    if not item_num:
        return _error("item_num is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:item:{item_num}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'itemnum="{oslc_escape(item_num)}"',
            select="itemnum,description,itemtype,commodity,commoditygroup,issueunit,orderunit,status,inspectionrequired,lottype,manufacturer,modelnum,description_longdescription",
            page_size=5,
        )
        return await client.get(ITEM_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=300)
        members: List[Dict] = data.get("member", [])
        if not members:
            return _error(f"Item '{item_num}' not found", "NOT_FOUND")
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(members[0], cached=cached, duration_ms=duration_ms)
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_storerooms(
    site_id: str,
    active_only: bool = True,
) -> Dict[str, Any]:
    """
    List storeroom locations for a site. Storerooms are operating locations
    with type=STOREROOM. The result is the input vocabulary for
    `check_stock_level`, `create_material_request`, and `transfer_inventory`.

    Args:
        site_id:     Site to list storerooms for
        active_only: When True, omit non-OPERATING locations
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:storerooms:{site_id}:{active_only}"
    cache = get_cache()

    async def fetch():
        # Single-condition WHERE only — fetch by type, post-filter siteid in Python.
        client = await get_connected_client()
        params = client.build_oslc_query(
            where='type="STOREROOM"',
            select="location,description,siteid,status,type,parent",
            order_by="+location",
            page_size=200,
            collectioncount=1,
        )
        return await client.get(LOC_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=600)
        members: List[Dict] = data.get("member", [])
        site_u = site_id.upper()
        rooms = []
        for r in members:
            if (r.get("siteid") or "").upper() != site_u:
                continue
            if active_only and (r.get("status") or "").upper() not in ("", "OPERATING", "ACTIVE"):
                continue
            rooms.append(r)
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"site_id": site_id, "storerooms": rooms},
            cached=cached, duration_ms=duration_ms, record_count=len(rooms),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_reorder_recommendations(site_id: str) -> Dict[str, Any]:
    """
    Get inventory reorder recommendations based on current stock levels and usage.

    Args:
        site_id: Site to analyse

    Returns:
        Prioritised list of items recommended for reorder with suggested quantities.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    # Delegate to list_low_stock_items and enrich with cost estimates
    low_stock_result = await list_low_stock_items(site_id)
    if not low_stock_result["success"]:
        return low_stock_result

    items = low_stock_result["data"]["low_stock_items"]
    recommendations = []
    for item in items:
        suggested_qty = float(item.get("suggested_order_qty", 0) or 0)
        std_cost = float(item.get("stdcost", 0) or 0)
        recommendations.append({
            "itemnum": item.get("itemnum"),
            "description": item.get("description"),
            "storeroom": item.get("storeloc"),
            "current_stock": item.get("curbal"),
            "reorder_point": item.get("reorderpoint"),
            "shortage": item.get("shortage"),
            "recommended_order_qty": suggested_qty,
            "estimated_cost": round(suggested_qty * std_cost, 2),
            "urgency": "HIGH" if float(item.get("curbal", 0) or 0) == 0 else "MEDIUM",
        })

    start = time.monotonic()
    duration_ms = int((time.monotonic() - start) * 1000)
    return _envelope(
        {"site_id": site_id, "recommendations": recommendations, "total_items": len(recommendations)},
        duration_ms=duration_ms, record_count=len(recommendations)
    )


def _avg_cost_from_invcost(invcost_lines: List[Dict[str, Any]]) -> float:
    """
    Pull a representative unit cost out of the embedded `invcost` child collection.
    Maximo stores cost in MXINVCOST rows with fields like `avgcost`, `lastcost`, `stdcost`.
    Prefer avgcost, fall back to lastcost, then stdcost.
    """
    for cost_field in ("avgcost", "lastcost", "stdcost"):
        for row in invcost_lines:
            v = row.get(cost_field)
            if v not in (None, "", 0):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
    return 0.0


@require_role("readonly")
async def get_inventory_valuation(
    site_id: str,
    storeroom: Optional[str] = None,
    top_n: int = 20,
) -> Dict[str, Any]:
    """
    Compute total inventory valuation (sum of curbal × avgcost) for a site
    or a specific storeroom. Returns an overall total plus the top-N items
    by line value — useful for finance reconciliation and dead-stock review.

    Args:
        site_id:   Site to value
        storeroom: Optional storeroom filter (Python post-filter)
        top_n:     Number of top-valued items to return (default 20)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        # Single-condition WHERE on siteid; storeroom filtered in Python.
        # `invcost` is requested as an embedded child collection — it returns
        # inline as a list of {avgcost, lastcost, stdcost} rows on this build.
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="itemnum,siteid,storeloc,curbal,abctype,invcost",
            page_size=200,
        )
        data = await client.get(INV_OS, params=params)
        rows: List[Dict] = data.get("member", [])

        site_u = site_id.upper()
        sr_u = storeroom.upper() if storeroom else None
        in_scope: List[Dict] = []
        for r in rows:
            if (r.get("siteid") or "").upper() != site_u:
                continue
            if sr_u and (r.get("storeloc") or "").upper() != sr_u:
                continue
            in_scope.append(r)

        valued: List[Dict[str, Any]] = []
        total_value = 0.0
        for r in in_scope:
            qty = float(r.get("curbal") or 0)
            unit_cost = _avg_cost_from_invcost(r.get("invcost") or [])
            line_value = qty * unit_cost
            total_value += line_value
            valued.append(
                {
                    "itemnum": r.get("itemnum"),
                    "storeroom": r.get("storeloc"),
                    "curbal": qty,
                    "unit_cost": round(unit_cost, 4),
                    "line_value": round(line_value, 2),
                    "abctype": r.get("abctype"),
                }
            )

        valued.sort(key=lambda x: x["line_value"], reverse=True)
        items_with_cost = sum(1 for v in valued if v["unit_cost"] > 0)

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "storeroom": storeroom,
                "total_items": len(in_scope),
                "items_with_known_cost": items_with_cost,
                "total_valuation": round(total_value, 2),
                "top_items": valued[:top_n],
            },
            duration_ms=duration_ms, record_count=len(in_scope),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_critical_spares_check(
    site_id: str,
    priority_threshold: int = 2,
) -> Dict[str, Any]:
    """
    For each critical asset (priority ≤ priority_threshold), report the
    spare parts on its BOM and whether each is below its inventory reorder
    point. Surfaces stock-out risk for the assets that matter most.

    Note: Maximo exposes asset spare parts via the `sparepart` child
    collection on `mxasset`. On builds where that collection is not
    populated through OSLC, this tool returns the critical-asset list
    annotated with a `data_unavailable` flag so the caller can take
    operational action (e.g. ask their Maximo admin to enable it).

    Args:
        site_id:            Site to analyse
        priority_threshold: Asset priority cutoff (1=highest). Default 2.
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        # Step 1 — fetch critical assets at this site (priority ≤ threshold).
        asset_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,siteid,priority,status,sparepart",
            page_size=200,
        )
        a_data = await client.get("/os/mxasset", params=asset_params)
        site_u = site_id.upper()
        critical_assets = [
            a for a in a_data.get("member", [])
            if (a.get("siteid") or "").upper() == site_u
            and isinstance(a.get("priority"), (int, float))
            and a.get("priority") is not None
            and float(a["priority"]) <= priority_threshold
        ]

        # Step 2 — fetch all inventory at this site to look up reorder status.
        inv_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="itemnum,siteid,storeloc,curbal,reorderpoint,minlevel",
            page_size=200,
        )
        inv_data = await client.get(INV_OS, params=inv_params)
        inv_index: Dict[str, Dict[str, Any]] = {}
        for r in inv_data.get("member", []):
            if (r.get("siteid") or "").upper() != site_u:
                continue
            key = (r.get("itemnum") or "").upper()
            if key:
                # Keep best (lowest) curbal record per item — most-stocked-out wins.
                prev = inv_index.get(key)
                if prev is None or float(r.get("curbal") or 0) < float(prev.get("curbal") or 0):
                    inv_index[key] = r

        annotated = []
        spare_data_seen = False
        for a in critical_assets:
            spares = a.get("sparepart") or []
            if spares:
                spare_data_seen = True
            spare_status = []
            stockout_count = 0
            for sp in spares:
                item_num = (sp.get("itemnum") or "").upper()
                inv = inv_index.get(item_num)
                cur = float(inv.get("curbal") or 0) if inv else 0
                rop = float(inv.get("reorderpoint") or 0) if inv else 0
                below_rop = bool(inv and cur <= rop)
                if below_rop:
                    stockout_count += 1
                spare_status.append(
                    {
                        "itemnum": sp.get("itemnum"),
                        "qty_required": sp.get("quantity") or sp.get("qty"),
                        "in_stock": cur,
                        "reorder_point": rop,
                        "below_reorder_point": below_rop,
                        "in_inventory_master": inv is not None,
                    }
                )
            annotated.append(
                {
                    "assetnum": a.get("assetnum"),
                    "description": a.get("description"),
                    "priority": a.get("priority"),
                    "status": a.get("status"),
                    "spare_count": len(spares),
                    "spares_below_reorder_point": stockout_count,
                    "spares": spare_status,
                }
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "priority_threshold": priority_threshold,
                "critical_asset_count": len(critical_assets),
                "data_unavailable": (len(critical_assets) > 0 and not spare_data_seen),
                "data_unavailable_note": (
                    "Critical assets found but no asset.sparepart child rows returned by OSLC. "
                    "Ask your Maximo admin to enable sparepart on the mxasset object structure."
                ) if (len(critical_assets) > 0 and not spare_data_seen) else None,
                "critical_assets": annotated,
            },
            duration_ms=duration_ms, record_count=len(critical_assets),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
