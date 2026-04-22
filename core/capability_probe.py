"""
core/capability_probe.py — Lightweight health probe for Maximo object structures.

is_object_structure_healthy() makes a minimal OSLC request (pageSize=1) to
determine whether an object structure endpoint is reachable and functional.

Results are stored in an in-process cache with a configurable TTL so that
repeated calls within the same process do not generate extra HTTP traffic.
The cache is intentionally NOT shared across workers; each process probes
independently, which keeps the implementation stateless and dependency-free.

Probe TTL
---------
Default: 300 s (5 minutes).  Override by setting PROBE_CACHE_TTL_SECONDS in
the environment / .env before the module is imported.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.settings import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process probe cache
# ---------------------------------------------------------------------------
# object_structure → (is_healthy: bool, expires_at: float)
_PROBE_CACHE: Dict[str, Tuple[bool, float]] = {}

_PROBE_TTL_SECONDS: float = float(
    getattr(get_settings(), "PROBE_CACHE_TTL_SECONDS", None) or 300
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def is_object_structure_healthy(object_structure: str) -> bool:
    """
    Return True when the Maximo object structure endpoint is responsive.

    The check is based on a single OSLC GET with oslc.pageSize=1.
    Successful responses (any 2xx) return True; connection errors, auth
    failures, 404s, and 5xx errors return False.

    Results are cached for _PROBE_TTL_SECONDS to avoid per-request probes.

    Args:
        object_structure: Maximo object structure name, e.g. "mxwo".

    Returns:
        True  — endpoint responded successfully.
        False — endpoint unreachable or returned an error.
    """
    now = time.monotonic()
    cached = _PROBE_CACHE.get(object_structure)
    if cached is not None:
        is_healthy, expires_at = cached
        if now < expires_at:
            logger.debug(
                "probe cache hit: /os/%s → %s",
                object_structure,
                "healthy" if is_healthy else "unhealthy",
            )
            return is_healthy

    is_healthy = await _probe(object_structure)
    _PROBE_CACHE[object_structure] = (is_healthy, now + _PROBE_TTL_SECONDS)
    logger.info(
        "capability probe: /os/%s → %s",
        object_structure,
        "healthy" if is_healthy else "UNHEALTHY",
    )
    return is_healthy


def invalidate_probe_cache(object_structure: Optional[str] = None) -> None:
    """
    Invalidate one or all cached probe results.

    Args:
        object_structure: If supplied, only that entry is removed.
                          If None, the entire cache is cleared.
    """
    if object_structure is not None:
        _PROBE_CACHE.pop(object_structure, None)
    else:
        _PROBE_CACHE.clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _probe(object_structure: str) -> bool:
    """
    Execute a single minimal OSLC probe request against object_structure.

    Uses oslc.pageSize=1 with no where/select to minimise server load.
    Returns True on any successful HTTP response or on timeout (slow server
    does not mean endpoint is missing), False only on definitive errors
    (404, 401/403, connection refused).

    A timeout means the server is reachable but slow — the endpoint likely
    exists. Returning False here would cache the candidate as unhealthy and
    cause OBJECT_STRUCTURE_UNAVAILABLE even when the OS is perfectly valid.
    """
    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            select="*",
            page_size=1,
        )
        await client.get(f"/os/{object_structure}", params=params)
        return True
    except MaximoAuthError as exc:
        logger.warning(
            "probe auth error for /os/%s: %s", object_structure, exc
        )
        return False
    except MaximoAPIError as exc:
        msg = str(exc).lower()
        # Timeout: server is slow but endpoint may exist — assume healthy so
        # the real OSLC query gets a chance to run with a full timeout budget.
        if "timed out" in msg or "timeout" in msg:
            logger.warning(
                "probe timed out for /os/%s — assuming healthy (slow server). "
                "Increase HTTP_READ_TIMEOUT_SECONDS if queries also time out.",
                object_structure,
            )
            return True
        # 404 = endpoint definitively absent; other 4xx/5xx = definitively broken.
        logger.warning(
            "probe API error for /os/%s (status=%s): %s",
            object_structure,
            exc.status_code,
            exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "probe unexpected error for /os/%s [%s]: %s",
            object_structure,
            type(exc).__name__,
            exc,
        )
        return False
