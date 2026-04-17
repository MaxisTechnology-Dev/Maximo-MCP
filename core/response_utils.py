"""
Shared response helpers for MCP tools and HTTP adapters.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def success_response(
    data: Any,
    *,
    cached: bool = False,
    duration_ms: int = 0,
    record_count: Optional[int] = None,
    request_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    if request_id:
        meta["request_id"] = request_id
    if metadata:
        meta.update(metadata)
    return {"success": True, "data": data, "metadata": meta}


def error_response(
    message: str,
    code: str = "API_ERROR",
    *,
    request_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"success": False, "error": message, "error_code": code}
    if request_id or metadata:
        payload["metadata"] = {}
        if request_id:
            payload["metadata"]["request_id"] = request_id
        if metadata:
            payload["metadata"].update(metadata)
    return payload


def attach_request_id(payload: Any, request_id: Optional[str]) -> Dict[str, Any]:
    if isinstance(payload, dict):
        if request_id:
            metadata = payload.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata.setdefault("request_id", request_id)
        return payload
    return success_response(payload, request_id=request_id)
