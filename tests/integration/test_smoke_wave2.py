"""
tests/integration/test_smoke_wave2.py — Live smoke test for Wave-2 tools.

8 new tools:
  Reliability  : get_failure_class_hierarchy, get_meter_readings,
                 get_asset_criticality_matrix
  Reporting    : get_failure_pareto, get_bad_actor_assets
  AI (re-enabled): detect_asset_anomalies, suggest_root_cause,
                   summarize_asset_health
"""
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
    return f"[{'PASS' if ok else 'FAIL'}] {name:<32} {ms:>5}ms  {note}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_smoke_wave2() -> None:
    """Live smoke test — fails if any Wave-2 tool returns success=False."""
    failures = await _run_smoke()
    assert not failures, f"{len(failures)} Wave-2 tool(s) failed: {[name for name, _ in failures]}"


async def _run_smoke() -> List[Tuple[str, Dict[str, Any]]]:
    from tools import assets, reporting, ai_intelligence as ai, workorders
    from core.maximo_client import get_connected_client

    print("=" * 78)
    print("  Wave-2 smoke test against:", os.environ.get("MAXIMO_URL"))
    print("=" * 78)

    try:
        client = await get_connected_client()
        whoami = await client.get("/whoami", params={"lean": "1"})
        print(f"  Connected as user: {whoami.get('userName') or whoami.get('personid')!r}")
    except Exception as exc:
        print(f"  CONNECTIVITY FAILED: {exc!r}")
        return [("connectivity", {"error": f"{type(exc).__name__}: {exc!r}"})]

    # Discovery: pick a site_id and an asset_num that has WO history.
    site_id = "BEDFORD"
    sample_asset = None
    try:
        wo_list = await workorders.list_workorders(site_id=site_id, page_size=20)
        if wo_list.get("success"):
            for w in wo_list["data"].get("workorders", []):
                if w.get("assetnum"):
                    sample_asset = w["assetnum"]
                    break
        print(f"  site_id={site_id!r}, sample_asset={sample_asset!r}")
    except Exception as exc:
        print(f"  Discovery failed: {exc!r}")

    results = []
    failures = []

    def record(name, ok, payload, ms, note):
        results.append(_line(name, ok, ms, note))
        if not ok:
            failures.append((name, payload))

    print("-" * 78)

    # 1. get_failure_class_hierarchy --------------------------------------------
    ok, payload, ms = await _call(assets.get_failure_class_hierarchy())
    if ok:
        classes = payload["data"].get("classes", [])
        endpoint = payload["data"].get("endpoint")
        note = f"endpoint={endpoint} root_classes={len(classes)}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_failure_class_hierarchy", ok, payload, ms, note)

    # 2. get_asset_criticality_matrix ------------------------------------------
    ok, payload, ms = await _call(assets.get_asset_criticality_matrix(site_id=site_id, top_n=5))
    if ok:
        d = payload["data"]
        note = f"total_assets={d.get('total_assets')} buckets={d.get('priority_buckets')}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_asset_criticality_matrix", ok, payload, ms, note)

    # 3. get_meter_readings -----------------------------------------------------
    if sample_asset:
        ok, payload, ms = await _call(
            assets.get_meter_readings(asset_num=sample_asset, site_id=site_id, period_days=365)
        )
        if ok:
            d = payload["data"]
            note = f"asset={sample_asset} readings={len(d.get('readings', []))} meters={len(d.get('meters_summary', []))}"
        else:
            note = _truncate(payload.get("error") or payload)
    else:
        ok, payload, ms = False, {"error": "no sample asset"}, 0
        note = "skipped — no sample asset"
    record("get_meter_readings", ok, payload, ms, note)

    # 4. get_failure_pareto -----------------------------------------------------
    ok, payload, ms = await _call(reporting.get_failure_pareto(site_id=site_id, period_months=24, top_n=5))
    if ok:
        d = payload["data"]
        note = f"corrective_wos={d.get('total_corrective_wos')} with_code={d.get('total_with_failure_code')} top={len(d.get('pareto', []))}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_failure_pareto", ok, payload, ms, note)

    # 5. get_bad_actor_assets ---------------------------------------------------
    ok, payload, ms = await _call(reporting.get_bad_actor_assets(site_id=site_id, period_months=24, top_n=5))
    if ok:
        d = payload["data"]
        ba = d.get("bad_actors", [])
        sample = ba[0] if ba else None
        note = f"unique_assets={d.get('total_assets_with_corrective_wo')} top={len(ba)} sample={sample}"
    else:
        note = _truncate(payload.get("error") or payload)
    record("get_bad_actor_assets", ok, payload, ms, note)

    # 6. detect_asset_anomalies -------------------------------------------------
    if sample_asset:
        ok, payload, ms = await _call(
            ai.detect_asset_anomalies(asset_num=sample_asset, site_id=site_id, lookback_days=365)
        )
        if ok:
            d = payload["data"]
            note = f"failures={d.get('total_failures_in_window')} anomaly={d.get('anomaly_detected')} severity={d.get('severity')}"
        else:
            note = _truncate(payload.get("error") or payload)
    else:
        ok, payload, ms = False, {"error": "no sample asset"}, 0
        note = "skipped — no sample asset"
    record("detect_asset_anomalies", ok, payload, ms, note)

    # 7. suggest_root_cause -----------------------------------------------------
    if sample_asset:
        ok, payload, ms = await _call(
            ai.suggest_root_cause(
                asset_num=sample_asset,
                site_id=site_id,
                failure_description="Pump making unusual grinding noise during operation",
            )
        )
        if ok:
            d = payload["data"]
            note = f"causes={len(d.get('root_causes', []))} source={d.get('source')} history={d.get('historical_failures_analysed')}"
        else:
            note = _truncate(payload.get("error") or payload)
    else:
        ok, payload, ms = False, {"error": "no sample asset"}, 0
        note = "skipped — no sample asset"
    record("suggest_root_cause", ok, payload, ms, note)

    # 8. summarize_asset_health -------------------------------------------------
    if sample_asset:
        ok, payload, ms = await _call(
            ai.summarize_asset_health(asset_num=sample_asset, site_id=site_id)
        )
        if ok:
            d = payload["data"]
            note = f"score={d.get('overall_score')} status={d.get('status_label')!r} issues={len(d.get('key_issues', []))}"
        else:
            note = _truncate(payload.get("error") or payload)
    else:
        ok, payload, ms = False, {"error": "no sample asset"}, 0
        note = "skipped — no sample asset"
    record("summarize_asset_health", ok, payload, ms, note)

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
