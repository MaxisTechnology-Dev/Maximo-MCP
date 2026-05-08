"""
tools/purchasing.py — Purchase order and vendor management tools for IBM Maximo.
"""

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.audit import get_audit_logger
from core.cache import get_cache
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role

PO_OS = "/os/mxpo"
RECEIPT_OS = "/os/mxreceipt"
VENDOR_OS = "/os/mxvendor"
PR_OS = "/os/mxpr"


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
async def list_purchase_orders(
    site_id: Optional[str] = None,
    status: Optional[str] = None,
    vendor: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List purchase orders with optional filters. Pagination is applied in Python
    after a single-condition OSLC fetch (compound WHERE is unreliable on some
    Maximo instances).

    Args:
        site_id:   Filter by ordering site
        status:    PO status (WAPPR, APPR, INPRG, CLOSE, ...)
        vendor:    Vendor/company code
        date_from: ISO date — orders placed on or after
        date_to:   ISO date — orders placed on or before
        page_size: Records per page (default 50, max 200)
        page_num:  1-based page number
    """
    start = time.monotonic()
    page_size = max(1, min(int(page_size or 50), 200))

    # Pick exactly one server-side filter; keep the rest for Python post-filter.
    if vendor:
        where = f'vendor="{oslc_escape(vendor)}"'
    elif site_id:
        where = f'siteid="{oslc_escape(site_id)}"'
    elif status:
        where = f'status="{oslc_escape(status)}"'
    else:
        where = None

    cache_key = f"maximo:po_list:{site_id}:{status}:{vendor}:{date_from}:{date_to}:{page_size}:{page_num}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=where,
            select="ponum,description,status,siteid,vendor,orderdate,requireddate,totalcost",
            order_by="-orderdate",
            page_size=200,
            collectioncount=1,
        )
        return await client.get(PO_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        members: List[Dict] = data.get("member", [])

        def _matches(po: Dict) -> bool:
            if site_id and (po.get("siteid") or "").upper() != site_id.upper():
                return False
            if status and (po.get("status") or "").upper() != status.upper():
                return False
            if vendor and (po.get("vendor") or "").upper() != vendor.upper():
                return False
            od = po.get("orderdate") or ""
            if date_from and od < date_from:
                return False
            if date_to and od > date_to:
                return False
            return True

        filtered = [po for po in members if _matches(po)]
        total = len(filtered)
        start_idx = (page_num - 1) * page_size
        page_rows = filtered[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"purchase_orders": page_rows, "totalCount": total},
            cached=cached, duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_vendors(
    name_filter: Optional[str] = None,
    active_only: bool = True,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List vendor (company) records. Used to discover vendor codes that
    `get_purchase_order` and `get_vendor_performance` need as inputs.

    Args:
        name_filter: Case-insensitive substring match on vendor name
        active_only: When True, omit disabled/inactive vendor records
        page_size:   Records per page (default 50, max 200)
        page_num:    1-based page number
    """
    start = time.monotonic()
    page_size = max(1, min(int(page_size or 50), 200))

    cache_key = f"maximo:vendor_list:{name_filter}:{active_only}:{page_size}:{page_num}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            select="company,name,disabled,type,city,country,homepage,phone",
            order_by="+company",
            page_size=200,
            collectioncount=1,
        )
        return await client.get(VENDOR_OS, params=params)

    try:
        try:
            data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=300)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                return _error(
                    "Vendor object structure not published in this Maximo instance (404 /os/mxvendor).",
                    "NOT_FOUND",
                )
            raise

        members: List[Dict] = data.get("member", [])
        nf = (name_filter or "").lower()

        def _matches(v: Dict) -> bool:
            if active_only and bool(v.get("disabled", False)):
                return False
            if nf and nf not in (v.get("name") or "").lower() and nf not in (v.get("company") or "").lower():
                return False
            return True

        filtered = [v for v in members if _matches(v)]
        total = len(filtered)
        start_idx = (page_num - 1) * page_size
        page_rows = filtered[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"vendors": page_rows, "totalCount": total},
            cached=cached, duration_ms=duration_ms, record_count=len(page_rows),
        )
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


