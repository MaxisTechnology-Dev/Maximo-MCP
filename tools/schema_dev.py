"""
tools/schema_dev.py — Maximo object structure discovery, OSLC validation, and code generation.
"""

import asyncio
import logging
import random
import time
from typing import Any, Dict, List, Optional

from core.cache import SCHEMA_CACHE_KEY, SCHEMA_CACHE_TTL, get_cache
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.rbac import require_role
from core.settings import get_settings

logger = logging.getLogger(__name__)


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


def _metadata_backoff_seconds(attempt: int) -> float:
    settings = get_settings()
    base = float(getattr(settings, "HTTP_RETRY_BACKOFF_BASE_SECONDS", 0.8) or 0.8)
    cap = float(getattr(settings, "HTTP_RETRY_BACKOFF_MAX_SECONDS", 15.0) or 15.0)
    wait = min(cap, base * (2**attempt))
    return max(0.1, wait + random.random() * 0.25)


def _normalize_metadata_attribute(attr: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(attr, dict):
        return None
    name = (
        attr.get("name")
        or attr.get("attributeName")
        or attr.get("attrName")
        or attr.get("column")
        or attr.get("attribute")
    )
    if not name:
        return None
    length = attr.get("length")
    if length is None:
        length = attr.get("size") or attr.get("maxLength")
    try:
        if length is not None:
            length = int(length)
    except (TypeError, ValueError):
        length = None
    field: Dict[str, Any] = {
        "name": str(name),
        "type": str(
            attr.get("type")
            or attr.get("maxType")
            or attr.get("attributeType")
            or "string"
        ),
        "required": bool(attr.get("required", attr.get("requiredFlag", False))),
        "length": length,
        "title": str(
            attr.get("title")
            or attr.get("titleName")
            or attr.get("remarks")
            or ""
        ),
        "readonly": bool(attr.get("readonly", attr.get("readOnly", attr.get("read_only", False)))),
    }
    domain = attr.get("domain") or attr.get("domainName") or attr.get("domainid")
    if domain is not None and str(domain).strip() != "":
        field["domain"] = domain
    if field["length"] is None:
        del field["length"]
    return field


def _parse_metadata_attributes(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    attrs = raw.get("attributes")
    if attrs is None:
        attrs = raw.get("Attributes", [])
    # Defensive: some environments may return dict-of-attrs; normalize to a list.
    if isinstance(attrs, dict):
        attrs = list(attrs.values())
    if not isinstance(attrs, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in attrs:
        parsed = _normalize_metadata_attribute(item)
        if parsed:
            out.append(parsed)
    return out


def _parse_metadata_relationships(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    rels = raw.get("relationships") if "relationships" in raw else raw.get("Relationships", [])
    # Defensive: some environments return dict-of-relationships; normalize to list.
    if isinstance(rels, dict):
        # Try to preserve the relationship name if it's the dict key.
        normalized: List[Dict[str, Any]] = []
        for k, v in rels.items():
            if isinstance(v, dict):
                normalized.append({"name": k, **v})
        rels = normalized
    if not isinstance(rels, list):
        return []
    out: List[Dict[str, Any]] = []
    for r in rels:
        if not isinstance(r, dict):
            continue
        name = r.get("name") or r.get("relationshipName") or r.get("relationship")
        if not name:
            continue
        target = (
            r.get("object")
            or r.get("objectName")
            or r.get("targetObject")
            or r.get("target")
            or r.get("relatedObject")
        )
        card = (
            r.get("cardinality")
            or r.get("cardinalityText")
            or r.get("cardinalityHint")
        )
        out.append(
            {
                "name": str(name),
                "target_object": str(target) if target is not None else "",
                "cardinality": str(card) if card is not None else "",
            }
        )
    return out


def _looks_like_metadata_payload(data: Any) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    return any(k in data for k in ("attributes", "Attributes", "relationships", "Relationships"))


def _metadata_endpoint_for_base_url(object_structure_upper: str) -> str:
    """
    Build a metadata endpoint that works regardless of MAXIMO_URL base path.

    Common deployments set MAXIMO_URL to one of:
    - https://host/maximo/oslc
    - https://host/maximo/api
    - https://host

    The metadata endpoint is typically reachable as:
    - /api/metadata/{OS}   (when base_url already includes /maximo/oslc or /maximo)
    - /maximo/api/metadata/{OS} (when base_url is host root)
    """
    settings = get_settings()
    base = (getattr(settings, "MAXIMO_URL", "") or "").rstrip("/").lower()
    # If base_url is /maximo/oslc, metadata is usually under /maximo/api (different context).
    # Use an absolute URL so httpx doesn't append it to /maximo/oslc.
    if base.endswith("/maximo/oslc") or "/maximo/oslc" in base:
        host = (getattr(settings, "MAXIMO_HOST", "") or "").rstrip("/")
        if host:
            return f"{host}/maximo/api/metadata/{object_structure_upper}"
        return f"/maximo/api/metadata/{object_structure_upper}"

    # If the base already includes "/maximo", don't prepend it again.
    if "/maximo/" in base or base.endswith("/maximo") or base.endswith("/maximo/api"):
        return f"/api/metadata/{object_structure_upper}"
    return f"/maximo/api/metadata/{object_structure_upper}"


async def _fetch_object_metadata_raw(
    object_structure_upper: str,
    *,
    siteid: Optional[str] = None,
    orgid: Optional[str] = None,
) -> Dict[str, Any]:
    """
    GET /maximo/api/metadata/{OS} — no lean=1 (metadata is not an OSLC lean collection).
    Uses MaximoClient._request for retries/401 handling; adds extra backoff for empty bodies.
    """
    client = await get_connected_client()
    endpoint = _metadata_endpoint_for_base_url(object_structure_upper)
    params: Optional[Dict[str, Any]] = None
    if siteid or orgid:
        params = {}
        if siteid:
            params["siteid"] = siteid
        if orgid:
            params["orgid"] = orgid
    settings = get_settings()
    max_attempts = int(getattr(settings, "HTTP_MAX_RETRIES", 3) or 3) + 1
    last_exc: Optional[Exception] = None

    for attempt in range(max_attempts):
        t0 = time.monotonic()
        try:
            data = await client._request("GET", endpoint, params=params)
        except (MaximoAPIError, MaximoAuthError) as exc:
            last_exc = exc
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "get_schema_details: metadata endpoint=%s failed after %dms (attempt %d/%d): %s",
                endpoint,
                elapsed_ms,
                attempt + 1,
                max_attempts,
                exc,
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(_metadata_backoff_seconds(attempt))
            else:
                raise
            continue

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if isinstance(data, dict):
            data.pop("_duration_ms", None)

        logger.info(
            "get_schema_details: metadata endpoint=%s completed in %dms (attempt %d/%d)",
            endpoint,
            elapsed_ms,
            attempt + 1,
            max_attempts,
        )

        if isinstance(data, dict) and data:
            if not _looks_like_metadata_payload(data):
                logger.warning(
                    "get_schema_details: unexpected metadata shape for %s keys=%s",
                    object_structure_upper,
                    list(data.keys())[:20],
                )
            return data

        logger.warning(
            "get_schema_details: empty or non-object metadata for %s (attempt %d/%d)",
            object_structure_upper,
            attempt + 1,
            max_attempts,
        )
        if attempt < max_attempts - 1:
            await asyncio.sleep(_metadata_backoff_seconds(attempt))
        else:
            break

    if last_exc:
        raise last_exc
    return {}


async def _fetch_object_describe_raw(object_structure_upper: str) -> Dict[str, Any]:
    """
    Fallback: GET /os/{OS}?describe=1
    We call MaximoClient._request to avoid implicit lean=1 injection.
    """
    client = await get_connected_client()
    endpoint = f"/os/{object_structure_upper}?describe=1"
    t0 = time.monotonic()
    data = await client._request("GET", endpoint, params=None)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if isinstance(data, dict):
        data.pop("_duration_ms", None)
    logger.info("get_schema_details: fallback endpoint=%s completed in %dms", endpoint, elapsed_ms)
    return data if isinstance(data, dict) else {}


def _parse_describe_properties(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse /os/{OS}?describe=1 response (OpenAPI-ish) into the same field shape.
    """
    props = raw.get("properties")
    if not isinstance(props, dict):
        return []
    out: List[Dict[str, Any]] = []
    for field_name, field_def in props.items():
        if not field_name or str(field_name).startswith("_"):
            continue
        if not isinstance(field_def, dict):
            continue
        length = field_def.get("maxLength")
        try:
            if length is not None:
                length = int(length)
        except (TypeError, ValueError):
            length = None
        field: Dict[str, Any] = {
            "name": str(field_name),
            "type": str(field_def.get("type", "string")),
            "required": bool(field_def.get("required", False)),
            "title": str(field_def.get("title") or field_def.get("description") or ""),
            "readonly": bool(field_def.get("readonly", field_def.get("readOnly", False))),
        }
        if length is not None:
            field["length"] = length
        domain = field_def.get("domain") or field_def.get("domainid") or field_def.get("domainName")
        if domain is not None and str(domain).strip() != "":
            field["domain"] = domain
        out.append(field)
    return out


async def _get_schema() -> Dict[str, Any]:
    """Fetch and cache the Maximo OpenAPI schema."""
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        return await client.get("/schema")

    data, _ = await cache.get_or_fetch(SCHEMA_CACHE_KEY, fetch, ttl=SCHEMA_CACHE_TTL)
    return data


@require_role("readonly")
async def list_object_structures(
    filter_keyword: Optional[str] = None,
    include_custom: bool = True,
) -> Dict[str, Any]:
    """
    List all available Maximo object structures (APIs).
    Results are cached for 24 hours.

    Args:
        filter_keyword: Optional keyword to filter by name or description
        include_custom: Include custom (ZCUST-prefixed) object structures

    Returns:
        List of object structures with name, description, and endpoint URL.
    """
    start = time.monotonic()
    cache = get_cache()
    cache_key = f"maximo:os_list:{filter_keyword}:{include_custom}"

    async def fetch():
        client = await get_connected_client()
        # Object structures are published via the MXINTOBJECT integration MBO (not bare /os).
        params: Dict[str, Any] = {
            "lean": "1",
            "oslc.select": "intobjectname,description,usewith",
            "oslc.pageSize": 500,
        }
        data = await client.get("/os/MXINTOBJECT", params=params)
        members: List[Dict] = list(data.get("member", []))
        page = 1
        while data.get("nextPage") and page < 100:
            page += 1
            data = await client.get(
                "/os/MXINTOBJECT",
                params={**params, "pageno": page},
            )
            members.extend(data.get("member", []))

        return {"member": members}

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=SCHEMA_CACHE_TTL)
        members: List[Dict] = data.get("member", [])

        # Filter
        if filter_keyword:
            kw = filter_keyword.lower()
            members = [
                m
                for m in members
                if kw in str(m.get("intobjectname", "")).lower()
                or kw in str(m.get("description", "")).lower()
            ]
        if not include_custom:
            members = [
                m
                for m in members
                if not str(m.get("intobjectname", "")).upper().startswith("Z")
            ]

        results = [
            {
                "name": m.get("intobjectname", m.get("name", m.get("objectname", ""))),
                "description": m.get("description", ""),
                "usewith": m.get("usewith", ""),
                "endpoint": f"/os/{str(m.get('intobjectname', '')).lower()}",
            }
            for m in members
        ]

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"object_structures": results, "totalCount": len(results)},
            cached=cached, duration_ms=duration_ms, record_count=len(results)
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_schema_details(
    object_structure: str,
    include_relationships: bool = True,
    siteid: Optional[str] = None,
    orgid: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get detailed field information for a Maximo object structure.

    Uses GET /maximo/api/metadata/{OBJECT_STRUCTURE} (object structure name uppercased).

    Args:
        object_structure:      Object structure name (e.g., "mxasset", "mxwo")
        include_relationships: Include child/relationship objects

    Returns:
        Fields (from attributes) with name, type, required, length, title, readonly, domain;
        optional relationships with name, target_object, cardinality.
    """
    if not object_structure:
        return _error("object_structure is required", "VALIDATION_ERROR")

    os_upper = str(object_structure).strip().upper()
    if not os_upper:
        return _error("object_structure is required", "VALIDATION_ERROR")

    start = time.monotonic()
    # One cached payload per OS; relationships are derived without a second HTTP call.
    cache_key = f"maximo:schema:metadata:{os_upper}:{(siteid or '').strip().upper()}:{(orgid or '').strip().upper()}"
    cache = get_cache()

    async def fetch():
        # Skip /api/metadata — not available on this Maximo version (BMXAA8816E).
        # Try /os describe with a 20-second timeout, then fall back to a probe record
        # (page_size=1) to infer field names when describe is too slow for large OS.
        client = await get_connected_client()
        try:
            raw = await asyncio.wait_for(
                client._request("GET", f"/os/{os_upper}?describe=1", params=None),
                timeout=20.0,
            )
            if isinstance(raw, dict):
                raw.pop("_duration_ms", None)
            return {"_source": "describe", "payload": raw if isinstance(raw, dict) else {}}
        except asyncio.TimeoutError:
            logger.warning(
                "get_schema_details: describe for %s timed out; falling back to probe record",
                os_upper,
            )

        # Fallback: fetch one record and extract field names as minimal schema
        try:
            params = client.build_oslc_query(page_size=1)
            sample = await client.get(f"/os/{os_upper}", params=params)
            members = sample.get("member", [])
            if members:
                field_names = [k for k in members[0].keys() if not k.startswith(("_", "href", "rdf:"))]
                properties = {f: {"title": f, "type": "string", "_inferred": True} for f in field_names}
                return {"_source": "describe", "payload": {"properties": properties, "_inferred_from_probe": True}}
        except Exception:
            pass

        raise MaximoAPIError(
            f"Schema describe for {os_upper} timed out after 20 s and probe fallback failed."
        )

    try:
        raw, cached = await cache.get_or_fetch(cache_key, fetch, ttl=SCHEMA_CACHE_TTL)
        duration_ms = int((time.monotonic() - start) * 1000)

        if not isinstance(raw, dict):
            logger.error(
                "get_schema_details: cached metadata for %s is not a dict: %s",
                os_upper,
                type(raw).__name__,
            )
            return _error(
                "Unexpected metadata response type from cache",
                "INTERNAL_ERROR",
            )

        source = str(raw.get("_source") or "metadata")
        payload_raw = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw

        # If metadata returned empty/unexpected, fallback to describe=1 for resilience.
        if source == "metadata" and (not isinstance(payload_raw, dict) or not payload_raw or not _looks_like_metadata_payload(payload_raw)):
            logger.warning(
                "get_schema_details: metadata payload empty/unexpected for %s; falling back to /os describe",
                os_upper,
            )
            source = "describe"
            payload_raw = await _fetch_object_describe_raw(os_upper)

        if not isinstance(payload_raw, dict):
            return _error("Unexpected metadata response type from API", "INTERNAL_ERROR")

        if source == "describe":
            fields = _parse_describe_properties(payload_raw)
            rels: List[Dict[str, Any]] = []
            raw_attr_count = len(payload_raw.get("properties", {}) or {}) if isinstance(payload_raw.get("properties"), dict) else 0
        else:
            fields = _parse_metadata_attributes(payload_raw)
            rels = _parse_metadata_relationships(payload_raw) if include_relationships else []
            attrs_raw = payload_raw.get("attributes") or payload_raw.get("Attributes") or []
            if isinstance(attrs_raw, dict):
                raw_attr_count = len(attrs_raw)
            elif isinstance(attrs_raw, list):
                raw_attr_count = len(attrs_raw)
            else:
                raw_attr_count = 0

        logger.info(
            "get_schema_details: source=%s os=%s raw_attr_count=%d parsed_field_count=%d include_relationships=%s",
            source,
            os_upper,
            raw_attr_count,
            len(fields),
            include_relationships,
        )

        payload: Dict[str, Any] = {
            "object_structure": os_upper,
            "field_count": len(fields),
            "fields": fields,
            "source": source,
        }
        if include_relationships:
            payload["relationship_count"] = len(rels)
            payload["relationships"] = rels

        if not fields and not (include_relationships and payload.get("relationships")):
            logger.warning(
                "get_schema_details: no attributes or relationships parsed for %s (source=%s)",
                os_upper,
                source,
            )

        return _envelope(
            payload,
            cached=cached,
            duration_ms=duration_ms,
            record_count=len(fields),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        logger.error(
            "get_schema_details: API error for %s: %s",
            os_upper,
            exc,
        )
        return _error(str(exc))
    except Exception as exc:
        logger.exception(
            "get_schema_details: unexpected error for %s",
            os_upper,
        )
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def validate_oslc_query(
    object_structure: str,
    where_clause: Optional[str] = None,
    select_clause: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validate OSLC query syntax by doing a dry-run against Maximo with page_size=1.
    Returns validation result and any error messages.

    Args:
        object_structure: Object structure to query
        where_clause:     OSLC where clause to validate
        select_clause:    OSLC select clause to validate

    Returns:
        is_valid (bool), error details, and sample field list.
    """
    if not object_structure:
        return _error("object_structure is required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        params: Dict[str, Any] = {"lean": "1", "oslc.pageSize": 1}
        if where_clause:
            params["oslc.where"] = where_clause
        if select_clause:
            params["oslc.select"] = select_clause

        data = await client.get(f"/os/{object_structure}", params=params)
        duration_ms = int((time.monotonic() - start) * 1000)

        members = data.get("member", [])
        available_fields = list(members[0].keys()) if members else []

        return _envelope(
            {
                "is_valid": True,
                "object_structure": object_structure,
                "where_clause": where_clause,
                "select_clause": select_clause,
                "sample_record_fields": available_fields,
                "message": "Query is valid and returned results." if members else "Query is valid but returned no results.",
            },
            duration_ms=duration_ms
        )
    except MaximoAPIError as exc:
        return _envelope(
            {
                "is_valid": False,
                "object_structure": object_structure,
                "where_clause": where_clause,
                "select_clause": select_clause,
                "error": str(exc),
                "message": "OSLC query failed validation.",
            },
            duration_ms=int((time.monotonic() - start) * 1000)
        )
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def generate_api_code(
    object_structure: str,
    operation: str = "list",
    language: str = "python",
    where_clause: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate ready-to-use code for Maximo API operations.

    Args:
        object_structure: Maximo object structure (e.g., "mxasset")
        operation:        "list" | "get" | "create" | "update" | "delete"
        language:         "python" | "javascript" | "curl" | "sql"
        where_clause:     Optional OSLC where clause for list/get operations

    Returns:
        Generated code string for the requested operation.
    """
    from core.settings import get_settings
    settings = get_settings()
    base_url = settings.MAXIMO_URL
    os_endpoint = f"/os/{object_structure}"
    where = where_clause or f'siteid="BEDFORD"'

    templates: Dict[str, Dict[str, str]] = {
        "python": {
            "list": f'''import httpx
import base64

# Maximo Basic Auth
creds = base64.b64encode("username:password".encode()).decode()
headers = {{"Authorization": f"Basic {{creds}}", "Accept": "application/json"}}

url = "{base_url}{os_endpoint}"
params = {{
    "lean": "1",
    "oslc.where": '{where}',
    "oslc.pageSize": 50
}}

with httpx.Client() as client:
    resp = client.get(url, headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()
    records = data.get("member", [])
    print(f"Found {{len(records)}} {object_structure} records")
    for r in records:
        print(r)
''',
            "create": f'''import httpx, base64

creds = base64.b64encode("username:password".encode()).decode()
headers = {{"Authorization": f"Basic {{creds}}", "Content-Type": "application/json"}}

body = {{
    # Add required fields here
    "siteid": "BEDFORD",
}}

with httpx.Client() as client:
    resp = client.post("{base_url}{os_endpoint}?lean=1", headers=headers, json=body)
    resp.raise_for_status()
    print("Created:", resp.json())
''',
        },
        "javascript": {
            "list": f'''const creds = btoa('username:password');
const headers = {{
  'Authorization': `Basic ${{creds}}`,
  'Accept': 'application/json'
}};

const params = new URLSearchParams({{
  lean: '1',
  'oslc.where': '{where}',
  'oslc.pageSize': '50'
}});

const resp = await fetch(`{base_url}{os_endpoint}?${{params}}`, {{headers}});
const data = await resp.json();
console.log('Records:', data.member?.length);
data.member?.forEach(r => console.log(r));
''',
        },
        "curl": {
            "list": f'''curl -u "username:password" \\
  -H "Accept: application/json" \\
  "{base_url}{os_endpoint}?lean=1&oslc.where={where}&oslc.pageSize=50"
''',
            "create": f'''curl -u "username:password" \\
  -X POST \\
  -H "Content-Type: application/json" \\
  -H "Accept: application/json" \\
  -d '{{"siteid":"BEDFORD"}}' \\
  "{base_url}{os_endpoint}?lean=1"
''',
        },
        "sql": {
            "list": f'''-- Direct Maximo DB query (use only for reporting, not production updates)
-- Object structure: {object_structure}
SELECT *
FROM {object_structure.upper().replace("MX", "")}
WHERE SITEID = 'BEDFORD'
ORDER BY CHANGEDATE DESC
FETCH FIRST 50 ROWS ONLY;
''',
        },
    }

    lang_templates = templates.get(language.lower())
    if not lang_templates:
        return _error(f"Language '{language}' not supported. Choose: python, javascript, curl, sql", "VALIDATION_ERROR")

    op_templates = lang_templates
    code = op_templates.get(operation, op_templates.get("list", "# Operation not implemented for this language"))

    start = time.monotonic()
    duration_ms = int((time.monotonic() - start) * 1000)
    return _envelope(
        {
            "object_structure": object_structure,
            "operation": operation,
            "language": language,
            "code": code,
        },
        duration_ms=duration_ms
    )


@require_role("admin")
async def build_custom_object_structure(
    name: str,
    base_object: str,
    fields: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Create a new custom object structure in Maximo via the API.

    Args:
        name:        New OS name (should start with Z for custom)
        base_object: Maximo base object to extend (e.g., "ASSET", "WORKORDER")
        fields:      List of {name, type, description, required} field dicts

    Returns:
        Created object structure definition.
    """
    if not name or not base_object or not fields:
        return _error("name, base_object, and fields are required", "VALIDATION_ERROR")
    if not name.upper().startswith("Z"):
        return _error("Custom object structures must start with 'Z' (e.g., ZMYASSET)", "VALIDATION_ERROR")

    from core.audit import get_audit_logger
    start = time.monotonic()
    audit = get_audit_logger()

    body = {
        "name": name.upper(),
        "description": f"Custom OS based on {base_object}",
        "objectname": base_object.upper(),
        "osattribute": [
            {
                "attributename": f.get("name", "").upper(),
                "attributetype": f.get("type", "ALN"),
                "required": f.get("required", False),
                "remarks": f.get("description", ""),
            }
            for f in fields
        ],
    }

    try:
        client = await get_connected_client()
        result = await client.post("/os/mxos", body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        inputs = {"name": name, "base_object": base_object, "field_count": len(fields)}
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("build_custom_object_structure", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
