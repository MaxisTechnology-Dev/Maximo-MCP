"""tests/integration/test_smoke_wave5.py — Live smoke test for the 6 Wave-5 compliance/EHS tools."""
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
    return f"[{'PASS' if ok else 'FAIL'}] {name:<36} {ms:>5}ms  {note}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_smoke_wave5() -> None:
    """Live smoke test — fails if any Wave-5 tool returns success=False."""
    failures = await _run_smoke()
    assert not failures, f"{len(failures)} Wave-5 tool(s) failed: {[name for name, _ in failures]}"


async def _run_smoke() -> List[Tuple[str, Dict[str, Any]]]:
    from tools import compliance
    from core.maximo_client import get_connected_client

    print("=" * 78)
    print("  Wave-5 smoke test against:", os.environ.get("MAXIMO_URL"))
    print("=" * 78)

    try:
        client = await get_connected_client()
        whoami = await client.get("/whoami", params={"lean": "1"})
        print(f"  Connected as: {whoami.get('userName') or whoami.get('personid')!r}")
    except Exception as exc:
        print(f"  CONNECTIVITY FAILED: {exc!r}")
        return [("connectivity", {"error": f"{type(exc).__name__}: {exc!r}"})]

    site_id = "BEDFORD"
    print(f"  site_id={site_id!r}")
    print("-" * 78)

    results: List[str] = []
    failures: List[Tuple[str, Dict[str, Any]]] = []

    def record(name: str, ok: bool, payload: Dict[str, Any], ms: int, note: str) -> None:
        results.append(_line(name, ok, ms, note))
        if not ok:
            failures.append((name, payload))

    # 1. list_calibration_due
    ok, payload, ms = await _call(compliance.list_calibration_due(site_id=site_id, days_ahead=365))
    if ok:
        d = payload["data"]
        unav = d.get("data_unavailable")
        note = f"due={d.get('total_due')} overdue={d.get('overdue_count')} data_unav={bool(unav)}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("list_calibration_due", ok, payload, ms, note)

    # 2. list_inspections_due
    ok, payload, ms = await _call(compliance.list_inspections_due(site_id=site_id, days_ahead=365))
    if ok:
        d = payload["data"]
        note = f"due={d.get('total_due')} overdue={d.get('overdue_count')}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("list_inspections_due", ok, payload, ms, note)

    # 3. list_permits_to_work
    ok, payload, ms = await _call(compliance.list_permits_to_work(site_id=site_id))
    if ok:
        d = payload["data"]
        unav = d.get("data_unavailable")
        note = f"permits={d.get('totalCount', len(d.get('permits', [])))} data_unav={bool(unav)}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("list_permits_to_work", ok, payload, ms, note)

    # 4. list_certifications_expiring
    ok, payload, ms = await _call(compliance.list_certifications_expiring(days_ahead=365))
    if ok:
        d = payload["data"]
        note = f"labor={d.get('total_active_labor')} buckets={d.get('buckets')} data_unav={bool(d.get('data_unavailable'))}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("list_certifications_expiring", ok, payload, ms, note)

    # 5. list_incidents
    ok, payload, ms = await _call(compliance.list_incidents(site_id=site_id))
    if ok:
        d = payload["data"]
        note = f"source={d.get('source')} incidents={d.get('totalCount', len(d.get('incidents', [])))}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("list_incidents", ok, payload, ms, note)

    # 6. get_compliance_dashboard
    ok, payload, ms = await _call(compliance.get_compliance_dashboard(site_id=site_id, days_ahead=365))
    if ok:
        d = payload["data"]
        note = f"summary={d.get('summary')} unavailable_sections={d.get('data_unavailable_sections')}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_compliance_dashboard", ok, payload, ms, note)

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
