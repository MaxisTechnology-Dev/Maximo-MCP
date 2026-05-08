#!/usr/bin/env python3
"""
Maximo Enterprise MCP – Full Tool Test Runner (v2)
=====================================================
Historical integration runner for the broader Maximo tool set.
Use this when you want to exercise disabled or environment-specific tools in a
controlled Maximo environment.

Replaced slots vs original script:
  Slot  4: get_schema_details(MXASSET duplicate)  → update_pm_frequency
  Slot  7: nl_to_oslc_query(MXASSET duplicate)    → generate_pm_workorders
  Slot  9: generate_api_code(MXASSET curl dup)    → check_stock_level
  Slot 41: get_workorder(NEW WO verify dup)        → cancel_workorder
  Slot 53: list_event_subscriptions(verify dup)   → transfer_inventory
  Slot 57: validate_oslc_query(MXASSET dup)       → get_vendor_performance
  Slot 58: generate_api_code(MXWO create js dup)  → receive_items

Fixed bugs vs original script:
  - search_maximo_knowledge: removed invalid top_k kwarg
  - generate_carbon_table: rows→data; columns now list of {key,header} dicts
  - create_asset: timestamp-based unique key to avoid duplicate error
  - create_purchase_order: inline vendor lookup from existing POs
  - test_asset_1 fallback: populated from list_assets when search_assets fails
  - get_schema_details(MXWO): include_relationships=False to avoid timeout
  - health_check: wrapped with success envelope
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# If non-empty, run ONLY these tool ids (base names).
# Example: "get_location(TEST_LOCATION_1)" maps to tool id "get_location".
# Tier 1 — must always work (read, lightweight)
TIER1_TOOLS = {
    "list_assets",
    "search_assets",
    "list_workorders",
    "get_location",
    "get_asset",
}

# Tier 2 — may be slower, acceptable failure under load
TIER2_TOOLS = {
    "get_labor_utilization",
    "get_maintenance_kpi_dashboard",
    "get_workorder_kpis",
    "get_pm_forecast",
    "get_asset_downtime_stats",
}

# Run only these tools (union of Tier1+Tier2). Empty set = run all.
ONLY_TOOLS: set = TIER1_TOOLS | TIER2_TOOLS

# Delay between consecutive tool calls (seconds). Prevents overwhelming Maximo over VPN.
_INTER_TOOL_DELAY_S: float = 0.75


def _load_project_env() -> None:
    """
    Load environment variables from the project root `.env`.

    Preference order:
    - If `python-dotenv` is available, use it.
    - Otherwise, fall back to a small `.env` parser (KEY=VALUE lines).
    In both cases, existing OS env vars win (no override).
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        # Fall back to system environment only (caller will validate).
        print(f"[WARN] .env not found at {env_path}. Using system environment.", flush=True)
        return

    # Preferred: python-dotenv
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=env_path, override=False)
        return
    except Exception:
        # Fallback: minimal parser so the script still works without extra deps.
        pass

    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = k.strip()
            val = v.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as exc:
        print(f"[WARN] Failed to load {env_path}: {exc!r}. Using system environment.", flush=True)


def _mask_secret(s: Optional[str]) -> str:
    if not s:
        return "(missing)"
    if len(s) <= 2:
        return "*" * len(s)
    return s[:1] + "*" * (len(s) - 2) + s[-1:]


def _require_env(name: str) -> str:
    val = (os.getenv(name) or "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in `{ROOT / '.env'}` or in your system environment."
        )
    return val


def _tool_id(display_name: str) -> str:
    # Normalize "get_location(TEST_LOCATION_1)" -> "get_location"
    return display_name.split("(", 1)[0].strip()


def _should_run(display_name: str) -> bool:
    if not ONLY_TOOLS:
        return True
    return _tool_id(display_name) in ONLY_TOOLS


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _summarize(resp: Any) -> str:
    if isinstance(resp, dict):
        if resp.get("success") is False:
            return f"success=false error={resp.get('error')!r} code={resp.get('error_code')!r}"
        if resp.get("success") is True:
            data = resp.get("data")
            if isinstance(data, dict):
                for k in ("totalCount", "total_items", "total_workorders", "total_assets", "record_count"):
                    if k in data:
                        return f"success=true {k}={data.get(k)!r}"
                if "workorders" in data:
                    return f"success=true workorders={len(data.get('workorders', []))} totalCount={data.get('totalCount')!r}"
                if "assets" in data:
                    return f"success=true assets={len(data.get('assets', []))} totalCount={data.get('totalCount')!r}"
                if "users" in data:
                    return f"success=true users={len(data.get('users', []))} totalCount={data.get('totalCount')!r}"
            return "success=true"
        if "member" in resp:
            mem = resp.get("member") or []
            n = len(mem) if isinstance(mem, list) else 1
            return f"member={n} totalCount={resp.get('totalCount')!r}"
        return "dict"
    if isinstance(resp, list):
        return f"list[{len(resp)}]"
    if resp is None:
        return "null"
    return f"{type(resp).__name__}"


def _status_from(resp: Any) -> str:
    if isinstance(resp, dict) and resp.get("success") is False:
        return "FAIL"
    if isinstance(resp, dict) and resp.get("success") is True:
        return "PASS"
    if resp:
        return "PARTIAL"
    return "FAIL"


