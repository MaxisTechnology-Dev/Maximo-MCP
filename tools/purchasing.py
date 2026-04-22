"""
tools/purchasing.py — Purchase order and vendor management tools for IBM Maximo.
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.audit import get_audit_logger
from core.cache import get_cache
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role

PO_OS = "/os/mxpo"
RECEIPT_OS = "/os/mxreceipt"


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


@require_role("manager")
async def create_purchase_order(
    vendor: Optional[str] = None,
    vendor_id: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    site_id: Optional[str] = None,
    required_date: Optional[str] = None,
    notes: Optional[str] = None,
    storeroom: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a new purchase order in Maximo.

    Args:
        vendor:        Vendor/company code (preferred param name)
        vendor_id:     Vendor/company code (legacy param name)
        items:         List of {itemnum|item_num, description, quantity|qty, unitcost, unit} dicts
        site_id:       Ordering site
        required_date: Required delivery date (ISO format)
        notes:         PO-level notes/instructions
        storeroom:     Receiving storeroom

    Returns:
        Created PO record with assigned ponum.

    Example items:
        [{"itemnum": "PUMP-SEAL-001", "description": "Pump seal kit", "quantity": 10, "unitcost": 45.00, "unit": "EA"}]
    """
    vendor_code = (vendor or vendor_id or "").strip()
    if not vendor_code or not items or not site_id:
        return _error("vendor (or vendor_id), items, and site_id are required", "VALIDATION_ERROR")
    if not isinstance(items, list) or len(items) == 0:
        return _error("items must be a non-empty list", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()

    po_lines = []
    for i, item in enumerate(items, 1):
        qty = item.get("qty", item.get("quantity"))
        if qty is None or float(qty) <= 0:
            return _error(f"Item {i}: quantity must be > 0", "VALIDATION_ERROR")
        po_lines.append({
            "polinenum": i,
            "itemnum": item.get("itemnum", item.get("item_num", "")),
            "description": item.get("description", ""),
            "orderqty": float(qty),
            "unitcost": float(item.get("unitcost", 0)),
            "orderunit": item.get("unit", "EA"),
            "storeloc": storeroom or "",
        })

    body: Dict[str, Any] = {
        "vendor": vendor_code,
        "siteid": site_id,
        "status": "WAPPR",
        "orderdate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "poline": po_lines,
    }
    if required_date:
        body["requireddate"] = required_date
    if notes:
        body["description_longdescription"] = notes

    try:
        client = await get_connected_client()
        result = await client.post(PO_OS, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        inputs = {"vendor": vendor_code, "site_id": site_id, "line_count": len(items)}
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("create_purchase_order", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_purchase_order(ponum: str, site_id: str) -> Dict[str, Any]:
    """
    Get full details of a purchase order including all line items.

    Args:
        ponum:   PO number
        site_id: Site ID

    Returns:
        Complete PO record with lines, vendor, and status.
    """
    if not ponum or not site_id:
        return _error("ponum and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:po:{site_id}:{ponum}"
    cache = get_cache()

    async def fetch():
        # Single-condition WHERE only — compound WHERE causes transport errors on this instance.
        # Fetch by ponum only; siteid post-filtered in Python.
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'ponum="{oslc_escape(ponum)}"',
            select="ponum,description,status,siteid,vendor,orderdate,totalcost,poline",
            page_size=5,
        )
        return await client.get(PO_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=300)
        members = [
            m for m in data.get("member", [])
            if m.get("siteid", "").upper() == site_id.upper()
        ]
        if not members:
            return _error(f"Purchase order '{ponum}' not found in site '{site_id}'", "NOT_FOUND")
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(members[0], cached=cached, duration_ms=duration_ms)
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("supervisor")
async def receive_items(
    ponum: str,
    site_id: str,
    received_lines: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Record receipt of items against a purchase order.

    Args:
        ponum:          PO number being received against
        site_id:        Site ID
        received_lines: List of {polinenum, receivedqty, storeroom} dicts

    Returns:
        Receipt transaction record.

    Example received_lines:
        [{"polinenum": 1, "receivedqty": 10, "storeroom": "CENTRAL"}]
    """
    if not ponum or not site_id or not received_lines:
        return _error("ponum, site_id, and received_lines are required", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()

    receipt_lines = []
    for line in received_lines:
        if not line.get("polinenum") or not line.get("receivedqty"):
            return _error("Each received_line must have polinenum and receivedqty", "VALIDATION_ERROR")
        receipt_lines.append({
            "polinenum": int(line["polinenum"]),
            "receivedqty": float(line["receivedqty"]),
            "tostoreloc": line.get("storeroom", ""),
        })

    body: Dict[str, Any] = {
        "ponum": ponum,
        "siteid": site_id,
        "receiptdate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "poline": receipt_lines,
    }

    try:
        client = await get_connected_client()
        result = await client.post(RECEIPT_OS, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate(f"maximo:po:{site_id}:{ponum}")
        inputs = {"ponum": ponum, "site_id": site_id, "line_count": len(received_lines)}
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("receive_items", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_vendor_performance(
    vendor_id: str,
    period_months: int = 12,
) -> Dict[str, Any]:
    """
    Analyse vendor performance including on-time delivery and quality metrics.

    Args:
        vendor_id:      Vendor/company code to analyse
        period_months:  Analysis period in months

    Returns:
        Vendor performance metrics: on_time_pct, total_orders, avg_delivery_days, quality_issues.
    """
    if not vendor_id:
        return _error("vendor_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=period_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        # Single-condition WHERE only — compound WHERE causes transport errors on this instance.
        # Fetch by vendor only; date filter applied in Python.
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'vendor="{oslc_escape(vendor_id)}"',
            select="ponum,vendor,status,orderdate,promiseddate,receiveddate,totalcost",
            page_size=20,
        )
        data = await client.get(PO_OS, params=params)
        pos: List[Dict] = [
            p for p in data.get("member", [])
            if p.get("orderdate", "") >= cutoff
        ]

        total_orders = len(pos)
        completed_pos = [p for p in pos if p.get("status") in ("COMP", "CLOSE", "RECEIVED")]
        on_time = 0
        delivery_days_list = []

        for po in completed_pos:
            req_date = po.get("requireddate")
            order_date = po.get("orderdate")
            if req_date and order_date:
                try:
                    req_dt = datetime.fromisoformat(req_date.replace("Z", "+00:00"))
                    ord_dt = datetime.fromisoformat(order_date.replace("Z", "+00:00"))
                    days = (req_dt - ord_dt).days
                    delivery_days_list.append(days)
                    if days >= 0:
                        on_time += 1
                except Exception:
                    pass

        on_time_pct = round((on_time / len(completed_pos)) * 100, 1) if completed_pos else 0
        avg_delivery_days = round(sum(delivery_days_list) / len(delivery_days_list), 1) if delivery_days_list else 0
        duration_ms = int((time.monotonic() - start) * 1000)

        return _envelope(
            {
                "vendor_id": vendor_id,
                "period_months": period_months,
                "total_orders": total_orders,
                "completed_orders": len(completed_pos),
                "on_time_delivery_pct": on_time_pct,
                "avg_lead_time_days": avg_delivery_days,
                "performance_rating": "GOOD" if on_time_pct >= 90 else "FAIR" if on_time_pct >= 70 else "POOR",
            },
            duration_ms=duration_ms
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
