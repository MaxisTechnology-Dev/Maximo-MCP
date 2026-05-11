"""tests/integration/test_smoke_wave8.py — Live smoke test for the 6 Wave-8 AI-moat tools.

Each tool gracefully falls back to deterministic / statistical output when
no OPENAI_API_KEY is set, so this smoke validates the rule-based path
even on CI with no LLM credentials. When the operator has a real
OPENAI_API_KEY, the same test will produce LLM-enhanced output and the
`source` field flips to "llm-enhanced".
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
    return f"[{'PASS' if ok else 'FAIL'}] {name:<36} {ms:>5}ms  {note}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_smoke_wave8() -> None:
    """Live smoke — every Wave-8 tool must return success=True (rule-based or LLM)."""
    failures = await _run_smoke()
    assert not failures, f"{len(failures)} Wave-8 tool(s) failed: {[name for name, _ in failures]}"


async def _run_smoke() -> List[Tuple[str, Dict[str, Any]]]:
    from tools import ai_moat, workorders
    from core.maximo_client import get_connected_client

    print("=" * 78)
    print("  Wave-8 AI-moat smoke test against:", os.environ.get("MAXIMO_URL"))
    print("  OPENAI_API_KEY:", "set" if os.environ.get("OPENAI_API_KEY") else "not set (rule-based path)")
    print("=" * 78)

    try:
        client = await get_connected_client()
        whoami = await client.get("/whoami", params={"lean": "1"})
        print(f"  Connected as: {whoami.get('userName') or whoami.get('personid')!r}")
    except Exception as exc:
        print(f"  CONNECTIVITY FAILED: {exc!r}")
        return [("connectivity", {"error": f"{type(exc).__name__}: {exc!r}"})]

    site_id = "BEDFORD"
    asset_num = "1001"

    # Discover a real wonum
    sample_wonum: str | None = None
    try:
        wo_list = await workorders.list_workorders(site_id=site_id, page_size=5)
        if wo_list.get("success"):
            wos = wo_list["data"].get("workorders", [])
            if wos:
                sample_wonum = wos[0].get("wonum")
    except Exception:
        pass
    print(f"  site_id={site_id!r}  asset_num={asset_num!r}  sample_wonum={sample_wonum!r}")
    print("-" * 78)

    results: List[str] = []
    failures: List[Tuple[str, Dict[str, Any]]] = []

    def record(name, ok, payload, ms, note):
        results.append(_line(name, ok, ms, note))
        if not ok:
            failures.append((name, payload))

    # 1. generate_workorder_summary
    if sample_wonum:
        ok, p, ms = await _call(ai_moat.generate_workorder_summary(sample_wonum, site_id))
        d = p.get("data", {}) if ok else {}
        note = (
            f"wo={sample_wonum} src={d.get('source')!r} para_len={len((d.get('summary_paragraph') or ''))}"
            if ok else _truncate(p.get("error") or p)
        )
    else:
        ok, p, ms = False, {"error": "no sample wonum"}, 0
        note = "skipped — no sample wonum"
    record("generate_workorder_summary", ok, p, ms, note)

    # 2. auto_classify_failure
    ok, p, ms = await _call(
        ai_moat.auto_classify_failure(
            description="Pump making unusual grinding noise during operation",
            asset_num=asset_num, site_id=site_id,
        )
    )
    d = p.get("data", {}) if ok else {}
    note = (
        f"src={d.get('source')!r} considered={d.get('total_classes_considered')} "
        f"top1={(d.get('rankings') or [{}])[0].get('failurecode')!r}"
        if ok else _truncate(p.get("error") or p)
    )
    record("auto_classify_failure", ok, p, ms, note)

    # 3. chat_with_asset
    ok, p, ms = await _call(
        ai_moat.chat_with_asset(
            asset_num=asset_num, site_id=site_id,
            question="When did this asset last fail and what was the root cause?",
            lookback_days=3650,
        )
    )
    d = p.get("data", {}) if ok else {}
    ctx = d.get("context_used", {})
    note = (
        f"src={d.get('source')!r} wos={ctx.get('wo_count')} corrective={ctx.get('corrective_wo_count')} "
        f"answer_len={len((d.get('answer') or ''))}"
        if ok else _truncate(p.get("error") or p)
    )
    record("chat_with_asset", ok, p, ms, note)

    # 4. recommend_pm_optimization
    ok, p, ms = await _call(
        ai_moat.recommend_pm_optimization(asset_num=asset_num, site_id=site_id, period_months=240)
    )
    d = p.get("data", {}) if ok else {}
    note = (
        f"src={d.get('source')!r} pms={d.get('active_pms')} cm_wos={d.get('corrective_wos')} "
        f"recs={len(d.get('recommendations') or [])} data_unav={bool(d.get('data_unavailable'))}"
        if ok else _truncate(p.get("error") or p)
    )
    record("recommend_pm_optimization", ok, p, ms, note)

    # 5. predict_failure_window
    ok, p, ms = await _call(
        ai_moat.predict_failure_window(asset_num=asset_num, site_id=site_id, lookback_months=240)
    )
    d = p.get("data", {}) if ok else {}
    note = (
        f"src={d.get('source')!r} failures={d.get('corrective_failure_count')} "
        f"mtbf={d.get('mtbf_days')}d urgency={d.get('urgency')!r} data_unav={bool(d.get('data_unavailable'))}"
        if ok else _truncate(p.get("error") or p)
    )
    record("predict_failure_window", ok, p, ms, note)

    # 6. generate_runbook_from_history
    ok, p, ms = await _call(
        ai_moat.generate_runbook_from_history(
            asset_num=asset_num, site_id=site_id,
            problem_description="Reduce track speed; asset showing wear and degradation",
            lookback_months=240,
        )
    )
    d = p.get("data", {}) if ok else {}
    rb = d.get("runbook") if isinstance(d.get("runbook"), dict) else {}
    note = (
        f"src={d.get('source')!r} same={d.get('same_asset_relevant_wos')} "
        f"similar={d.get('similar_asset_relevant_wos')} "
        f"steps={len((rb or {}).get('steps') or [])} data_unav={bool(d.get('data_unavailable'))}"
        if ok else _truncate(p.get("error") or p)
    )
    record("generate_runbook_from_history", ok, p, ms, note)

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
