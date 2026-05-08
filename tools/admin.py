"""
tools/admin.py — Maximo user, security group, and audit log query tools.
"""

import time
from typing import Any, Dict, List, Optional

from core.audit import get_audit_logger
from core.cache import get_cache
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role

USER_OS = "/os/mxperson"
GROUP_OS = "/os/mxsecgroup"


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


@require_role("admin")
async def list_users(site_id: Optional[str] = None) -> Dict[str, Any]:
    """
    List Maximo user (person) records.

    Args:
        site_id: Optional filter by default site

    Returns:
        List of user records with person ID, name, status, and security groups.
    """
    start = time.monotonic()
    where_parts = []
    if site_id:
        where_parts.append(f'defsite="{oslc_escape(site_id)}"')
    where = " and ".join(where_parts) if where_parts else None

    cache_key = f"maximo:users:{site_id}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        # OSLC orderBy requires +/- prefix; bare "personid" fails to parse (BMXAA8744E).
        all_members: List[Dict[str, Any]] = []
        page = 1
        total_count: Optional[int] = None
        data: Dict[str, Any] = {}
        page_size = 500
        while page < 200:
            data = await client.get(
                USER_OS,
                params=client.build_oslc_query(
                    where=where,
                    select="personid,displayname,status,defsite,primaryemail,phone,groupuser",
                    order_by="+personid",
                    page_size=page_size,
                    page_num=page,
                    collectioncount=1,
                ),
            )
            if total_count is None:
                total_count = data.get("totalCount")
            chunk = data.get("member", [])
            if isinstance(chunk, dict):
                chunk = [chunk]
            all_members.extend(chunk)
            if not chunk:
                break
            if len(chunk) < page_size:
                break
            if total_count is not None and len(all_members) >= total_count:
                break
            if data.get("nextPage"):
                page += 1
                continue
            # Some Maximo builds omit nextPage; continue while a full page may imply more rows.
            if total_count is not None and len(all_members) < total_count:
                page += 1
                continue
            break
        return {
            "member": all_members,
            "totalCount": total_count if total_count is not None else len(all_members),
        }

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=600)
        members = data.get("member", [])
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"users": members, "totalCount": data.get("totalCount", len(members))},
            cached=cached, duration_ms=duration_ms, record_count=len(members)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("manager")
async def get_user(user_id: str) -> Dict[str, Any]:
    """
    Get detailed information for a specific Maximo user.

    Args:
        user_id: Person ID (username)

    Returns:
        User record including contact details, status, and security group memberships.
    """
    if not user_id:
        return _error("user_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:user:{user_id}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'personid="{oslc_escape(user_id)}"',
            select="*",
        )
        return await client.get(USER_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=300)
        members = data.get("member", [])
        if not members:
            return _error(f"User '{user_id}' not found", "NOT_FOUND")
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(members[0], cached=cached, duration_ms=duration_ms)
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("admin")
async def list_security_groups() -> Dict[str, Any]:
    """
    List all Maximo security groups with their descriptions and member counts.

    Returns:
        List of security groups with name, description, and associated applications.
    """
    start = time.monotonic()
    cache_key = "maximo:security_groups"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            select="groupname,description,grouptype",
            order_by="+groupname",
            page_size=10,
        )
        return await client.get(GROUP_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=1800)
        members = data.get("member", [])
        # Count members per group
        enriched = []
        for group in members:
            group_users = group.get("groupuser", [])
            enriched.append({
                **{k: v for k, v in group.items() if k != "groupuser"},
                "member_count": len(group_users) if isinstance(group_users, list) else 0,
            })
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"security_groups": enriched, "totalCount": len(enriched)},
            cached=cached, duration_ms=duration_ms, record_count=len(enriched)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        # mxsecgroup may not be published on all Maximo instances
        if isinstance(exc, MaximoAPIError) and getattr(exc, "status_code", 0) == 404:
            duration_ms = int((time.monotonic() - start) * 1000)
            return _envelope(
                {"security_groups": [], "totalCount": 0, "not_available": True,
                 "message": "Security group object structure not published (404 /os/mxsecgroup)."},
                duration_ms=duration_ms, record_count=0
            )
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("manager")
async def query_audit_log(
    tool_name: Optional[str] = None,
    user_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """
    Search the MCP audit trail for tool call history.

    Args:
        tool_name: Filter by tool name (substring match)
        user_id:   Filter by user ID (exact match)
        date_from: ISO timestamp — show entries from this date/time
        date_to:   ISO timestamp — show entries up to this date/time
        limit:     Maximum records to return (default: 100, max: 1000)

    Returns:
        Matching audit log entries, newest first.
    """
    limit = min(limit, 1000)
    start = time.monotonic()

    audit = get_audit_logger()
    records = await audit.query(
        tool_name=tool_name,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    return _envelope(
        {"records": records, "count": len(records), "filters": {
            "tool_name": tool_name, "user_id": user_id, "date_from": date_from, "date_to": date_to
        }},
        duration_ms=duration_ms, record_count=len(records)
    )
