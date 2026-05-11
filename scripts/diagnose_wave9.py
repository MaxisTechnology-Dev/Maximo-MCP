"""Probe Wave-9 data shape: spatial coordinate fields, GIS endpoints, location proximity."""
from __future__ import annotations
import asyncio
import os
import sys
import json

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


async def probe(label, endpoint, params):
    from core.maximo_client import get_connected_client
    client = await get_connected_client()
    try:
        r = await client.get(endpoint, params={"lean": "1", **params})
        rows = r.get("member", [])
        keys = list(rows[0].keys())[:20] if rows else "(none)"
        print(f"[OK]   {label:<55} rows={len(rows):<3}")
        if rows:
            print(f"        keys={keys}")
        return r
    except Exception as exc:
        msg = str(exc)
        if len(msg) > 110:
            msg = msg[:110] + "…"
        print(f"[FAIL] {label:<55} {msg}")
        return {}


async def main() -> int:
    print("=== Spatial / GIS object structures ===")
    for ep in [
        "/os/mxgis", "/os/mxapigis",
        "/os/mxlocgis", "/os/mxapilocgis",
        "/os/mxassetspatial", "/os/mxapiassetspatial",
        "/os/mxapilocation",
    ]:
        await probe(ep, ep, {"oslc.pageSize": 2})

    print("\n=== Asset coordinate fields ===")
    # Try common lat/lon column names
    for select in [
        "assetnum,siteid,location,latitude,longitude",
        "assetnum,siteid,location,latitudey,longitudex",
        "assetnum,siteid,location,gispoint",
        "assetnum,siteid,location,xcoord,ycoord",
        "assetnum,siteid,location,coordinates",
    ]:
        await probe(
            f"mxasset {select}",
            "/os/mxasset",
            {"oslc.pageSize": 2, "oslc.where": 'siteid="BEDFORD"', "oslc.select": select},
        )

    print("\n=== Location coordinate fields ===")
    for select in [
        "location,siteid,latitude,longitude",
        "location,siteid,latitudey,longitudex",
        "location,siteid,gispoint",
        "location,siteid,xcoord,ycoord",
    ]:
        await probe(
            f"mxoperloc {select}",
            "/os/mxoperloc",
            {"oslc.pageSize": 2, "oslc.where": 'siteid="BEDFORD"', "oslc.select": select},
        )

    print("\n=== Wider asset record (look for any geographic field) ===")
    r = await probe(
        "mxasset full record",
        "/os/mxasset",
        {"oslc.pageSize": 1, "oslc.where": 'assetnum="1001"', "oslc.select": "*"},
    )
    if r.get("member"):
        keys = sorted(r["member"][0].keys())
        # Filter for anything that looks geographic
        geo_like = [k for k in keys if any(t in k.lower() for t in ("lat", "long", "lon", "gis", "coord", "geo", "point", "x", "y"))]
        print(f"        Geographic-looking keys on asset 1001: {geo_like}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
