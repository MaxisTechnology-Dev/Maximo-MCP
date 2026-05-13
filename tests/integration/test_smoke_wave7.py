"""tests/integration/test_smoke_wave7.py — Live smoke test for the 18 Wave-7 vertical tools."""
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


# Env-var values we never want to surface in stdout / CI logs. Maximo error
# responses can echo back parts of the auth header or user identifier; CodeQL's
# taint analysis (correctly) flags `print(payload)` paths that could leak this.
_SECRET_VARS = ("MAXIMO_PASSWORD", "MAXIMO_USERNAME", "MAXIMO_API_KEY",
                "OPENAI_API_KEY", "MCP_ACCESS_TOKEN")


def _redact(s: str) -> str:
    """Mask known secret env-var values inside a string before printing."""
    if not isinstance(s, str) or not s:
        return s
    for var in _SECRET_VARS:
        v = os.environ.get(var, "")
        if v and len(v) >= 3:
            s = s.replace(v, "***REDACTED***")
    return s


def _truncate(value: Any, max_len: int = 240) -> str:
    s = json.dumps(value, default=str) if not isinstance(value, str) else value
    s = _redact(s)
    return s if len(s) <= max_len else s[:max_len] + "…"


async def _call(coro) -> Tuple[bool, Dict[str, Any], int]:
    start = time.monotonic()
    try:
        result = await coro
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc!r}"}, int((time.monotonic() - start) * 1000)
    return bool(result.get("success")), result, int((time.monotonic() - start) * 1000)


def _line(name: str, ok: bool, ms: int, note: str) -> str:
    return f"[{'PASS' if ok else 'FAIL'}] {name:<38} {ms:>5}ms  {_redact(note)}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_smoke_wave7() -> None:
    """Live smoke test — fails if any Wave-7 vertical tool returns success=False."""
    failures = await _run_smoke()
    assert not failures, f"{len(failures)} Wave-7 tool(s) failed: {[name for name, _ in failures]}"


