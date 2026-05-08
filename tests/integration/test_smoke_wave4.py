"""tests/integration/test_smoke_wave4.py — Live smoke for Wave-4 procurement/cost + Wave-6 exports."""
from __future__ import annotations
import asyncio
import base64
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _truncate(value: Any, max_len: int = 240) -> str:
    s = json.dumps(value, default=str) if not isinstance(value, str) else value
    return s if len(s) <= max_len else s[:max_len] + "…"


async def _call(coro) -> Tuple[bool, Dict[str, Any], int]:
    start = time.monotonic()
    try:
        result = await coro
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc!r}"}, int((time.monotonic() - start) * 1000)
    return bool(result.get("success")), result, int((time.monotonic() - start) * 1000)


def _line(name: str, ok: bool, ms: int, note: str) -> str:
    return f"[{'PASS' if ok else 'FAIL'}] {name:<36} {ms:>5}ms  {note}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_smoke_wave4() -> None:
    """Live smoke test — fails if any Wave-4 + Wave-6 tool returns success=False."""
    failures = await _run_smoke()
    assert not failures, f"{len(failures)} Wave-4/6 tool(s) failed: {[name for name, _ in failures]}"


async def _run_smoke() -> List[Tuple[str, Dict[str, Any]]]:
    from tools import purchasing, workorders, inventory, assets, reporting
    from core.maximo_client import get_connected_client

    print("=" * 78)
    print("  Wave-4 + Wave-6 smoke test against:", os.environ.get("MAXIMO_URL"))
    print("=" * 78)

    try:
        client = await get_connected_client()
        whoami = await client.get("/whoami", params={"lean": "1"})
        print(f"  Connected as: {whoami.get('userName') or whoami.get('personid')!r}")
    except Exception as exc:
        print(f"  CONNECTIVITY FAILED: {exc!r}")
        return [("connectivity", {"error": f"{type(exc).__name__}: {exc!r}"})]

    site_id = "BEDFORD"
    sample_wonum: str | None = None
    try:
        wo = await workorders.list_workorders(site_id=site_id, page_size=5)
        if wo.get("success"):
            wos = wo["data"].get("workorders", [])
            if wos:
                sample_wonum = wos[0].get("wonum")
        print(f"  site_id={site_id!r}, sample_wonum={sample_wonum!r}")
    except Exception as exc:
        print(f"  Discovery failed: {exc!r}")

    results: list[str] = []
    failures: list[tuple[str, Dict[str, Any]]] = []

    def record(name: str, ok: bool, payload: Dict[str, Any], ms: int, note: str) -> None:
        results.append(_line(name, ok, ms, note))
        if not ok:
            failures.append((name, payload))

    print("-" * 78)

    # 1. list_purchase_requisitions ---------------------------------------------
    ok, payload, ms = await _call(purchasing.list_purchase_requisitions(site_id=site_id, page_size=5))
    if ok:
        prs = payload["data"].get("purchase_requisitions", [])
        note = f"records={len(prs)} total={payload['data'].get('totalCount', 0)}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("list_purchase_requisitions", ok, payload, ms, note)

    # 2. get_spend_analysis -----------------------------------------------------
    ok, payload, ms = await _call(purchasing.get_spend_analysis(site_id=site_id, period_months=240, group_by="vendor"))
    if ok:
        d = payload["data"]
        note = f"pos={d.get('total_purchase_orders')} spend=${d.get('total_spend')} top_vendors={len(d.get('top_groups', []))}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_spend_analysis", ok, payload, ms, note)

    # 3. get_workorder_costs ----------------------------------------------------
    if sample_wonum:
        ok, payload, ms = await _call(workorders.get_workorder_costs(sample_wonum, site_id))
        if ok:
            d = payload["data"]
            note = f"wo={sample_wonum} total=${d.get('total_cost')} categories={[b['category'] for b in d.get('breakdown', [])]}"
        else:
            note = _truncate(payload.get("error") or payload)
    else:
        ok, payload, ms = False, {"error": "no sample wonum"}, 0
        note = "skipped — no sample wonum"
    record("get_workorder_costs", ok, payload, ms, note)

    # 4. get_inventory_valuation ------------------------------------------------
    ok, payload, ms = await _call(inventory.get_inventory_valuation(site_id=site_id, top_n=5))
    if ok:
        d = payload["data"]
        note = f"items={d.get('total_items')} with_cost={d.get('items_with_known_cost')} valuation=${d.get('total_valuation')}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_inventory_valuation", ok, payload, ms, note)

    # 5. get_critical_spares_check ---------------------------------------------
    ok, payload, ms = await _call(inventory.get_critical_spares_check(site_id=site_id, priority_threshold=3))
    if ok:
        d = payload["data"]
        unav = d.get("data_unavailable")
        note = f"critical={d.get('critical_asset_count')} data_unav={bool(unav)}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_critical_spares_check", ok, payload, ms, note)

    # 6. get_warranty_status ---------------------------------------------------
    ok, payload, ms = await _call(assets.get_warranty_status(site_id=site_id))
    if ok:
        d = payload["data"]
        note = f"assets={d.get('total_assets')} buckets={d.get('buckets')} data_unav={bool(d.get('data_unavailable'))}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_warranty_status", ok, payload, ms, note)

    # 7. export_workorders_excel -----------------------------------------------
    ok, payload, ms = await _call(reporting.export_workorders_excel(site_id=site_id, max_records=10))
    if ok:
        d = payload["data"]
        b64 = d.get("base64_content", "")
        try:
            size_bytes = len(base64.b64decode(b64)) if b64 else 0
        except Exception:
            size_bytes = -1
        note = f"file={d.get('filename')} records={d.get('record_count')} size={size_bytes}b"
    else:
        note = _truncate(payload.get("error") or payload)
    record("export_workorders_excel", ok, payload, ms, note)

    # 8. export_asset_report_pdf -----------------------------------------------
    ok, payload, ms = await _call(reporting.export_asset_report_pdf(site_id=site_id, max_records=10))
    if ok:
        d = payload["data"]
        b64 = d.get("base64_content", "")
        try:
            size_bytes = len(base64.b64decode(b64)) if b64 else 0
        except Exception:
            size_bytes = -1
        note = f"file={d.get('filename')} records={d.get('record_count')} size={size_bytes}b"
    else:
        note = _truncate(payload.get("error") or payload)
    record("export_asset_report_pdf", ok, payload, ms, note)

    print("\n".join(results))
    print("-" * 78)
    passed = sum(1 for r in results if r.startswith("[PASS]"))
    print(f"  {passed}/{len(results)} tools passed")
    if failures:
        print("\nFailure detail:")
        for name, payload in failures:
            print(f"  {name}: {_truncate(payload, 360)}")
    return failures


if __name__ == "__main__":
    raise SystemExit(0 if not asyncio.run(_run_smoke()) else 1)