@require_role("readonly")
async def list_purchase_requisitions(
    site_id: Optional[str] = None,
    status: Optional[str] = None,
    vendor: Optional[str] = None,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List purchase requisitions (PRs are upstream of POs in the procurement
    workflow). Single-condition WHERE; remaining filters in Python.

    Args:
        site_id:   Filter by site
        status:    PR status code (WAPPR, APPR, CAN, CLOSE, ...)
        vendor:    Vendor / company code
        page_size: Records per page (default 50, max 200)
        page_num:  1-based page number
    """
    start = time.monotonic()
    page_size = max(1, min(int(page_size or 50), 200))

    if status:
        where = f'status="{oslc_escape(status)}"'
    elif vendor:
        where = f'vendor="{oslc_escape(vendor)}"'
    elif site_id:
        where = f'siteid="{oslc_escape(site_id)}"'
    else:
        where = None

    cache_key = f"maximo:pr_list:{site_id}:{status}:{vendor}:{page_size}:{page_num}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=where,
            select="prnum,description,siteid,status,vendor,prdate,requireddate,totalcost",
            page_size=200,
            collectioncount=1,
        )
        return await client.get(PR_OS, params=params)

    try:
        try:
            data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=60)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                return _error(
                    "Purchase Requisition object structure not published in this Maximo instance (404 /os/mxpr).",
                    "NOT_FOUND",
                )
            raise

        members: List[Dict] = data.get("member", [])

        def _matches(pr: Dict) -> bool:
            if site_id and (pr.get("siteid") or "").upper() != site_id.upper():
                return False
            if status and (pr.get("status") or "").upper() != status.upper():
                return False
            if vendor and (pr.get("vendor") or "").upper() != vendor.upper():
                return False
            return True

        filtered = [pr for pr in members if _matches(pr)]
        total = len(filtered)
        start_idx = (page_num - 1) * page_size
        page_rows = filtered[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"purchase_requisitions": page_rows, "totalCount": total},
            cached=cached, duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_spend_analysis(
    site_id: str,
    period_months: int = 12,
    group_by: str = "vendor",
    top_n: int = 10,
) -> Dict[str, Any]:
    """
    Aggregate purchasing spend over a look-back window. Group by vendor,
    status, or worktype to spot concentration risk and category drift.

    Args:
        site_id:       Site to analyse
        period_months: Look-back window in months (default 12)
        group_by:      "vendor" (default) | "status" | "worktype"
        top_n:         Number of top groups to return (default 10)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")
    if group_by not in {"vendor", "status", "worktype"}:
        return _error("group_by must be one of: vendor, status, worktype", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=period_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="ponum,siteid,vendor,status,orderdate,totalcost,worktype",
            page_size=200,
        )
        data = await client.get(PO_OS, params=params)
        rows: List[Dict] = data.get("member", [])

        site_u = site_id.upper()
        in_scope = [
            p for p in rows
            if (p.get("siteid") or "").upper() == site_u
            and (p.get("orderdate") or "") >= cutoff
        ]

        sums: Dict[str, float] = {}
        counts: Dict[str, int] = {}
        total_spend = 0.0
        for p in in_scope:
            key = (p.get(group_by) or "(unknown)").strip() or "(blank)"
            cost = float(p.get("totalcost") or 0)
            sums[key] = sums.get(key, 0.0) + cost
            counts[key] = counts.get(key, 0) + 1
            total_spend += cost

        ranked = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        top_groups = [
            {
                "key": key,
                "po_count": counts.get(key, 0),
                "total_spend": round(amount, 2),
                "share_pct": round((amount / total_spend) * 100, 1) if total_spend else 0,
            }
            for key, amount in ranked
        ]

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "period_months": period_months,
                "group_by": group_by,
                "total_purchase_orders": len(in_scope),
                "total_spend": round(total_spend, 2),
                "top_groups": top_groups,
            },
            duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
