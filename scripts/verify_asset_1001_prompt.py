"""
scripts/verify_asset_1001_prompt.py

Ground-truth verification for the user prompt:
    "Asset 1001 isn't behaving. Pull its details, its recent work order
     history, and check what spare parts we have in inventory."

Calls the exact tools an LLM would use for that prompt and prints structured
output you can diff against what Claude Desktop / Cursor / Code shows in chat.

Usage:
    MAXIMO_URL=... MAXIMO_HOST=... MAXIMO_USERNAME=... MAXIMO_PASSWORD=... \\
        CURRENT_USER_ROLE=admin \\
        python scripts/verify_asset_1001_prompt.py

The default target is asset 1001 in site BEDFORD; override via env if you need:
    ASSET_NUM=PUMP-7 SITE_ID=PLANT-A python scripts/verify_asset_1001_prompt.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


ASSET_NUM = os.environ.get("ASSET_NUM", "1001")
SITE_ID = os.environ.get("SITE_ID", "BEDFORD")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "365"))


def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _summary(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Strip noisy nested data so the diff against chat output is readable."""
    if not isinstance(envelope, dict):
        return {"raw": envelope}
    if not envelope.get("success"):
        return {"success": False, "error": envelope.get("error"), "error_code": envelope.get("error_code")}
    data = envelope.get("data", {})
    return {"success": True, "data": data, "metadata": envelope.get("metadata", {})}


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


async def main() -> int:
    print("Verifying prompt against:")
    print(f"  MAXIMO_URL = {os.environ.get('MAXIMO_URL')}")
    print(f"  asset_num  = {ASSET_NUM}")
    print(f"  site_id    = {SITE_ID}")

    from tools import assets, inventory
    from core.maximo_client import get_connected_client

    # Connectivity probe
    try:
        client = await get_connected_client()
        whoami = await client.get("/whoami", params={"lean": "1"})
        print(f"  user       = {whoami.get('userName') or whoami.get('personid', '?')!r}")
    except Exception as exc:
        print(f"  CONNECTIVITY FAILED: {exc!r}")
        return 2

    # ──────────────────────────────────────────────────────────────────────────
    # Step 1 — Asset details
    # The LLM will read "pull its details" and fire get_asset(asset_num, site_id).
    # ──────────────────────────────────────────────────────────────────────────
    _section(f"Step 1 — get_asset(asset_num={ASSET_NUM!r}, site_id={SITE_ID!r})")
    a = await assets.get_asset(ASSET_NUM, SITE_ID)
    if a.get("success"):
        d = a["data"]
        print(_json({
            "assetnum": d.get("assetnum"),
            "description": d.get("description"),
            "siteid": d.get("siteid"),
            "status": d.get("status"),
            "priority": d.get("priority"),
            "assettype": d.get("assettype"),
            "location": d.get("location"),
            "manufacturer": d.get("manufacturer"),
            "vendor": d.get("vendor"),
            "installdate": d.get("installdate"),
            "purchaseprice": d.get("purchaseprice"),
            "sparepart_child_rows": len(d.get("sparepart") or []),
        }))
    else:
        print(_json(a))

    # ──────────────────────────────────────────────────────────────────────────
    # Step 2 — Recent work order history
    # ──────────────────────────────────────────────────────────────────────────
    _section(f"Step 2 — get_asset_history(asset_num={ASSET_NUM!r}, site_id={SITE_ID!r}, lookback_days={LOOKBACK_DAYS})")
    h = await assets.get_asset_history(ASSET_NUM, SITE_ID, LOOKBACK_DAYS)
    if h.get("success"):
        d = h["data"]
        wos = d.get("workorders") or d.get("history") or []
        print(_json({
            "asset_num": d.get("asset_num"),
            "lookback_days": d.get("lookback_days"),
            "totalCount": d.get("totalCount") or len(wos),
            "summary_keys": list(d.keys()),
        }))
        # Print first 5 WOs so the user can spot-check status / dates
        if wos:
            print("\n  First 5 work orders:")
            for wo in wos[:5]:
                print(f"    - wonum={wo.get('wonum')!r:<10} "
                      f"status={wo.get('status')!r:<10} "
                      f"worktype={wo.get('worktype')!r:<8} "
                      f"reportdate={wo.get('reportdate')!r:<35} "
                      f"description={(wo.get('description') or '')[:60]!r}")
        else:
            print("  (no work orders in window)")
    else:
        print(_json(h))

    # ──────────────────────────────────────────────────────────────────────────
    # Step 3a — Spare parts attached to THIS asset (asset.sparepart child)
    # On this Maximo build the OSLC `sparepart` collection is empty, so we
    # surface that fact and pivot to the site-wide fallback below.
    # ──────────────────────────────────────────────────────────────────────────
    _section(f"Step 3a — Spare parts attached to asset {ASSET_NUM} (asset.sparepart child)")
    if a.get("success"):
        spares: List[Dict[str, Any]] = a["data"].get("sparepart") or []
        if spares:
            print(f"  {len(spares)} spare parts on asset record:")
            for s in spares[:10]:
                print(f"    - itemnum={s.get('itemnum')!r:<14} "
                      f"qty={s.get('quantity') or s.get('qty')!r:<6} "
                      f"description={(s.get('description') or '')[:60]!r}")
        else:
            print("  No spare parts attached to the asset record.")
            print("  (Note: on this Maximo build the `mxasset.sparepart` child collection")
            print("   is not populated via OSLC — see memory/asset_sparepart_unavailable.md.)")
    else:
        print("  (asset fetch failed — see Step 1)")

    # ──────────────────────────────────────────────────────────────────────────
    # Step 3b — Site-wide fallbacks the LLM is likely to use:
    # - get_critical_spares_check (needs sparepart, will return data_unavailable)
    # - list_low_stock_items (always works) — gives the practical answer
    # ──────────────────────────────────────────────────────────────────────────
    _section(f"Step 3b — get_critical_spares_check(site_id={SITE_ID!r}, priority_threshold=3)")
    cs = await inventory.get_critical_spares_check(SITE_ID, priority_threshold=3)
    if cs.get("success"):
        d = cs["data"]
        print(_json({
            "critical_asset_count": d.get("critical_asset_count"),
            "data_unavailable": d.get("data_unavailable"),
            "data_unavailable_note": d.get("data_unavailable_note"),
        }))
    else:
        print(_json(cs))

    _section(f"Step 3c — list_low_stock_items(site_id={SITE_ID!r})  [practical fallback]")
    ls = await inventory.list_low_stock_items(SITE_ID)
    if ls.get("success"):
        d = ls["data"]
        items = d.get("low_stock_items") or []
        print(f"  Total low-stock items at {SITE_ID}: {len(items)}")
        if items:
            print("  First 5 items below reorder point:")
            for it in items[:5]:
                print(f"    - itemnum={it.get('itemnum')!r:<10} "
                      f"storeloc={it.get('storeloc')!r:<10} "
                      f"curbal={it.get('curbal')!r:<6} "
                      f"reorderpoint={it.get('reorderpoint')!r:<6} "
                      f"shortage={it.get('shortage')!r}")
    else:
        print(_json(ls))

    print()
    print("=" * 78)
    print("  END OF VERIFICATION")
    print("=" * 78)
    print(
        "Compare each section above with what Claude Desktop / Cursor / Code\n"
        "returned for the same prompt. Counts and IDs should match exactly;\n"
        "natural-language summaries will differ but the underlying data won't."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
