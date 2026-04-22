#!/usr/bin/env python3
"""
Integration test: verify Maximo URL, auth session, and sample OSLC reads.

Run from the project root (loads .env automatically via pydantic-settings):

    python scripts/test_maximo_connection.py

Exit code 0 = all critical checks passed; 1 = failure.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

# Allow `python scripts/test_maximo_connection.py` without PYTHONPATH.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def run() -> int:
    from core.maximo_client import MaximoAuthError, get_connected_client
    from tools.schema_dev import list_object_structures
    from tools.workorders import list_workorders

    print("=== Maximo MCP connectivity test ===\n")

    # 1) Session + whoami
    try:
        client = await get_connected_client()
        w = await client.get("/whoami", params={"lean": "1"})
        user = w.get("userName") or w.get("personid", "?")
        ver = w.get("maximoVersion", "?")
        print(f"[PASS] whoami user={user!r} maximoVersion={ver!r}")
    except MaximoAuthError as exc:
        print(f"[FAIL] authentication: {exc}")
        print("       Check MAXIMO_URL, MAXIMO_HOST, MAXIMO_USERNAME, MAXIMO_PASSWORD, AUTH_MODE=basic")
        return 1
    except Exception as exc:
        print(f"[FAIL] whoami: {exc}")
        traceback.print_exc()
        return 1

    ok = True

    # 2) Work orders — single status (indexed; should be fast on large DBs)
    try:
        r = await list_workorders(status="INPRG", page_size=5, page_num=1)
        if not r.get("success"):
            print(f"[WARN] list_workorders (INPRG): {r.get('error')}")
            ok = False
        else:
            rows = r["data"]["workorders"]
            tc = r["data"].get("totalCount", len(rows))
            print(f"[PASS] list_workorders INPRG: page rows={len(rows)}, totalCount={tc}")
            if rows:
                row = rows[0]
                print(
                    f"       sample wonum={row.get('wonum')} "
                    f"status={row.get('status')} site={row.get('siteid')}"
                )
    except Exception as exc:
        print(f"[WARN] list_workorders INPRG: {exc}")
        ok = False

    # 3) OPEN aggregate filter (OR clause — may take longer on busy servers)
    try:
        r = await list_workorders(status="OPEN", page_size=5, page_num=1)
        if not r.get("success"):
            print(f"[WARN] list_workorders OPEN: {r.get('error')}")
        else:
            rows = r["data"]["workorders"]
            tc = r["data"].get("totalCount", len(rows))
            print(f"[PASS] list_workorders OPEN: page rows={len(rows)}, totalCount={tc}")
    except Exception as exc:
        print(f"[WARN] list_workorders OPEN: {exc}")

    # 4) Object structures (MXINTOBJECT)
    try:
        r = await list_object_structures()
        if not r.get("success"):
            print(f"[WARN] list_object_structures: {r.get('error')}")
        else:
            n = r["data"].get("totalCount", 0)
            print(f"[PASS] list_object_structures: totalCount={n}")
    except Exception as exc:
        print(f"[WARN] list_object_structures: {exc}")

    print("\n=== done ===")
    return 0 if ok else 1


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