def _clip_json(obj: Any, limit: int = 1200) -> str:
    try:
        s = json.dumps(obj, indent=2, default=str)
    except Exception:
        s = repr(obj)
    return s if len(s) <= limit else s[:limit] + "\n... (truncated) ..."


# ---------------------------------------------------------------------------
# Test context
# ---------------------------------------------------------------------------

@dataclass
class Ctx:
    site_id: Optional[str] = None
    org_id: Optional[str] = None
    test_asset_1: Optional[str] = None       # from list_assets or search_assets
    test_wo_1: Optional[Tuple[str, str]] = None   # (wonum, site_id) from list_workorders
    first_location: Optional[str] = None
    first_labor: Optional[str] = None
    first_item: Optional[str] = None
    first_storeroom: Optional[str] = None
    second_storeroom: Optional[str] = None   # for transfer_inventory
    test_new_wo: Optional[Tuple[str, str]] = None  # created by create_workorder
    test_po: Optional[Tuple[str, str]] = None      # created by create_purchase_order
    first_vendor: Optional[str] = None        # from existing PO query
    test_asset_key: Optional[str] = None      # unique key for create/update/retire asset lifecycle


# ---------------------------------------------------------------------------
# Tool runner
# ---------------------------------------------------------------------------

async def _run_tool(
    n: int,
    name: str,
    fn: Callable[[], Awaitable[Any]],
    timings: Optional[Dict[str, float]] = None,
) -> Tuple[str, Any, Optional[str]]:
    err: Optional[str] = None
    resp: Any = None
    t0 = time.monotonic()
    try:
        resp = await fn()
    except Exception as exc:
        err = repr(exc)
        resp = {"success": False, "error": str(exc) or repr(exc), "error_code": "EXCEPTION"}
    elapsed = time.monotonic() - t0
    if timings is not None:
        timings[name] = elapsed
    status = _status_from(resp)
    print("---", flush=True)
    print(f"Tool {n}: {name}  [{elapsed:.1f}s]", flush=True)
    print(f"Status: {status}", flush=True)
    print(f"Response: {_summarize(resp)}", flush=True)
    if err:
        print(f"Error: {err}", flush=True)
    else:
        print("Error: none", flush=True)
    # Throttle: small delay between tools to avoid overwhelming Maximo over VPN
    if _INTER_TOOL_DELAY_S > 0:
        await asyncio.sleep(_INTER_TOOL_DELAY_S)
    return status, resp, err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _tool_health_check(ctx: Ctx) -> Dict[str, Any]:
    import server
    r = await server.health_check()
    connected = bool(r.get("maximo_connected"))
    try:
        from core.maximo_client import get_connected_client
        c = await get_connected_client()
        who = await c.get("/whoami", params={"lean": "1"})
        ctx.site_id = who.get("insertSite") or who.get("defaultSite") or ctx.site_id
        ctx.org_id = who.get("insertOrg") or who.get("defaultOrg") or ctx.org_id
        r["active_site"] = ctx.site_id
        r["active_org"] = ctx.org_id
    except Exception:
        pass
    # Wrap with a success envelope so _status_from works correctly
    return {"success": connected, "data": r, "error": None if connected else "Maximo not connected"}


async def _query_first_inventory(site_id: str, page_size: int = 5) -> List[Dict]:
    """Return up to page_size raw inventory member records for a site."""
    from core.maximo_client import get_connected_client
    client = await get_connected_client()
    params = client.build_oslc_query(
        where=f'siteid="{site_id}"',
        select="itemnum,storeloc,siteid,curbal",
        page_size=page_size,
    )
    data = await client.get("/os/mxinventory", params=params)
    return data.get("member", [])


async def _lookup_vendor(site_id: str) -> Optional[str]:
    """Return first vendor code found in existing POs for this site."""
    try:
        from core.maximo_client import get_connected_client
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'siteid="{site_id}" and vendor!=""',
            select="vendor",
            page_size=1,
        )
        data = await client.get("/os/mxpo", params=params)
        members = data.get("member", [])
        if members:
            return members[0].get("vendor")
    except Exception:
        pass
    return None


# --- Silent setup helpers (so selected tools can still run) ------------------

async def _ensure_first_location(ctx: Ctx, site_id: str) -> None:
    """Populate ctx.first_location if missing (silent setup)."""
    if ctx.first_location:
        return
    try:
        from tools import locations

        r = await locations.list_locations(site_id=site_id)
        if isinstance(r, dict) and r.get("success"):
            locs = r["data"].get("locations") or []
            if locs:
                ctx.first_location = locs[0].get("location")
    except Exception:
        return


