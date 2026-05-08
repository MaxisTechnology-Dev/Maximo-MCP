"""
tests/integration/test_smoke_wave1.py — Live smoke test for the 10 Wave-1 tools.

Calls each Wave-1 tool against the configured Maximo instance with reasonable
default inputs and asserts that every one returns success. Discovery calls
run first to find a real site_id / wonum to feed the dependent tools.

Run as a pytest module (preferred):
    pytest tests/integration/test_smoke_wave1.py -m integration -v

Or directly as a script for ad-hoc debugging:
    python tests/integration/test_smoke_wave1.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

# Make sure the project root is importable when run directly as a script.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _truncate(value: Any, max_len: int = 240) -> str:
    s = json.dumps(value, default=str) if not isinstance(value, str) else value
    return s if len(s) <= max_len else s[:max_len] + "…"


def _result_line(name: str, ok: bool, note: str, duration_ms: int) -> str:
    flag = "PASS" if ok else "FAIL"
    return f"[{flag}] {name:<32} {duration_ms:>5}ms  {note}"


async def _call(name: str, coro) -> Tuple[bool, Dict[str, Any], int]:
    start = time.monotonic()
    try:
        result = await coro
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        return False, {"error": f"{type(exc).__name__}: {exc!r}"}, elapsed
    elapsed = int((time.monotonic() - start) * 1000)
    return bool(result.get("success")), result, elapsed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_smoke_wave1() -> None:
    """Live smoke test — fails if any Wave-1 tool returns success=False."""
    failures = await _run_smoke()
    assert not failures, f"{len(failures)} Wave-1 tool(s) failed: {[name for name, _ in failures]}"


async def _run_smoke() -> List[Tuple[str, Dict[str, Any]]]:
    # Import after env is set so settings pick up creds.
    from tools import inventory, purchasing, workorders
    from core.maximo_client import get_connected_client

    print("=" * 78)
    print("  Wave-1 smoke test against:", os.environ.get("MAXIMO_URL"))
    print("=" * 78)

    # 0) Basic connectivity probe via the maximo client
    try:
        client = await get_connected_client()
        whoami = await client.get("/whoami", params={"lean": "1"})
        user = whoami.get("userName") or whoami.get("personid", "?")
        print(f"  Connected as user: {user!r}")
    except Exception as exc:
        print(f"  CONNECTIVITY FAILED: {type(exc).__name__}: {exc!r}")
        return [("connectivity", {"error": f"{type(exc).__name__}: {exc!r}"})]

    # Discover a site_id by listing existing work orders. Default fallback BEDFORD.
    site_id = "BEDFORD"
    sample_wonum: Optional[str] = None
    sample_item: Optional[str] = None
    sample_storeroom: Optional[str] = None
    sample_sr: Optional[str] = None

    try:
        wo_list = await workorders.list_workorders(page_size=5)
        if wo_list.get("success"):
            wos = wo_list["data"].get("workorders", [])
            if wos:
                site_id = wos[0].get("siteid", site_id) or site_id
                sample_wonum = wos[0].get("wonum")
                print(f"  Discovered site_id={site_id!r}, sample wonum={sample_wonum!r}")
    except Exception as exc:
        print(f"  Discovery via list_workorders failed: {exc!r}")

    print("-" * 78)
    results: List[str] = []
    failures: List[Tuple[str, Dict[str, Any]]] = []

    def _record(name: str, ok: bool, payload: Dict[str, Any], elapsed: int, note: str) -> None:
        results.append(_result_line(name, ok, note, elapsed))
        if not ok:
            failures.append((name, payload))

    # 1) list_purchase_orders ---------------------------------------------------
    ok, payload, ms = await _call(
        "list_purchase_orders",
        purchasing.list_purchase_orders(site_id=site_id, page_size=5),
    )
    if ok:
        pos = payload["data"].get("purchase_orders", [])
        note = f"records={len(pos)}, total={payload['data'].get('totalCount', 0)}"
    else:
        note = _truncate(payload.get("error") or payload)
    _record("list_purchase_orders", ok, payload, ms, note)

    # 2) list_vendors -----------------------------------------------------------
    ok, payload, ms = await _call(
        "list_vendors",
        purchasing.list_vendors(page_size=5),
    )
    if ok:
        vendors = payload["data"].get("vendors", [])
        note = f"records={len(vendors)}, total={payload['data'].get('totalCount', 0)}"
    else:
        note = _truncate(payload.get("error") or payload)
    _record("list_vendors", ok, payload, ms, note)

    # 3) list_items -------------------------------------------------------------
    ok, payload, ms = await _call(
        "list_items",
        inventory.list_items(page_size=5),
    )
    if ok:
        items = payload["data"].get("items", [])
        if items:
            sample_item = items[0].get("itemnum")
        note = f"records={len(items)}, total={payload['data'].get('totalCount', 0)}, sample_item={sample_item!r}"
    else:
        note = _truncate(payload.get("error") or payload)
    _record("list_items", ok, payload, ms, note)

    # 4) get_item ---------------------------------------------------------------
    if sample_item:
        ok, payload, ms = await _call(
            "get_item",
            inventory.get_item(sample_item),
        )
        note = (
            f"item={sample_item} description={_truncate(payload['data'].get('description'), 60)!r}"
            if ok else _truncate(payload.get("error") or payload)
        )
    else:
        ok, payload, ms = False, {"error": "no sample_item discovered"}, 0
        note = "skipped — list_items returned no rows"
    _record("get_item", ok, payload, ms, note)

    # 5) list_storerooms --------------------------------------------------------
    ok, payload, ms = await _call(
        "list_storerooms",
        inventory.list_storerooms(site_id=site_id),
    )
    if ok:
        rooms = payload["data"].get("storerooms", [])
        if rooms:
            sample_storeroom = rooms[0].get("location")
        note = f"records={len(rooms)}, sample={sample_storeroom!r}"
    else:
        note = _truncate(payload.get("error") or payload)
    _record("list_storerooms", ok, payload, ms, note)

    # 6) list_service_requests --------------------------------------------------
    ok, payload, ms = await _call(
        "list_service_requests",
        workorders.list_service_requests(site_id=site_id, page_size=5),
    )
    if ok:
        srs = payload["data"].get("service_requests", [])
        if srs:
            sample_sr = srs[0].get("ticketid")
        note = f"records={len(srs)}, total={payload['data'].get('totalCount', 0)}"
    else:
        note = _truncate(payload.get("error") or payload)
    _record("list_service_requests", ok, payload, ms, note)

    # 7) get_service_request ----------------------------------------------------
    if sample_sr:
        ok, payload, ms = await _call(
            "get_service_request",
            workorders.get_service_request(sample_sr, site_id),
        )
        note = (
            f"ticket={sample_sr} status={payload['data'].get('status')!r}"
            if ok else _truncate(payload.get("error") or payload)
        )
    else:
        ok, payload, ms = False, {"error": "no sample SR discovered"}, 0
        note = "skipped — list_service_requests returned no rows"
    _record("get_service_request", ok, payload, ms, note)

    # 8) list_job_plans ---------------------------------------------------------
    ok, payload, ms = await _call(
        "list_job_plans",
        workorders.list_job_plans(site_id=site_id, page_size=5),
    )
    if ok:
        jps = payload["data"].get("job_plans", [])
        note = f"records={len(jps)}, total={payload['data'].get('totalCount', 0)}"
    else:
        note = _truncate(payload.get("error") or payload)
    _record("list_job_plans", ok, payload, ms, note)

    # 9) get_my_assigned_workorders ---------------------------------------------
    labor_code = os.environ.get("MAXIMO_USERNAME", "MAXADMIN")
    ok, payload, ms = await _call(
        "get_my_assigned_workorders",
        workorders.get_my_assigned_workorders(labor_code=labor_code, site_id=site_id, page_size=5),
    )
    if ok:
        my_wos = payload["data"].get("workorders", [])
        note = f"labor={labor_code} records={len(my_wos)}, total={payload['data'].get('totalCount', 0)}"
    else:
        note = _truncate(payload.get("error") or payload)
    _record("get_my_assigned_workorders", ok, payload, ms, note)

    # 10) get_workorder_tasks ---------------------------------------------------
    if sample_wonum:
        ok, payload, ms = await _call(
            "get_workorder_tasks",
            workorders.get_workorder_tasks(sample_wonum, site_id),
        )
        note = (
            f"parent={sample_wonum} tasks={len(payload['data'].get('tasks', []))}"
            if ok else _truncate(payload.get("error") or payload)
        )
    else:
        ok, payload, ms = False, {"error": "no sample wonum discovered"}, 0
        note = "skipped — list_workorders returned no rows"
    _record("get_workorder_tasks", ok, payload, ms, note)

    # ---------------------------------------------------------------------------
    print("\n".join(results))
    print("-" * 78)
    passed = sum(1 for r in results if r.startswith("[PASS]"))
    print(f"  {passed}/{len(results)} tools passed")
    if failures:
        print("\nFailures detail:")
        for name, payload in failures:
            print(f"  {name}: {_truncate(payload, 360)}")
    return failures


if __name__ == "__main__":
    raise SystemExit(0 if not asyncio.run(_run_smoke()) else 1)