async def _run_smoke() -> List[Tuple[str, Dict[str, Any]]]:
    from tools import verticals
    from core.maximo_client import get_connected_client

    print("=" * 78)
    print("  Wave-7 vertical smoke test against:", os.environ.get("MAXIMO_URL"))
    print("=" * 78)

    try:
        client = await get_connected_client()
        whoami = await client.get("/whoami", params={"lean": "1"})
        print(f"  Connected as: {whoami.get('userName') or whoami.get('personid')!r}")
    except Exception as exc:
        print(f"  CONNECTIVITY FAILED: {exc!r}")
        return [("connectivity", {"error": f"{type(exc).__name__}: {exc!r}"})]

    site_id = "BEDFORD"
    asset_num = "1001"  # known asset on demo data
    print(f"  site_id={site_id!r}  asset_num={asset_num!r}")
    print("-" * 78)

    results: List[str] = []
    failures: List[Tuple[str, Dict[str, Any]]] = []

    def record(name: str, ok: bool, payload: Dict[str, Any], ms: int, note: str) -> None:
        results.append(_line(name, ok, ms, note))
        if not ok:
            failures.append((name, payload))

    # ── Pharma ─────────────────────────────────────────────────────────────────
    ok, p, ms = await _call(verticals.get_calibration_audit_trail(asset_num, site_id, period_months=24))
    note = (
        f"cals={p['data'].get('total_calibrations')} completed={p['data'].get('completed_calibrations')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_calibration_audit_trail", ok, p, ms, note)

    ok, p, ms = await _call(verticals.list_cleanroom_assets(site_id))
    note = (
        f"cleanroom_locs={p['data'].get('matching_locations')} assets={len(p['data'].get('assets_in_cleanroom', []))} "
        f"data_unav={bool(p['data'].get('data_unavailable'))}"
        if ok else _truncate(p.get("error") or p)
    )
    record("list_cleanroom_assets", ok, p, ms, note)

    ok, p, ms = await _call(verticals.get_gxp_compliance_status(site_id))
    note = (
        f"risk={p['data'].get('gxp_risk_score')} rating={p['data'].get('compliance_rating')!r} "
        f"unavail={p['data'].get('data_unavailable_sections')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_gxp_compliance_status", ok, p, ms, note)

    # ── Oil & gas ──────────────────────────────────────────────────────────────
    ok, p, ms = await _call(verticals.get_turnaround_status(site_id, top_n=3))
    note = (
        f"parent_groups={p['data'].get('total_parent_workorders')} top={len(p['data'].get('top_turnarounds', []))}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_turnaround_status", ok, p, ms, note)

    ok, p, ms = await _call(verticals.list_pressure_vessels_due(site_id, days_ahead=365))
    d = p.get("data", {}) if ok else {}
    note = (
        f"vessels={d.get('total_pressure_vessels')} due_in_window={d.get('inspections_due_in_window')} data_unav={bool(d.get('data_unavailable'))}"
        if ok else _truncate(p.get("error") or p)
    )
    record("list_pressure_vessels_due", ok, p, ms, note)

    ok, p, ms = await _call(verticals.get_lifting_register(site_id, period_months=240))
    note = (
        f"lifts={p['data'].get('total_lift_workorders')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_lifting_register", ok, p, ms, note)

    # ── Manufacturing ──────────────────────────────────────────────────────────
    ok, p, ms = await _call(verticals.get_oee(site_id, period_days=365))
    d = p.get("data", {}) if ok else {}
    note = (
        f"avail={d.get('availability_pct')}% downtime_hr={d.get('downtime_hours')} corrective_wos={d.get('corrective_wo_count')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_oee", ok, p, ms, note)

    ok, p, ms = await _call(verticals.get_production_line_status(site_id))
    d = p.get("data", {}) if ok else {}
    note = (
        f"locations_in_scope={d.get('locations_in_scope')} open_wos={d.get('open_wos')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_production_line_status", ok, p, ms, note)

    ok, p, ms = await _call(verticals.list_changeover_workorders(site_id, period_months=240))
    note = (
        f"changeovers={p['data'].get('total_changeovers')} avg_hr={p['data'].get('avg_changeover_hours')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("list_changeover_workorders", ok, p, ms, note)

    # ── Utilities ──────────────────────────────────────────────────────────────
    ok, p, ms = await _call(verticals.get_outage_impact_analysis(asset_num, site_id))
    d = p.get("data", {}) if ok else {}
    note = (
        f"impact_score={d.get('impact_score')} children={d.get('child_asset_count')} downstream_locs={d.get('downstream_location_count')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_outage_impact_analysis", ok, p, ms, note)

    # Use a real top-level location for the zone test — fall back to "" if discovery fails
    zone = "BR450"
    ok, p, ms = await _call(verticals.list_grid_zone_assets(site_id, zone))
    d = p.get("data", {}) if ok else {}
    note = (
        f"zone={zone} locs_in_zone={d.get('locations_in_zone')} assets={d.get('asset_count')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("list_grid_zone_assets", ok, p, ms, note)

    ok, p, ms = await _call(verticals.get_reliability_indices(site_id, period_months=240))
    d = p.get("data", {}) if ok else {}
    note = (
        f"customers={d.get('customer_locations')} outages={d.get('outage_workorders')} saidi_min={d.get('saidi_proxy_minutes_per_customer')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_reliability_indices", ok, p, ms, note)

    # ── Healthcare ─────────────────────────────────────────────────────────────
    ok, p, ms = await _call(verticals.list_medical_devices_due(site_id, days_ahead=365))
    d = p.get("data", {}) if ok else {}
    note = (
        f"devices={d.get('total_medical_devices')} due={d.get('due_in_window')} data_unav={bool(d.get('data_unavailable'))}"
        if ok else _truncate(p.get("error") or p)
    )
    record("list_medical_devices_due", ok, p, ms, note)

    ok, p, ms = await _call(verticals.get_device_lifecycle_status(site_id))
    d = p.get("data", {}) if ok else {}
    note = (
        f"total={d.get('total_assets')} buckets={d.get('buckets')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_device_lifecycle_status", ok, p, ms, note)

    ok, p, ms = await _call(verticals.get_environment_of_care_status(site_id))
    d = p.get("data", {}) if ok else {}
    note = (
        f"score={d.get('eoc_score')} rating={d.get('rating')!r} unavail={d.get('data_unavailable_sections')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_environment_of_care_status", ok, p, ms, note)

    # ── Transportation ─────────────────────────────────────────────────────────
    ok, p, ms = await _call(verticals.get_fleet_readiness(site_id))
    d = p.get("data", {}) if ok else {}
    note = (
        f"fleet={d.get('fleet_size')} ready={d.get('ready_vehicles')} ready_pct={d.get('readiness_pct')} method={d.get('detection_method')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_fleet_readiness", ok, p, ms, note)

    ok, p, ms = await _call(verticals.list_mileage_based_pm_due(site_id))
    d = p.get("data", {}) if ok else {}
    note = (
        f"matched_meters={d.get('matched_meters')} data_unav={bool(d.get('data_unavailable'))}"
        if ok else _truncate(p.get("error") or p)
    )
    record("list_mileage_based_pm_due", ok, p, ms, note)

    ok, p, ms = await _call(verticals.get_fuel_consumption_trend(asset_num, site_id, period_days=365))
    d = p.get("data", {}) if ok else {}
    note = (
        f"readings={d.get('reading_count')} intervals={d.get('intervals_analysed')} avg_per_day={d.get('avg_consumption_per_day')} spikes={d.get('spike_count')}"
        if ok else _truncate(p.get("error") or p)
    )
    record("get_fuel_consumption_trend", ok, p, ms, note)

    # The `results` list holds lines from `_line(...)` which already routes its
    # `note` field through `_redact()` — see helper above. Runtime values of
    # MAXIMO_PASSWORD / OPENAI_API_KEY / etc. are replaced with ***REDACTED***
    # before reaching stdout. CodeQL's taint flow doesn't model our redaction
    # as a sanitiser, so we suppress the false positive here.
    print("\n".join(results))  # lgtm[py/clear-text-logging-sensitive-data]
    print("-" * 78)
    passed = sum(1 for r in results if r.startswith("[PASS]"))
    print(f"  {passed}/{len(results)} tools passed")
    if failures:
        print("\nFailure detail:")
        for name, payload in failures:
            # _truncate() applies _redact() before returning — see helper above.
            print(f"  {name}: {_truncate(payload, 360)}")  # lgtm[py/clear-text-logging-sensitive-data]
    return failures


if __name__ == "__main__":
    raise SystemExit(0 if not asyncio.run(_run_smoke()) else 1)
