"""tests/integration/test_smoke_wave3.py — Live smoke test for the 6 Wave-3 planner/scheduler tools."""
from __future__ import annotations
import asyncio
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
    return f"[{'PASS' if ok else 'FAIL'}] {name:<40} {ms:>5}ms  {note}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_smoke_wave3() -> None:
    """Live smoke test — fails if any Wave-3 tool returns success=False."""
    failures = await _run_smoke()
    assert not failures, f"{len(failures)} Wave-3 tool(s) failed: {[name for name, _ in failures]}"


async def _run_smoke() -> List[Tuple[str, Dict[str, Any]]]:
    from tools import workorders, labor
    from core.maximo_client import get_connected_client

    print("=" * 78)
    print("  Wave-3 smoke test against:", os.environ.get("MAXIMO_URL"))
    print("=" * 78)

    try:
        client = await get_connected_client()
        whoami = await client.get("/whoami", params={"lean": "1"})
        print(f"  Connected as: {whoami.get('userName') or whoami.get('personid')!r}")
    except Exception as exc:
        print(f"  CONNECTIVITY FAILED: {exc!r}")
        return [("connectivity", {"error": f"{type(exc).__name__}: {exc!r}"})]

    # Discover a site_id, sample wonum, sample jpnum, and a craft.
    site_id = "BEDFORD"
    sample_wonum: str | None = None
    sample_jpnum: str | None = None
    sample_craft: str | None = None
    try:
        wo = await workorders.list_workorders(site_id=site_id, page_size=5)
        if wo.get("success"):
            wos = wo["data"].get("workorders", [])
            if wos:
                sample_wonum = wos[0].get("wonum")
        jp = await workorders.list_job_plans(page_size=5)
        if jp.get("success"):
            jps = jp["data"].get("job_plans", [])
            if jps:
                sample_jpnum = jps[0].get("jpnum")
        cr = await labor.list_crafts(page_size=3)
        if cr.get("success"):
            crafts = cr["data"].get("crafts", [])
            if crafts:
                sample_craft = crafts[0].get("craft")
        print(f"  site_id={site_id!r}, sample_wonum={sample_wonum!r}, sample_jpnum={sample_jpnum!r}, sample_craft={sample_craft!r}")
    except Exception as exc:
        print(f"  Discovery failed: {exc!r}")

    results: list[str] = []
    failures: list[tuple[str, Dict[str, Any]]] = []

    def record(name: str, ok: bool, payload: Dict[str, Any], ms: int, note: str) -> None:
        results.append(_line(name, ok, ms, note))
        if not ok:
            failures.append((name, payload))

    print("-" * 78)

    # 1. list_crafts ------------------------------------------------------------
    ok, payload, ms = await _call(labor.list_crafts(page_size=5))
    if ok:
        n = len(payload["data"].get("crafts", []))
        ep = payload["data"].get("endpoint")
        note = f"endpoint={ep} crafts={n} sample={payload['data']['crafts'][0] if n else None}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("list_crafts", ok, payload, ms, note)

    # 2. find_available_technician ---------------------------------------------
    ok, payload, ms = await _call(labor.find_available_technician(site_id=site_id, page_size=5))
    if ok:
        d = payload["data"]
        techs = d.get("technicians", [])
        note = f"total={d.get('total_active_labor')} returned={len(techs)} asg_data={d.get('assignment_counts_available')}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("find_available_technician", ok, payload, ms, note)

    # 3. get_job_plan -----------------------------------------------------------
    if sample_jpnum:
        ok, payload, ms = await _call(workorders.get_job_plan(jpnum=sample_jpnum))
        if ok:
            s = payload["data"].get("summary", {})
            note = f"jp={sample_jpnum} tasks={s.get('task_count')} labor={s.get('planned_labor_lines')} mat={s.get('planned_material_lines')}"
        else:
            note = _truncate(payload.get("error") or payload)
    else:
        ok, payload, ms = False, {"error": "no sample jpnum"}, 0
        note = "skipped — no sample jpnum"
    record("get_job_plan", ok, payload, ms, note)

    # 4. estimate_workorder_cost -----------------------------------------------
    if sample_jpnum:
        ok, payload, ms = await _call(workorders.estimate_workorder_cost(jpnum=sample_jpnum))
        if ok:
            d = payload["data"]
            note = f"jp={sample_jpnum} total=${d.get('estimated_total_cost')} labor=${d['labor']['cost']} mat=${d['material']['cost']}"
        else:
            note = _truncate(payload.get("error") or payload)
    else:
        ok, payload, ms = False, {"error": "no sample jpnum"}, 0
        note = "skipped — no sample jpnum"
    record("estimate_workorder_cost", ok, payload, ms, note)

    # 5. get_workorder_actuals_vs_planned --------------------------------------
    if sample_wonum:
        ok, payload, ms = await _call(workorders.get_workorder_actuals_vs_planned(sample_wonum, site_id))
        if ok:
            d = payload["data"]
            note = f"wo={sample_wonum} est_hrs={d['labor_hours']['estimated']} act_hrs={d['labor_hours']['actual']} total_var_pct={d['total_cost'].get('variance_pct')}"
        else:
            note = _truncate(payload.get("error") or payload)
    else:
        ok, payload, ms = False, {"error": "no sample wonum"}, 0
        note = "skipped — no sample wonum"
    record("get_workorder_actuals_vs_planned", ok, payload, ms, note)

    # 6. get_schedule_calendar -------------------------------------------------
    # Pick a wide window so we hit something even on stale demo data.
    ok, payload, ms = await _call(
        workorders.get_schedule_calendar(
            site_id=site_id,
            date_from="1990-01-01",
            date_to="2030-01-01",
            group_by="date",
        )
    )
    if ok:
        d = payload["data"]
        by_date = d.get("by_date", [])
        note = f"scheduled={d.get('total_scheduled')} buckets={len(by_date)} first_day={by_date[0]['date'] if by_date else None}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_schedule_calendar", ok, payload, ms, note)

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