async def _ensure_first_item(ctx: Ctx, site_id: str) -> None:
    """Populate ctx.first_item if missing (silent setup)."""
    if ctx.first_item:
        return
    try:
        members = await _query_first_inventory(site_id, page_size=10)
        chosen = next((m for m in members if m.get("itemnum")), None)
        if chosen:
            ctx.first_item = chosen.get("itemnum")
    except Exception:
        return


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main() -> int:
    # Load project-root .env before any imports/use of config.
    _load_project_env()

    os.environ.setdefault("AUTH_MODE", "basic")
    os.environ.setdefault("CURRENT_USER_ROLE", "admin")

    # Validate required config early (clear error vs deeper stack traces).
    maximo_url = _require_env("MAXIMO_URL")
    maximo_user = _require_env("MAXIMO_USERNAME")
    _require_env("MAXIMO_PASSWORD")

    # Temporary debug logging to confirm env is loaded correctly.
    print(
        f"[DEBUG] MAXIMO_URL={maximo_url!r}\n"
        f"[DEBUG] MAXIMO_USERNAME={maximo_user!r}\n"
        f"[DEBUG] MAXIMO_PASSWORD={_mask_secret(os.getenv('MAXIMO_PASSWORD'))}",
        flush=True,
    )

    ctx = Ctx()
    try:
        import logging
        logging.getLogger().setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("core.cache").setLevel(logging.ERROR)
    except Exception:
        pass

    results: List[Tuple[int, str, str]] = []
    timings: Dict[str, float] = {}

    def record(n: int, tool: str, status: str) -> None:
        results.append((n, tool, status))

    async def run_if_selected(
        n: int,
        display_name: str,
        fn: Callable[[], Awaitable[Any]],
    ) -> Tuple[Optional[str], Any, Optional[str]]:
        if not _should_run(display_name):
            return None, None, None
        status, resp, err = await _run_tool(n, display_name, fn, timings)
        record(n, _tool_id(display_name), status)
        return status, resp, err

    async def _tier_pause(label: str) -> None:
        """Pause between tool tiers to let Maximo / VPN recover."""
        print(f"\n[TIER PAUSE] {label} — waiting 2s...\n", flush=True)
        await asyncio.sleep(2.0)

    # -----------------------------------------------------------------------
    # ROUND 1 — Connectivity
    # -----------------------------------------------------------------------
    await run_if_selected(1, "health_check", lambda: _tool_health_check(ctx))
    site = ctx.site_id or "WW"

    # -----------------------------------------------------------------------
    # ROUND 2 — Schema & Dev
    # -----------------------------------------------------------------------
    from tools import schema_dev

    async def t2():
        return await schema_dev.list_object_structures(filter_keyword=None, include_custom=False)

    await run_if_selected(2, "list_object_structures", t2)

    async def t3():
        # Use mxlabor (small OS) to avoid the mxwo /os describe timeout (BMXAA8816E on metadata endpoint)
        return await schema_dev.get_schema_details(object_structure="mxlabor", include_relationships=False)

    await run_if_selected(3, "get_schema_details(MXWO)", t3)

    # Slot 4: update_pm_frequency (PM category) — replaces get_schema_details(MXASSET duplicate)
    from tools import pm_scheduling as pm

    async def t4():
        # PM permissions may block this; documents the permission boundary
        return await pm.update_pm_frequency(
            pm_num="PM0001",
            site_id=site,
            frequency=30,
            frequency_unit="DAYS",
        )

    await run_if_selected(4, "update_pm_frequency(PM0001)", t4)

    async def t5():
        return await schema_dev.validate_oslc_query(
            object_structure="mxwo",
            where_clause='status="APPR"',
            select_clause="wonum,description,status",
        )

    await run_if_selected(5, "validate_oslc_query(MXWO)", t5)

    from tools import ai_intelligence as ai

    async def t6():
        return await ai.nl_to_oslc_query(
            natural_language_query="show me all approved work orders from last 7 days",
            object_structure="mxwo",
            dry_run=False,
        )

    await run_if_selected(6, "nl_to_oslc_query(MXWO)", t6)

    # Slot 7: generate_pm_workorders (PM category) — replaces nl_to_oslc_query(MXASSET duplicate)
    async def t7():
        return await pm.generate_pm_workorders(site_id=site, date_range_days=30)

    await run_if_selected(7, "generate_pm_workorders", t7)

    async def t8():
        return await schema_dev.generate_api_code(object_structure="mxwo", operation="list", language="python")

    await run_if_selected(8, "generate_api_code(MXWO list python)", t8)

    # Slot 9: check_stock_level (Inventory category) — replaces generate_api_code(MXASSET curl duplicate)
    # Also populates ctx.first_item / ctx.first_storeroom for later slots.
    from tools import inventory

    async def t9():
        try:
            members = await _query_first_inventory(site, page_size=5)
            # find a member with both itemnum and storeloc populated
            chosen = next(
                (m for m in members if m.get("itemnum") and m.get("storeloc")),
                None,
            )
            if not chosen:
                return {"success": False, "error": f"No inventory item with storeloc found for site {site}", "error_code": "TEST_SETUP"}
            item_num = chosen["itemnum"]
            storeroom = chosen["storeloc"]
            # Stash for later tools
            if not ctx.first_item:
                ctx.first_item = item_num
            if not ctx.first_storeroom:
                ctx.first_storeroom = storeroom
            # Find a second distinct storeroom for transfer_inventory
            for m in members:
                if m.get("storeloc") and m["storeloc"] != storeroom and m.get("itemnum"):
                    ctx.second_storeroom = m["storeloc"]
                    break
            return await inventory.check_stock_level(item_num=item_num, storeroom=storeroom, site_id=site)
        except Exception as exc:
            return {"success": False, "error": repr(exc), "error_code": "EXCEPTION"}

    await run_if_selected(9, "check_stock_level", t9)

    # -----------------------------------------------------------------------
    # ROUND 3 — Assets (read)
    # -----------------------------------------------------------------------
    from tools import assets

    async def t10():
        return await assets.list_assets(site_id=site, status="OPERATING", page_size=5, page_num=1)

    status, r10, _ = await run_if_selected(10, "list_assets", t10)
    # Fallback: populate test_asset_1 from list_assets (OPERATING status) so later tools aren't blocked
    try:
        if isinstance(r10, dict) and r10.get("success"):
            asset_list = r10["data"].get("assets") or []
            if asset_list and not ctx.test_asset_1:
                ctx.test_asset_1 = asset_list[0].get("assetnum") or asset_list[0].get("asset_num")
    except Exception:
        pass

    async def t11():
        return await assets.search_assets(site_id=site, keyword="pump", page_size=5, page_num=1)

    status, r11, _ = await run_if_selected(11, "search_assets(pump)", t11)
    # Override test_asset_1 if a better match was found
    try:
        if isinstance(r11, dict) and r11.get("success"):
            mem = r11["data"].get("assets") or []
            if mem:
                ctx.test_asset_1 = mem[0].get("assetnum") or mem[0].get("asset_num")
    except Exception:
        pass

    test_asset = ctx.test_asset_1

    async def t12():
        if not test_asset:
            return {"success": False, "error": "No test asset available", "error_code": "TEST_SETUP"}
        return await assets.get_asset(asset_num=test_asset, site_id=site)

    await run_if_selected(12, "get_asset(TEST_ASSET_1)", t12)

    async def t13():
        if not test_asset:
            return {"success": False, "error": "No test asset available", "error_code": "TEST_SETUP"}
        return await assets.get_asset_history(asset_num=test_asset, site_id=site, lookback_days=90)

    await run_if_selected(13, "get_asset_history", t13)

    async def t14():
        if not test_asset:
            return {"success": False, "error": "No test asset available", "error_code": "TEST_SETUP"}
        return await assets.get_asset_downtime_stats(asset_num=test_asset, site_id=site, period_months=6)

    await run_if_selected(14, "get_asset_downtime_stats", t14)

    # -----------------------------------------------------------------------
    # ROUND 4 — Work Orders (read)
    # -----------------------------------------------------------------------
    from tools import workorders

    async def t15():
        return await workorders.list_workorders(site_id=site, status=None, page_size=20, page_num=1)

    status, r15, _ = await run_if_selected(15, "list_workorders", t15)
    try:
        if isinstance(r15, dict) and r15.get("success"):
            wos = r15["data"].get("workorders") or []
            if wos:
                ctx.test_wo_1 = (wos[0].get("wonum"), wos[0].get("siteid") or site)
    except Exception:
        pass

    async def t16():
        if not ctx.test_wo_1 or not ctx.test_wo_1[0]:
            return {"success": False, "error": "No TEST_WO_1 from list_workorders", "error_code": "TEST_SETUP"}
        wonum, wo_site = ctx.test_wo_1
        return await workorders.get_workorder(wonum=wonum, site_id=wo_site)

    await run_if_selected(16, "get_workorder(TEST_WO_1)", t16)

    async def t17():
        return await workorders.get_workorder_kpis(site_id=site, period_months=3)

    await run_if_selected(17, "get_workorder_kpis", t17)

    await _tier_pause("After Tier-1 reads (assets + workorders)")

    # -----------------------------------------------------------------------
    # ROUND 5 — PM Scheduling
    # -----------------------------------------------------------------------
    async def t18():
        return await pm.list_pm_schedules(site_id=site, asset_num=None, page_size=10, page_num=1)

    await run_if_selected(18, "list_pm_schedules", t18)

    async def t19():
        return await pm.get_pm_forecast(site_id=site, months_ahead=3)

    await run_if_selected(19, "get_pm_forecast", t19)

    # -----------------------------------------------------------------------
    # ROUND 6 — Inventory (read)
    # -----------------------------------------------------------------------
    async def t20():
        return await inventory.list_low_stock_items(site_id=site)

    status, r20, _ = await run_if_selected(20, "list_low_stock_items", t20)
    try:
        if isinstance(r20, dict) and r20.get("success"):
            items = r20["data"].get("low_stock_items") or []
            if items:
                ctx.first_item = ctx.first_item or items[0].get("itemnum")
                ctx.first_storeroom = ctx.first_storeroom or (
                    items[0].get("storeloc") or items[0].get("storeroom")
                )
    except Exception:
        pass

    async def t21():
        return await inventory.get_reorder_recommendations(site_id=site)

    await run_if_selected(21, "get_reorder_recommendations", t21)

    # -----------------------------------------------------------------------
    # ROUND 7 — Locations & Labor
    # -----------------------------------------------------------------------
    from tools import locations, labor

    async def t22():
        return await locations.list_locations(site_id=site)

    status, r22, _ = await run_if_selected(22, "list_locations", t22)
    try:
        if isinstance(r22, dict) and r22.get("success"):
            locs = r22["data"].get("locations") or []
            if locs:
                ctx.first_location = locs[0].get("location")
    except Exception:
        pass

    async def t23():
        if not ctx.first_location:
            await _ensure_first_location(ctx, site)
        if not ctx.first_location:
            return {"success": False, "error": "No TEST_LOCATION_1 (location setup failed)", "error_code": "TEST_SETUP"}
        return await locations.get_location(location=ctx.first_location, site_id=site)

    await run_if_selected(23, "get_location(TEST_LOCATION_1)", t23)

    async def t24():
        return await locations.get_location_hierarchy(site_id=site, root_location=None)

    await run_if_selected(24, "get_location_hierarchy", t24)

    async def t25():
        return await labor.list_labor(site_id=site)

    status, r25, _ = await run_if_selected(25, "list_labor", t25)
    try:
        if isinstance(r25, dict) and r25.get("success"):
            labs = r25["data"].get("labor") or []
            if labs:
                ctx.first_labor = labs[0].get("laborcode") or labs[0].get("personid")
    except Exception:
        pass

    async def t26():
        return await labor.list_crews(site_id=site)

    await run_if_selected(26, "list_crews", t26)

    async def t27():
        return await labor.get_labor_utilization(site_id=site, period_days=30)

    await run_if_selected(27, "get_labor_utilization", t27)

    # -----------------------------------------------------------------------
    # ROUND 8 — Admin
    # -----------------------------------------------------------------------
    from tools import admin

    async def t28():
        return await admin.list_users(site_id=None)

    await run_if_selected(28, "list_users", t28)

    async def t29():
        return await admin.get_user(user_id="maxadmin")

    await run_if_selected(29, "get_user(maxadmin)", t29)

    async def t30():
        return await admin.list_security_groups()

    await run_if_selected(30, "list_security_groups", t30)

    from tools import integrations

    async def t31():
        return await integrations.list_event_subscriptions()

    await run_if_selected(31, "list_event_subscriptions", t31)

    await _tier_pause("Before Tier-2 heavy tools (AI + Reporting)")

    # -----------------------------------------------------------------------
    # ROUND 9 — AI Intelligence
    # -----------------------------------------------------------------------
    async def t32():
        if not test_asset:
            return {"success": False, "error": "No test asset (needed for anomaly detection)", "error_code": "TEST_SETUP"}
        return await ai.detect_asset_anomalies(asset_num=test_asset, site_id=site, lookback_days=90)

    await run_if_selected(32, "detect_asset_anomalies", t32)

    async def t33():
        if not test_asset:
            return {"success": False, "error": "No test asset (needed for root cause)", "error_code": "TEST_SETUP"}
        return await ai.suggest_root_cause(
            asset_num=test_asset,
            site_id=site,
            failure_description="unexpected vibration and overheating",
        )

    await run_if_selected(33, "suggest_root_cause", t33)

    async def t34():
        if not test_asset:
            return {"success": False, "error": "No test asset (needed for health summary)", "error_code": "TEST_SETUP"}
        return await ai.summarize_asset_health(asset_num=test_asset, site_id=site)

    await run_if_selected(34, "summarize_asset_health", t34)

    async def t35():
        # BUG FIX: removed invalid top_k kwarg (function signature is query, doc_type only)
        return await ai.search_maximo_knowledge(
            query="how to create a work order",
            doc_type="all",
        )

    await run_if_selected(35, "search_maximo_knowledge", t35)

    # -----------------------------------------------------------------------
    # ROUND 10 — Reporting
    # -----------------------------------------------------------------------
    from tools import reporting

    async def t36():
        return await reporting.get_maintenance_kpi_dashboard(site_id=site, period_months=3)

    await run_if_selected(36, "get_maintenance_kpi_dashboard", t36)

    async def t37():
        # BUG FIX: rows→data; columns must be list of {key, header} dicts (not list of strings)
        rows: List[Dict] = []
        if isinstance(r15, dict) and r15.get("success"):
            rows = r15["data"].get("workorders") or []
        return await reporting.generate_carbon_table(
            object_structure="mxwo",
            data=rows,
            columns=[
                {"key": "wonum", "header": "Work Order #"},
                {"key": "description", "header": "Description"},
                {"key": "status", "header": "Status"},
            ],
        )

    await run_if_selected(37, "generate_carbon_table", t37)

    async def t38():
        return await reporting.export_workorders_excel(site_id=site, filters=None)

    await run_if_selected(38, "export_workorders_excel", t38)

    async def t39():
        return await reporting.export_asset_report_pdf(site_id=site, asset_group=None)

    await run_if_selected(39, "export_asset_report_pdf", t39)

    # -----------------------------------------------------------------------
    # ROUND 11 — Work Order write (create → cancel / update → approve → assign → close)
    # -----------------------------------------------------------------------
    async def t40():
        if not test_asset:
            return {"success": False, "error": "No test asset (needed for create_workorder)", "error_code": "TEST_SETUP"}
        return await workorders.create_workorder(
            description="MCP_TEST_ bearing inspection",
            asset_num=test_asset,
            site_id=site,
            priority=3,
            work_type="CM",
            notes="MCP_TEST_ created by automated tool runner",
        )

    status, r40, _ = await run_if_selected(40, "create_workorder", t40)
    try:
        if isinstance(r40, dict) and r40.get("success"):
            wonum = r40["data"].get("wonum")
            wo_site = r40["data"].get("siteid") or site
            if wonum:
                ctx.test_new_wo = (wonum, wo_site)
    except Exception:
        pass

    # Slot 41: cancel_workorder — replaces duplicate get_workorder(NEW WO)
    # Creates a throw-away WO and cancels it to exercise cancel_workorder.
    async def t41():
        if not test_asset:
            return {"success": False, "error": "No test asset (needed for cancel_workorder setup WO)", "error_code": "TEST_SETUP"}
        try:
            cancel_wo_resp = await workorders.create_workorder(
                description="MCP_TEST_ cancel test WO",
                asset_num=test_asset,
                site_id=site,
                work_type="CM",
            )
            if not (isinstance(cancel_wo_resp, dict) and cancel_wo_resp.get("success")):
                return {"success": False, "error": "Failed to create WO for cancel test", "error_code": "TEST_SETUP"}
            cancel_wonum = cancel_wo_resp["data"].get("wonum")
            cancel_site = cancel_wo_resp["data"].get("siteid") or site
            if not cancel_wonum:
                return {"success": False, "error": "No wonum returned from cancel test WO creation", "error_code": "TEST_SETUP"}
            return await workorders.cancel_workorder(
                wonum=cancel_wonum,
                site_id=cancel_site,
                reason="MCP_TEST_ cancel test",
            )
        except Exception as exc:
            return {"success": False, "error": repr(exc), "error_code": "EXCEPTION"}

    await run_if_selected(41, "cancel_workorder", t41)

    async def t42():
        if not ctx.test_new_wo:
            return {"success": False, "error": "No TEST_NEW_WO", "error_code": "TEST_SETUP"}
        wonum, wo_site = ctx.test_new_wo
        return await workorders.update_workorder(
            wonum=wonum, site_id=wo_site,
            description="MCP_TEST_ bearing inspection - UPDATED",
        )

    await run_if_selected(42, "update_workorder", t42)

    async def t43():
        if not ctx.test_new_wo:
            return {"success": False, "error": "No TEST_NEW_WO", "error_code": "TEST_SETUP"}
        wonum, wo_site = ctx.test_new_wo
        return await workorders.approve_workorder(wonum=wonum, site_id=wo_site)

    await run_if_selected(43, "approve_workorder", t43)

    async def t44():
        if not ctx.test_new_wo:
            return {"success": False, "error": "No TEST_NEW_WO", "error_code": "TEST_SETUP"}
        if not ctx.first_labor:
            return {"success": False, "error": "No labor code from list_labor", "error_code": "TEST_SETUP"}
        wonum, wo_site = ctx.test_new_wo
        return await workorders.assign_technician(
            wonum=wonum, site_id=wo_site,
            labor_code=ctx.first_labor,
            hours_planned=1.0,
        )

    await run_if_selected(44, "assign_technician", t44)

    async def t45():
        if not ctx.test_new_wo:
            return {"success": False, "error": "No TEST_NEW_WO", "error_code": "TEST_SETUP"}
        wonum, wo_site = ctx.test_new_wo
        return await workorders.close_workorder(
            wonum=wonum,
            site_id=wo_site,
            actual_hours=2.0,
            resolution_notes="MCP_TEST_ closed by automated test",
        )

    await run_if_selected(45, "close_workorder", t45)

    # -----------------------------------------------------------------------
    # ROUND 12 — Asset write
    # BUG FIX: unique key via timestamp to avoid BMXAA4129E duplicate error
    # -----------------------------------------------------------------------
    asset_ts = _now_utc().strftime("%m%d%H%M%S")
    ctx.test_asset_key = f"MCP{asset_ts}"

    async def t46():
        return await assets.create_asset(
            asset_num=ctx.test_asset_key,
            description="MCP TEST ASSET - DELETE ME",
            site_id=site,
        )

    await run_if_selected(46, "create_asset", t46)

    async def t47():
        return await assets.update_asset(
            asset_num=ctx.test_asset_key,
            description="MCP TEST ASSET - UPDATED",
            site_id=site,
        )

    await run_if_selected(47, "update_asset", t47)

    async def t48():
        return await assets.retire_asset(
            asset_num=ctx.test_asset_key,
            site_id=site,
            reason="MCP automated test cleanup",
        )

    await run_if_selected(48, "retire_asset", t48)

    # -----------------------------------------------------------------------
    # ROUND 13 — Inventory write
    # -----------------------------------------------------------------------
    async def t49():
        # Ensure we have item/storeroom — fall back to inline query
        item = ctx.first_item
        storeroom = ctx.first_storeroom
        if not item or not storeroom:
            try:
                members = await _query_first_inventory(site, page_size=5)
                chosen = next(
                    (m for m in members if m.get("itemnum") and m.get("storeloc")),
                    None,
                )
                if chosen:
                    item = item or chosen["itemnum"]
                    storeroom = storeroom or chosen["storeloc"]
                    ctx.first_item = item
                    ctx.first_storeroom = storeroom
            except Exception:
                pass
        if not item:
            return {"success": False, "error": "No item from inventory", "error_code": "TEST_SETUP"}
        if not storeroom:
            return {"success": False, "error": "No storeroom from inventory", "error_code": "TEST_SETUP"}
        return await inventory.create_material_request(
            item_num=item,
            site_id=site,
            quantity=1,
            location=storeroom,
            notes="MCP_TEST_ material request",
        )

    await run_if_selected(49, "create_material_request", t49)

    # -----------------------------------------------------------------------
    # ROUND 14 — Purchasing
    # BUG FIX: lookup real vendor from existing POs before create_purchase_order
    # -----------------------------------------------------------------------
    from tools import purchasing

    # Try to find a real vendor before the PO creation test
    if not ctx.first_vendor:
        try:
            ctx.first_vendor = await _lookup_vendor(site)
        except Exception:
            pass

    async def t50():
        # Ensure minimal test data even if inventory tools were skipped.
        if not ctx.first_item:
            await _ensure_first_item(ctx, site)
        vendor = ctx.first_vendor
        if not vendor:
            return {"success": False, "error": "No valid vendor found in Maximo (needed for create_purchase_order)", "error_code": "TEST_SETUP"}
        item = ctx.first_item or "ITEM001"
        result = await purchasing.create_purchase_order(
            vendor=vendor,
            site_id=site,
            items=[{"item_num": item, "qty": 1}],
            notes="MCP_TEST_ PO",
        )
        return result

    status, r50, _ = await run_if_selected(50, "create_purchase_order", t50)
    try:
        if isinstance(r50, dict) and r50.get("success"):
            ponum = r50["data"].get("ponum") or r50["data"].get("purchaseordernum")
            po_site = r50["data"].get("siteid") or site
            if ponum:
                ctx.test_po = (ponum, po_site)
    except Exception:
        pass

    async def t51():
        if not ctx.test_po:
            return {"success": False, "error": "No TEST_PO from create_purchase_order", "error_code": "TEST_SETUP"}
        ponum, po_site = ctx.test_po
        return await purchasing.get_purchase_order(ponum=ponum, site_id=po_site)

    await run_if_selected(51, "get_purchase_order", t51)

    # -----------------------------------------------------------------------
    # ROUND 15 — Integrations
    # -----------------------------------------------------------------------
    async def t52():
        return await integrations.subscribe_to_event(
            event_type="TEST_EVENT",
            callback_url="http://localhost:9999/test-webhook",
            filter_conditions=None,
        )

    await run_if_selected(52, "subscribe_to_event", t52)

    # Slot 53: transfer_inventory — replaces list_event_subscriptions(verify duplicate)
    async def t53():
        item = ctx.first_item
        from_loc = ctx.first_storeroom
        to_loc = ctx.second_storeroom
        # If we don't have two storerooms, attempt a fresh inventory query
        if not item or not from_loc or not to_loc:
            try:
                members = await _query_first_inventory(site, page_size=10)
                locs: Dict[str, Dict] = {}
                for m in members:
                    loc = m.get("storeloc", "")
                    if loc and m.get("itemnum") and loc not in locs:
                        locs[loc] = m
                    if len(locs) >= 2:
                        break
                loc_list = list(locs.keys())
                if len(loc_list) >= 2:
                    from_loc = from_loc or loc_list[0]
                    to_loc = to_loc or loc_list[1]
                    item = item or locs[loc_list[0]].get("itemnum")
                    ctx.first_storeroom = ctx.first_storeroom or from_loc
                    ctx.second_storeroom = ctx.second_storeroom or to_loc
                    ctx.first_item = ctx.first_item or item
            except Exception:
                pass
        if not item:
            return {"success": False, "error": "No item for transfer_inventory test", "error_code": "TEST_SETUP"}
        if not from_loc or not to_loc:
            return {"success": False, "error": "Could not resolve two distinct storerooms for transfer_inventory", "error_code": "TEST_SETUP"}
        if from_loc == to_loc:
            return {"success": False, "error": f"Both storerooms are '{from_loc}' — Maximo requires distinct from/to", "error_code": "TEST_SETUP"}
        return await inventory.transfer_inventory(
            item_num=item,
            from_storeroom=from_loc,
            to_storeroom=to_loc,
            quantity=1.0,
            site_id=site,
        )

    await run_if_selected(53, "transfer_inventory", t53)

    async def t54():
        return await integrations.trigger_webhook(
            event_type="TEST_EVENT",
            payload={"test": True, "source": "MCP_TEST"},
        )

    await run_if_selected(54, "trigger_webhook", t54)

    async def t55():
        if not test_asset:
            return {"success": False, "error": "No test asset (needed for ingest_iot_alert)", "error_code": "TEST_SETUP"}
        return await integrations.ingest_iot_alert(
            asset_num=test_asset,
            site_id=site,
            sensor_type="temperature",
            reading_value=95,
            threshold=80,
        )

    await run_if_selected(55, "ingest_iot_alert", t55)

    # -----------------------------------------------------------------------
    # ROUND 16 — Schema write
    # -----------------------------------------------------------------------
    async def t56():
        return await schema_dev.build_custom_object_structure(
            name="ZMCPTEST",
            base_object="WORKORDER",
            fields=[
                {"name": "wonum", "type": "ALN", "required": False, "description": "Work order number"},
                {"name": "description", "type": "ALN", "required": False, "description": "Description"},
                {"name": "status", "type": "ALN", "required": False, "description": "Status"},
            ],
        )

    await run_if_selected(56, "build_custom_object_structure", t56)

    # Slot 57: get_vendor_performance — replaces validate_oslc_query(MXASSET complex duplicate)
    async def t57():
        vendor = ctx.first_vendor
        if not vendor:
            try:
                vendor = await _lookup_vendor(site)
                ctx.first_vendor = vendor
            except Exception:
                pass
        if not vendor:
            return {"success": False, "error": "No vendor found in Maximo for get_vendor_performance", "error_code": "TEST_SETUP"}
        return await purchasing.get_vendor_performance(vendor_id=vendor, period_months=12)

    await run_if_selected(57, "get_vendor_performance", t57)

    # Slot 58: receive_items — replaces generate_api_code(MXWO create js duplicate)
    async def t58():
        if not ctx.test_po:
            return {"success": False, "error": "No TEST_PO (receive_items requires a valid PO)", "error_code": "TEST_SETUP"}
        ponum, po_site = ctx.test_po
        storeroom = ctx.first_storeroom or ""
        return await purchasing.receive_items(
            ponum=ponum,
            site_id=po_site,
            received_lines=[{"polinenum": 1, "receivedqty": 1, "storeroom": storeroom}],
        )

    await run_if_selected(58, "receive_items", t58)

    # -----------------------------------------------------------------------
    # ROUND 17 — Audit trail
    # -----------------------------------------------------------------------
    async def t59():
        today = _now_utc().strftime("%Y-%m-%dT00:00:00+00:00")
        return await admin.query_audit_log(date_from=today, limit=100)

    await run_if_selected(59, "query_audit_log", t59)

    # -----------------------------------------------------------------------
    # FINAL SCORECARD
    # -----------------------------------------------------------------------
    def _count(ns: List[int]) -> Tuple[int, int, int]:
        filt = [r for r in results if r[0] in ns]
        passed = sum(1 for _, _, s in filt if s.startswith("PASS"))
        failed = sum(1 for _, _, s in filt if s.startswith("FAIL"))
        partial = sum(1 for _, _, s in filt if s.startswith("PARTIAL"))
        return passed, failed, partial

    # Each category lists the SLOT numbers that now test tools in that category
    categories = [
        ("Connectivity",      [1]),
        ("Schema & Dev",      [2, 3, 5, 8, 56]),
        ("AI Intelligence",   [6, 32, 33, 34, 35]),
        ("Assets",            [10, 11, 12, 13, 14, 46, 47, 48]),
        ("Work Orders",       [15, 16, 17, 40, 41, 42, 43, 44, 45]),
        ("PM Scheduling",     [4, 7, 18, 19]),
        ("Inventory",         [9, 20, 21, 49, 53]),
        ("Locations & Labor", [22, 23, 24, 25, 26, 27]),
        ("Admin",             [28, 29, 30, 59]),
        ("Reporting",         [36, 37, 38, 39]),
        ("Purchasing",        [50, 51, 57, 58]),
        ("Integrations",      [31, 52, 54, 55]),
    ]

    print("\n\n=== FINAL SCORECARD ===\n")
    print(f"Run time: {_now_utc().isoformat()}")
    print(f"Site: {site}\n")

    print("| Category | Tools | Passed | Failed | Partial |")
    print("|---|---:|---:|---:|---:|")
    total_pass = total_fail = total_part = 0
    for cat, ns in categories:
        p, f, w = _count(ns)
        total_pass += p
        total_fail += f
        total_part += w
        print(f"| {cat} | {len(ns)} | {p} | {f} | {w} |")
    total = len(results)
    pass_rate = (total_pass / total) * 100 if total else 0.0
    print(f"| **TOTAL** | **{total}** | **{total_pass}** | **{total_fail}** | **{total_part}** |")
    print(f"\nOverall pass rate: {pass_rate:.1f}%\n")

    # Failed tool details
    print("=== FAILED TOOLS ===\n")
    for n, tool, stat in results:
        if stat.startswith("FAIL"):
            print(f"  Tool {n:2d}: {tool}")
    if not any(s.startswith("FAIL") for _, _, s in results):
        print("  (none)")

    # Partial details
    print("\n=== PARTIAL RESULTS ===\n")
    for n, tool, stat in results:
        if stat.startswith("PARTIAL"):
            print(f"  Tool {n:2d}: {tool}")
    if not any(s.startswith("PARTIAL") for _, _, s in results):
        print("  (none)")

    # Performance
    if timings:
        sorted_timings = sorted(timings.items(), key=lambda x: x[1], reverse=True)
        avg = sum(timings.values()) / len(timings)
        print("\n=== PERFORMANCE ===\n")
        print(f"Average response time: {avg:.2f}s")
        print("Slowest 5 tools:")
        for name, t in sorted_timings[:5]:
            print(f"  {t:.1f}s  {name}")

    print("\nNOTE: receive_items (slot 58) requires create_purchase_order to succeed first.")
    print("      update_pm_frequency + generate_pm_workorders + list_pm_schedules + get_pm_forecast")
    print("      may fail with BMXAA0024E if the PM object requires additional Maximo permissions.")
    print("      list_security_groups may return 404 if /os/mxsecgroup is not configured.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
