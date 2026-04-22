"""
tools/integrations.py — Webhook subscriptions, IoT alert ingestion, and ERP event bridge.
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.audit import get_audit_logger
from core.cache import get_cache
from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.rbac import require_role

EVTCFG_OS = "/os/mxevntcfg"
LOCAL_SUBS_CACHE_KEY = "mcp:local_subscriptions"


async def _maximo_event_os_available() -> bool:
    """
    MXEVNTCFG is not always published as an object structure.
    When missing, fall back to a local in-process subscription registry.
    """
    try:
        client = await get_connected_client()
        # lightweight probe
        await client.get(EVTCFG_OS, params={"lean": "1", "oslc.pageSize": 1})
        return True
    except MaximoAPIError as exc:
        if getattr(exc, "status_code", 0) in (404, 400):
            return False
        return False
    except Exception:
        return False


async def _local_subs_get() -> List[Dict[str, Any]]:
    cache = get_cache()
    data = await cache.get(LOCAL_SUBS_CACHE_KEY)
    if isinstance(data, dict) and "subscriptions" in data:
        subs = data.get("subscriptions") or []
        return subs if isinstance(subs, list) else []
    if isinstance(data, list):
        return data
    return []


async def _local_subs_set(subs: List[Dict[str, Any]]) -> None:
    cache = get_cache()
    await cache.set(LOCAL_SUBS_CACHE_KEY, {"subscriptions": subs}, ttl=86400)


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0) -> Dict:
    return {"success": True, "data": data, "metadata": {"cached": cached, "duration_ms": duration_ms}}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


@require_role("admin")
async def subscribe_to_event(
    event_type: str,
    callback_url: str,
    filter_conditions: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Register a Maximo event listener / webhook subscription.
    Maximo will POST to callback_url when the specified event occurs.

    Args:
        event_type:        Event to subscribe to (e.g., "WOSTATUSCHANGE", "ASSETCHANGE")
        callback_url:      URL that Maximo will POST to when event fires
        filter_conditions: Optional field filters {field: value} to limit event scope

    Returns:
        Subscription record with assigned ID.
    """
    if not event_type or not callback_url:
        return _error("event_type and callback_url are required", "VALIDATION_ERROR")
    if not callback_url.startswith(("http://", "https://")):
        return _error("callback_url must be a valid HTTP/HTTPS URL", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()

    body: Dict[str, Any] = {
        "evtname": event_type.upper(),
        "url": callback_url,
        "active": True,
        "description": f"MCP webhook: {event_type} → {callback_url}",
        "createdate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }
    if filter_conditions:
        body["filter"] = " and ".join(f'{k}="{v}"' for k, v in filter_conditions.items())

    try:
        if not await _maximo_event_os_available():
            subs = await _local_subs_get()
            rec = {
                "evtname": body["evtname"],
                "url": body["url"],
                "active": True,
                "description": body.get("description", ""),
                "createdate": body.get("createdate", ""),
                "source": "local_fallback",
            }
            subs.append(rec)
            await _local_subs_set(subs)
            result = rec
        else:
            client = await get_connected_client()
            result = await client.post(EVTCFG_OS, body=body)
        duration_ms = int((time.monotonic() - start) * 1000)

        await get_cache().invalidate("maximo:subscriptions")
        inputs = {"event_type": event_type, "callback_url": callback_url}
        envelope = _envelope(result, duration_ms=duration_ms)
        await audit.record("subscribe_to_event", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_event_subscriptions() -> Dict[str, Any]:
    """
    List all active Maximo event listener / webhook subscriptions.

    Returns:
        List of active subscriptions with event type, callback URL, and status.
    """
    start = time.monotonic()
    cache = get_cache()

    async def fetch():
        if not await _maximo_event_os_available():
            subs = await _local_subs_get()
            return {"member": subs, "totalCount": len(subs)}
        client = await get_connected_client()
        params = client.build_oslc_query(
            where='active="true"',
            select="evtname,url,description,active,createdate",
            page_size=200,
            page_num=1,
            collectioncount=1,
        )
        return await client.get(EVTCFG_OS, params=params)

    try:
        data, cached = await cache.get_or_fetch("maximo:subscriptions", fetch, ttl=300)
        members = data.get("member", [])
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {"subscriptions": members, "count": len(members)},
            cached=cached, duration_ms=duration_ms
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("technician")
async def ingest_iot_alert(
    asset_num: str,
    sensor_type: str,
    reading_value: float,
    threshold: float,
    site_id: str,
    unit: Optional[str] = None,
    severity: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a Maximo work order triggered by an IoT sensor alert.
    Use this to bridge IoT platforms (SCADA, sensor APIs) to Maximo maintenance.

    Args:
        asset_num:     Asset number that triggered the alert
        sensor_type:   Sensor type (e.g., "VIBRATION", "TEMPERATURE", "PRESSURE")
        reading_value: Actual sensor reading that triggered the alert
        threshold:     Alert threshold that was exceeded
        site_id:       Site ID
        unit:          Unit of measurement (e.g., "mm/s", "°C", "bar")
        severity:      Alert severity: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"

    Returns:
        Created work order record with IoT alert details embedded.
    """
    if not all([asset_num, sensor_type, site_id]):
        return _error("asset_num, sensor_type, and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()

    severity = (severity or "MEDIUM").upper()
    unit_str = f" {unit}" if unit else ""
    priority_map = {"LOW": 4, "MEDIUM": 3, "HIGH": 2, "CRITICAL": 1}
    priority = priority_map.get(severity, 3)
    deviation_pct = round(((reading_value - threshold) / threshold) * 100, 1) if threshold else 0

    description = (
        f"IoT Alert [{severity}]: {sensor_type} on {asset_num} — "
        f"Reading {reading_value}{unit_str} exceeds threshold {threshold}{unit_str} "
        f"({deviation_pct:+.1f}%)"
    )

    wo_body: Dict[str, Any] = {
        "description": description[:100],
        "description_longdescription": (
            f"Automated work order created by IoT alert ingestion.\n"
            f"Sensor: {sensor_type}\n"
            f"Reading: {reading_value}{unit_str}\n"
            f"Threshold: {threshold}{unit_str}\n"
            f"Deviation: {deviation_pct:+.1f}%\n"
            f"Severity: {severity}\n"
            f"Alert time: {datetime.now().isoformat()}"
        ),
        "assetnum": asset_num,
        "siteid": site_id,
        "priority": priority,
        "worktype": "EM" if severity == "CRITICAL" else "CM",
        "status": "WAPPR",
        "reportdate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "reportedby": "IOT-SYSTEM",
    }

    try:
        client = await get_connected_client()
        result = await client.post("/os/mxwo", body=wo_body)
        duration_ms = int((time.monotonic() - start) * 1000)

        inputs = {
            "asset_num": asset_num, "sensor_type": sensor_type,
            "reading_value": reading_value, "threshold": threshold, "site_id": site_id
        }
        envelope = _envelope(
            {
                "work_order": result,
                "iot_alert": {
                    "sensor_type": sensor_type,
                    "reading_value": reading_value,
                    "threshold": threshold,
                    "unit": unit,
                    "severity": severity,
                    "deviation_pct": deviation_pct,
                },
            },
            duration_ms=duration_ms
        )
        await audit.record("ingest_iot_alert", inputs, envelope, duration_ms=duration_ms)
        return envelope
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("admin")
async def trigger_webhook(
    event_type: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Manually fire a test webhook for a registered event type.
    Useful for testing webhook integrations without waiting for real events.

    Args:
        event_type: Event type to simulate (e.g., "WOSTATUSCHANGE")
        payload:    Custom payload to include in the test webhook

    Returns:
        Webhook dispatch result with subscription list and HTTP status.
    """
    if not event_type or not payload:
        return _error("event_type and payload are required", "VALIDATION_ERROR")

    start = time.monotonic()
    audit = get_audit_logger()

    # Get matching subscriptions
    subs_result = await list_event_subscriptions()
    if not subs_result["success"]:
        return subs_result

    subs: List[Dict] = subs_result["data"]["subscriptions"]
    matching = [s for s in subs if s.get("evtname", "").upper() == event_type.upper()]

    if not matching:
        return _error(f"No active subscriptions found for event type '{event_type}'", "NOT_FOUND")

    import httpx
    results = []
    for sub in matching:
        try:
            async with httpx.AsyncClient(timeout=10) as http_client:
                resp = await http_client.post(
                    sub["url"],
                    json={"event_type": event_type, "payload": payload, "test": True},
                    headers={"X-Maximo-MCP-Test": "true", "Content-Type": "application/json"},
                )
                results.append({"url": sub["url"], "status_code": resp.status_code, "success": resp.status_code < 400})
        except Exception as exc:
            results.append({"url": sub["url"], "error": str(exc), "success": False})

    duration_ms = int((time.monotonic() - start) * 1000)
    inputs = {"event_type": event_type, "subscription_count": len(matching)}
    envelope = _envelope(
        {"event_type": event_type, "subscriptions_notified": len(matching), "results": results},
        duration_ms=duration_ms
    )
    await audit.record("trigger_webhook", inputs, envelope, duration_ms=duration_ms)
    return envelope
