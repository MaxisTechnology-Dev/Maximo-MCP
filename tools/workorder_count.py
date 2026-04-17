"""
tools/workorder_count.py — Stateless OSLC totalCount tool for Maximo work orders.

Designed for MCP tool usage:
- No session/cookies
- Basic Auth (no API keys)
- Dynamic filters -> oslc.where
- Efficient count via responseInfo.totalCount (pageSize=1, select=wonum)
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, Mapping, Tuple
from urllib.parse import urljoin

import requests
from requests.auth import HTTPBasicAuth

from core.oslc_utils import oslc_escape


MXWO_COUNT_ENDPOINT = "/maximo/oslc/os/mxwo"
_DEFAULT_TIMEOUT_SECONDS = 120

# Accepted external filter names -> OSLC attribute name in mxwo
_FILTER_FIELD_MAP: Dict[str, str] = {
    "siteid": "siteid",
    "status": "status",
    "wonum": "wonum",
    "assetnum": "assetnum",
    # Maximo mxwo uses wopriority in OSLC; we accept user-friendly "priority" too.
    "priority": "wopriority",
    "wopriority": "wopriority",
}


def _coerce_and_validate_filters(filters: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """
    Validate incoming filters and normalize them for OSLC where construction.

    Returns:
        (normalized_filters, oslc_field_map)
    """
    if not isinstance(filters, Mapping):
        raise TypeError("filters must be a dict-like mapping")
    if not filters:
        raise ValueError("filters must not be empty")

    normalized: Dict[str, Any] = {}
    oslc_fields: Dict[str, str] = {}

    for raw_key, raw_val in filters.items():
        if raw_key is None:
            raise ValueError("filter key must not be null")
        key = str(raw_key).strip().lower()
        if not key:
            raise ValueError("filter key must not be empty")
        if key not in _FILTER_FIELD_MAP:
            allowed = ", ".join(sorted(_FILTER_FIELD_MAP.keys()))
            raise ValueError(f"Unsupported filter '{raw_key}'. Allowed filters: {allowed}")

        if raw_val is None:
            raise ValueError(f"Filter '{raw_key}' must not be null")

        # Normalize and type-check values
        if key in ("priority", "wopriority"):
            if isinstance(raw_val, bool):
                raise TypeError("priority must be an int (bool is not allowed)")
            if not isinstance(raw_val, int):
                raise TypeError("priority must be an int")
            if raw_val < 0:
                raise ValueError("priority must be >= 0")
            normalized[key] = int(raw_val)
        else:
            # Treat all other supported filters as string-like
            if isinstance(raw_val, (int, float, bool)):
                raise TypeError(f"Filter '{raw_key}' must be a string")
            val = str(raw_val).strip()
            if not val:
                raise ValueError(f"Filter '{raw_key}' must not be empty")
            normalized[key] = val

        oslc_fields[key] = _FILTER_FIELD_MAP[key]

    return normalized, oslc_fields


def build_where_clause(filters: Mapping[str, Any]) -> str:
    """
    Build an OSLC where clause from a dynamic filters dict.

    Rules:
    - String values -> field="VALUE" (escaped)
    - Numeric values -> field=123
    - Join using ' and '

    Example:
        {"siteid": "WW", "priority": 1} -> 'siteid="WW" and wopriority=1'
    """
    normalized, oslc_fields = _coerce_and_validate_filters(filters)
    parts = []
    for key in sorted(normalized.keys()):
        field = oslc_fields[key]
        val = normalized[key]
        if isinstance(val, int):
            parts.append(f"{field}={val}")
        else:
            parts.append(f'{field}="{oslc_escape(val)}"')
    return " and ".join(parts)


def _get_env_credentials() -> Tuple[str, str]:
    user = (os.getenv("MAXIMO_USERNAME") or "").strip()
    pw = os.getenv("MAXIMO_PASSWORD") or ""
    if not user or not pw:
        raise ValueError("Missing MAXIMO_USERNAME/MAXIMO_PASSWORD environment variables")
    return user, pw


def _get_base_url() -> str:
    """
    Determines the base URL from environment.

    We prefer MAXIMO_URL (already used elsewhere in this repo). It may be either:
    - http(s)://host:port/maximo/oslc
    - http(s)://host:port/maximo/oslc/

    For this tool we call /maximo/oslc/os/mxwo, so we join from the host root.
    """
    base = (os.getenv("MAXIMO_URL") or "").strip()
    if not base:
        raise ValueError("Missing MAXIMO_URL environment variable")

    # Convert ".../maximo/oslc" into ".../" so urljoin with /maximo/oslc/os/mxwo is stable.
    # If MAXIMO_URL is already a host root, this is a no-op.
    b = base.rstrip("/")
    if b.lower().endswith("/maximo/oslc"):
        b = b[: -len("/maximo/oslc")]
    return b.rstrip("/") + "/"


def count_workorders(filters: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Count work orders using Maximo OSLC responseInfo.totalCount.

    Constraints (MCP-friendly):
    - Stateless (no cookies/session)
    - Basic Auth only (HTTPBasicAuth)
    - No pagination loops
    - Minimal payload: oslc.select=wonum and oslc.pageSize=1
    - Returns clean JSON only: {"filters": {...}, "count": N} or {"error": "..."}
    """
    try:
        normalized, _ = _coerce_and_validate_filters(filters)
        where = build_where_clause(normalized)
        user, pw = _get_env_credentials()
        base_url = _get_base_url()

        url = urljoin(base_url, MXWO_COUNT_ENDPOINT.lstrip("/"))

        # Some Maximo 7.6.x deployments accept Authorization only; others also require maxauth.
        # We keep this header aligned with Basic Auth credentials (no session/cookies).
        creds_b64 = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
        headers = {
            "Accept": "application/json",
            "maxauth": creds_b64,
        }
        params = {
            "lean": "1",
            "oslc.where": where,
            "oslc.select": "wonum",
            "oslc.pageSize": 1,
        }

        resp = requests.get(
            url,
            params=params,
            headers=headers,
            auth=HTTPBasicAuth(user, pw),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )

        if resp.status_code == 401:
            return {"error": "Unauthorized (401). Check Maximo username/password and Maximo security."}
        if resp.status_code >= 400:
            # Keep response body short and safe for MCP output
            body = (resp.text or "").strip()
            if len(body) > 500:
                body = body[:500] + "...(truncated)"
            return {"error": f"Maximo API error {resp.status_code}: {body or 'HTTP error'}"}

        try:
            data = resp.json()
        except ValueError:
            return {"error": "Invalid JSON response from Maximo"}

        ri = data.get("responseInfo") or data.get("oslc:responseInfo") or {}
        if not isinstance(ri, dict) or "totalCount" not in ri:
            return {"error": "Maximo response missing responseInfo.totalCount (ensure collectioncount support is enabled on server)"}

        try:
            total = int(ri.get("totalCount"))
        except (TypeError, ValueError):
            return {"error": "Maximo responseInfo.totalCount is not an integer"}

        return {"filters": dict(normalized), "count": total}

    except requests.exceptions.Timeout:
        return {"error": f"Request timed out after {_DEFAULT_TIMEOUT_SECONDS}s"}
    except requests.exceptions.ConnectionError:
        return {"error": "Connection error contacting Maximo"}
    except (TypeError, ValueError) as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Unexpected error: {type(exc).__name__}: {exc}"}

