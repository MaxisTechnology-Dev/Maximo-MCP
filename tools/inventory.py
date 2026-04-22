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
