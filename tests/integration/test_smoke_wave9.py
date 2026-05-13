"""tests/integration/test_smoke_wave9.py — Live smoke test for the 2 Wave-9 spatial tools.

Both tools are designed to ALWAYS succeed even when Maximo Spatial is not
installed. The success criterion isn't "found assets / built a route"
(that requires populated coordinates) but "tool returns a structured
envelope with `data_unavailable=True` and a friendly note when no
spatial data is available, OR returns geographic data when it is".
"""
from __future__ import annotations
import asyncio
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


async def _call(coro) -> Tuple[bool, Dict[str, Any], int]:
    start = time.monotonic()
    try:
        result = await coro
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc!r}"}, int((time.monotonic() - start) * 1000)
    return bool(result.get("success")), result, int((time.monotonic() - start) * 1000)


def _line(name: str, ok: bool, ms: int, note: str) -> str:
    return f"[{'PASS' if ok else 'FAIL'}] {name:<36} {ms:>5}ms  {_redact(note)}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_smoke_wave9() -> None:
    """Live smoke — both Wave-9 tools must return success=True (with or without spatial data)."""
    failures = await _run_smoke()
    assert not failures, f"{len(failures)} Wave-9 tool(s) failed: {[name for name, _ in failures]}"


async def _run_smoke() -> List[Tuple[str, Dict[str, Any]]]:
    from tools import spatial
    from core.maximo_client import get_connected_client

    print("=" * 78)
    print("  Wave-9 spatial smoke test against:", os.environ.get("MAXIMO_URL"))
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

    def record(name, ok, payload, ms, note):
        # On the failure path we ignore the caller-supplied note (which may
        # echo the API error message including potentially-sensitive content)
        # and replace it with the structured `error_code` enum only. Full
        # payload still goes into `failures` for the assertion message, but
        # only as part of the test failure — never via a `print()` sink.
        if not ok:
            code = payload.get("error_code", "UNKNOWN") if isinstance(payload, dict) else "UNKNOWN"
            safe_note = f"error_code={code}"
            results.append(_line(name, False, ms, safe_note))
            failures.append((name, payload))
        else:
            results.append(_line(name, True, ms, note))

    # 1. find_assets_near_location — Burlington/Bedford-ish coordinates
    ok, p, ms = await _call(
        spatial.find_assets_near_location(
            latitude=42.4906, longitude=-71.2756,  # Bedford, MA, US
            radius_km=50, site_id=site_id, max_results=20,
        )
    )
    d = p.get("data", {}) if ok else {}
    note = (
        f"matching={d.get('matching_count')} candidates={d.get('total_candidate_assets')} "
        f"variant={d.get('coordinate_variant_used')!r} data_unav={bool(d.get('data_unavailable'))}"
        if ok else ""  # record() supplies a sanitized failure note from error_code
    )
    record("find_assets_near_location", ok, p, ms, note)

    # 2. get_route_for_technician — use maxadmin as the labor (smoke test pattern)
    ok, p, ms = await _call(
        spatial.get_route_for_technician(labor_code="maxadmin", site_id=site_id, max_workorders=10)
    )
    d = p.get("data", {}) if ok else {}
    note = (
        f"route_len={d.get('route_length')} geographic={d.get('geographic_optimisation')} "
        f"data_unav={bool(d.get('data_unavailable_note'))}"
        if ok else ""  # record() supplies a sanitized failure note from error_code
    )
    record("get_route_for_technician", ok, p, ms, note)

    # Verify the friendly-message contract: when data_unavailable=True,
    # there must be a non-empty data_unavailable_note explaining what's missing.
    for name, payload in [("find_assets_near_location", p)]:
        d = payload.get("data", {}) if payload.get("success") else {}
        if d.get("data_unavailable"):
            assert d.get("data_unavailable_note"), (
                f"{name}: data_unavailable=True but no data_unavailable_note set. "
                "User-friendly messaging contract broken."
            )

    # Print only an aggregate pass/fail count. Per-tool result lines live in
    # the `results` list but are deliberately NOT printed — CodeQL flagged
    # the bulk `print(results)` as a clear-text-logging sink because the
    # taint flow analysis traces env-var reads (auth headers) all the way
    # through to the formatted lines. Operators can still see which tools
    # failed via the AssertionError message + per-test pytest output.
    print("-" * 78)
    passed = sum(1 for r in results if r.startswith("[PASS]"))
    print(f"  {passed}/{len(results)} tools passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(0 if not asyncio.run(_run_smoke()) else 1)
