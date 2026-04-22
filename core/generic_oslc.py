"""
core/generic_oslc.py — Generic OSLC query engine for IBM Maximo.

Two-layer architecture
----------------------
  Layer 1 (this module) — Core engine
      query_object_structure() resolves the correct object structure for a
      logical entity, applies field-alias mapping, probes candidates for
      availability, executes the query, and returns a normalised envelope.

  Layer 2 (tools/*.py) — Thin business wrappers
      Domain tools call query_object_structure() and adapt its output to
      their own response envelopes.  All object-structure selection, alias
      resolution, and fallback logic live here, not in the tool layer.

Fallback strategy
-----------------
For each entity the registry defines an ordered list of candidate object
structures (e.g. ["mxwo", "mxapiwodetail", "zwoapi"]).

1. The engine probes each candidate with is_object_structure_healthy().
   Candidates that are cached as unhealthy are skipped immediately.
2. The first healthy candidate is queried.  If the query itself fails
   (e.g. wrong field names on that specific Maximo build), the probe
   cache entry is invalidated and the next candidate is tried.
3. If every candidate fails the error envelope is returned:
       {"error": "OBJECT_STRUCTURE_UNAVAILABLE", "entity": ..., "tried": [...]}

Normalised response envelope (success)
---------------------------------------
{
    "object_structure": "mxwo",     # which OS was actually used
    "entity":           "workorder",
    "filters":          {...},       # caller-supplied (pre-alias) filters
    "totalCount":       42,
    "data":             [...],
    "_duration_ms":     123,
}

Error envelope
--------------
{
    "error":  "OBJECT_STRUCTURE_UNAVAILABLE" | "UNKNOWN_ENTITY" | "QUERY_ERROR",
    "entity": "workorder",
    "tried":  ["mxwo", "mxapiwodetail"],
    "detail": "human-readable cause",
}
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from core.capability_probe import invalidate_probe_cache, is_object_structure_healthy
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.object_registry import ObjectDefinition, get_object_definition, list_entities
from core.oslc_utils import oslc_escape, safe_field_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def query_object_structure(
    entity: str,
    filters: Dict[str, Any],
    select: Optional[List[str]] = None,
    page_size: int = 50,
    page_num: int = 1,
    order_by: Optional[str] = None,
    where_extra: Optional[str] = None,
    collectioncount: int = 1,
) -> Dict[str, Any]:
    """
    Execute a generic OSLC query for a logical Maximo entity.

    Parameters
    ----------
    entity : str
        Logical entity name registered in object_registry (e.g. "workorder",
        "asset").  Case-insensitive.
    filters : dict
        Simple equality filters using caller-friendly names.
        Examples::
            {"site_id": "BEDFORD"}            # → siteid="BEDFORD"
            {"priority": 1}                   # → wopriority=1
            {"asset_num": "PUMP-001"}         # → assetnum="PUMP-001"
        Field names are alias-resolved before building the WHERE clause.
        None/empty values are silently ignored.
    select : list[str], optional
        Caller-friendly field names to return.  Alias-resolved before use.
        Falls back to the entity's default_select when omitted.
    page_size : int
        Records per page (capped at 200 by MaximoClient).
    page_num : int
        1-based page number.
    order_by : str, optional
        Raw OSLC orderBy expression, e.g. "-reportdate" or "+assetnum".
        Use Maximo column names (not aliases) here.
    where_extra : str, optional
        A pre-built OSLC WHERE fragment that is ANDed with the clauses derived
        from *filters*.  Use this for complex expressions the engine cannot
        build from simple equality filters, e.g.::
            "status in [\"APPR\",\"WSCH\",\"INPRG\"]"
    collectioncount : int
        Pass 1 (default) to request totalCount in responseInfo.
        Pass 0 to skip the count SQL (faster, but totalCount = len(data)).

    Returns
    -------
    dict
        Success envelope (see module docstring) or error envelope.
    """
    start = time.monotonic()

    obj_def = get_object_definition(entity)
    if obj_def is None:
        return {
            "error": "UNKNOWN_ENTITY",
            "entity": entity,
            "tried": [],
            "detail": (
                f"No registry entry found for entity '{entity}'. "
                f"Known entities: {list_entities()}"
            ),
        }

    where = _build_where(obj_def, filters, where_extra)
    select_str = _build_select(obj_def, select)

    tried: List[str] = []
    last_error: str = ""

    for candidate in obj_def.candidates:
        tried.append(candidate)

        # Skip candidates that are cached-unhealthy (fast, no HTTP)
        healthy = await is_object_structure_healthy(candidate)
        if not healthy:
            logger.info(
                "generic_oslc: skipping unhealthy candidate '%s' for entity '%s'",
                candidate,
                entity,
            )
            continue

        try:
            raw = await _execute_query(
                object_structure=candidate,
                where=where,
                select=select_str,
                page_size=page_size,
                page_num=page_num,
                order_by=order_by,
                collectioncount=collectioncount if page_num == 1 else None,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            members = raw.get("member", [])
            total = raw.get("totalCount")
            if total is None:
                total = len(members)
            return {
                "object_structure": candidate,
                "entity": entity,
                "filters": filters,
                "totalCount": total,
                "data": members,
                "_duration_ms": duration_ms,
            }

        except (MaximoAPIError, MaximoAuthError) as exc:
            last_error = str(exc)
            logger.warning(
                "generic_oslc: query failed on '%s' for entity '%s': %s — "
                "invalidating probe cache and trying next candidate.",
                candidate,
                entity,
                exc,
            )
            # Invalidate so next call re-probes instead of skipping immediately
            invalidate_probe_cache(candidate)
            continue

        except Exception as exc:  # noqa: BLE001
            last_error = f"[{type(exc).__name__}] {exc}"
            logger.warning(
                "generic_oslc: unexpected error on '%s' for entity '%s': %s — "
                "trying next candidate.",
                candidate,
                entity,
                exc,
            )
            invalidate_probe_cache(candidate)
            continue

    return {
        "error": "OBJECT_STRUCTURE_UNAVAILABLE",
        "entity": entity,
        "tried": tried,
        "detail": last_error or "All candidate object structures are unavailable.",
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _execute_query(
    object_structure: str,
    where: Optional[str],
    select: Optional[str],
    page_size: int,
    page_num: int,
    order_by: Optional[str],
    collectioncount: Optional[int],
) -> Dict[str, Any]:
    """Execute the OSLC query against a specific object structure endpoint."""
    client = await get_connected_client()
    params = client.build_oslc_query(
        where=where,
        select=select,
        order_by=order_by,
        page_size=min(page_size, 200),
        page_num=page_num,
        collectioncount=collectioncount,
    )
    return await client.get(f"/os/{object_structure}", params=params)


def _build_where(
    obj_def: ObjectDefinition,
    filters: Dict[str, Any],
    where_extra: Optional[str],
) -> Optional[str]:
    """
    Build an OSLC WHERE clause from alias-mapped simple filters and an
    optional pre-built clause fragment.

    Each entry in *filters* is translated using the entity's alias map and
    validated through safe_field_name() to prevent injection.

    * Numeric values (int/float) → ``field=value``
    * All other values           → ``field="value"``  (OSLC-escaped)

    The *where_extra* fragment is appended last, joined with ``and``.
    Returns None when the result would be an empty string.
    """
    parts: List[str] = []

    for user_field, value in (filters or {}).items():
        if value is None:
            continue

        maximo_field = obj_def.resolve_alias(user_field)
        try:
            safe_field_name(maximo_field)
        except ValueError:
            logger.warning(
                "_build_where: rejected unsafe field name '%s' (alias of '%s')",
                maximo_field,
                user_field,
            )
            continue

        if isinstance(value, bool):
            # Booleans must be checked before int (bool is a subclass of int)
            parts.append(f'{maximo_field}="{str(value).lower()}"')
        elif isinstance(value, (int, float)):
            parts.append(f"{maximo_field}={value}")
        else:
            parts.append(f'{maximo_field}="{oslc_escape(str(value))}"')

    if where_extra:
        parts.append(where_extra)

    return " and ".join(parts) if parts else None


def _build_select(
    obj_def: ObjectDefinition,
    select: Optional[List[str]],
) -> Optional[str]:
    """
    Resolve the select list through alias mapping and return a
    comma-joined string, or None if the resolved list is empty
    (Maximo will then return all fields).
    """
    resolved = obj_def.resolve_select(select)
    return ",".join(resolved) if resolved else None
