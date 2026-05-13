"""Probe Wave-7 data shape: classification, assetspec, assettype, meter types, worktypes."""
from __future__ import annotations
import asyncio
import os
import sys
import json
from collections import Counter

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


async def main() -> int:
    from core.maximo_client import get_connected_client
    client = await get_connected_client()

    print("=== Asset classification + types (BEDFORD) ===")
    p = client.build_oslc_query(
        where='siteid="BEDFORD"',
        select="assetnum,description,assettype,classstructureid,status,priority",
        page_size=200,
    )
    r = await client.get("/os/mxasset", params=p)
    rows = r.get("member", [])
    print(f"  Total: {len(rows)} assets")
    print(f"  assettype distribution: {Counter(a.get('assettype') for a in rows).most_common(10)}")
    print(f"  classstructureid distribution: {Counter(a.get('classstructureid') for a in rows).most_common(10)}")
    print(f"  status distribution: {Counter(a.get('status') for a in rows).most_common(10)}")

    print("\n=== Asset specifications (assetspec child) ===")
    p = client.build_oslc_query(
        where='assetnum="1001"',
        select="assetnum,description,assettype,assetspec",
        page_size=2,
    )
    r = await client.get("/os/mxasset", params=p)
    rows = r.get("member", [])
    if rows:
        spec = rows[0].get("assetspec") or []
        print(f"  Asset 1001 has {len(spec)} spec rows")
        if spec:
            print(f"  First 3 spec rows: {json.dumps(spec[:3], default=str)[:500]}")

    print("\n=== Classification structures available ===")
    for ep in ["/os/mxclassification", "/os/mxapiclassification", "/os/mxclassstructure"]:
        try:
            r = await client.get(ep, params={"lean": "1", "oslc.pageSize": 5, "oslc.select": "classstructureid,description,classificationid"})
            rows = r.get("member", [])
            print(f"  {ep}: {len(rows)} rows; first: {rows[0] if rows else '(none)'}")
            break
        except Exception as exc:
            msg = str(exc)[:80]
            print(f"  {ep}: FAIL {msg}")

    print("\n=== Work types (mxwo) ===")
    p = client.build_oslc_query(
        where='siteid="BEDFORD"',
        select="wonum,worktype,status",
        page_size=200,
    )
    r = await client.get("/os/mxwo", params=p)
    wos = r.get("member", [])
    print(f"  worktype distribution: {Counter(w.get('worktype') for w in wos).most_common(15)}")

    print("\n=== Meter types (mxmeter) ===")
    for ep in ["/os/mxmeter", "/os/mxapimeter"]:
        try:
            r = await client.get(ep, params={"lean": "1", "oslc.pageSize": 200, "oslc.select": "metername,description,metertype,unitofmeasure"})
            rows = r.get("member", [])
            print(f"  {ep}: {len(rows)} meter definitions")
            if rows:
                types = Counter(m.get("metertype") for m in rows)
                print(f"  metertype distribution: {types.most_common(10)}")
                # Look for specific types relevant to verticals
                vehicle_meters = [m for m in rows if any(k in (m.get("metername") or "").upper() for k in ("ODOMETER", "MILEAGE", "MILES", "KM", "FUEL"))]
                if vehicle_meters:
                    print(f"  Vehicle-relevant meters: {[m.get('metername') for m in vehicle_meters[:5]]}")
                temp_meters = [m for m in rows if "TEMP" in (m.get("metername") or "").upper()]
                if temp_meters:
                    print(f"  Temperature meters: {[m.get('metername') for m in temp_meters[:5]]}")
                pressure_meters = [m for m in rows if "PRESS" in (m.get("metername") or "").upper() or "PSI" in (m.get("metername") or "").upper()]
                if pressure_meters:
                    print(f"  Pressure meters: {[m.get('metername') for m in pressure_meters[:5]]}")
            break
        except Exception as exc:
            msg = str(exc)[:80]
            print(f"  {ep}: FAIL {msg}")

    print("\n=== Locations: type distribution ===")
    p = client.build_oslc_query(
        where='siteid="BEDFORD"',
        select="location,description,type,parent,status",
        page_size=200,
    )
    r = await client.get("/os/mxoperloc", params=p)
    locs = r.get("member", [])
    print(f"  Total: {len(locs)} locations; type distribution: {Counter(l.get('type') for l in locs).most_common(10)}")

    print("\n=== Parent WO relationships (turnaround / project candidates) ===")
    p = client.build_oslc_query(
        where='siteid="BEDFORD"',
        select="wonum,parent,worktype,description,status",
        page_size=200,
    )
    r = await client.get("/os/mxwo", params=p)
    wos = r.get("member", [])
    parents = Counter(w.get("parent") for w in wos if w.get("parent"))
    print(f"  WOs with parent set: {sum(1 for w in wos if w.get('parent'))}")
    print(f"  Top 5 parent WOs (turnaround candidates): {parents.most_common(5)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
