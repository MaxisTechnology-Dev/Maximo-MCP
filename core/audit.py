"""
core/audit.py — Structured JSON-lines audit logger.
Every write operation (create/update/delete/status change) must call record().
Sensitive fields are NEVER written to the log.

Persistence is delegated to a pluggable sink (see core/audit_sinks.py):
file with rotation, stdout for cloud-native log shipping, or a composite.
query() reads back records and only works against a file-backed sink.
"""

import json
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.audit_sinks import AuditSink, FileSink

logger = logging.getLogger(__name__)

# Fields stripped before writing to audit log
SENSITIVE_FIELDS = {
    "api_key", "apikey", "password", "passwd", "secret",
    "token", "access_token", "refresh_token", "authorization",
    "maximo_api_key", "maximo_password", "oauth_client_secret",
}


def _sanitize(data: Any, depth: int = 0) -> Any:
    """Recursively remove sensitive keys from dicts."""
    if depth > 10:
        return data  # guard against deep nesting
    if isinstance(data, dict):
        return {
            k: "***REDACTED***" if k.lower() in SENSITIVE_FIELDS else _sanitize(v, depth + 1)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_sanitize(item, depth + 1) for item in data]
    return data


class AuditLogger:
    """
    Writes structured audit records via a pluggable sink. Backward
    compatible: AuditLogger("/path/to/file.jsonl") still works and builds
    a default FileSink. Pass `sink=...` to use stdout / composite / custom.
    """

    def __init__(
        self,
        log_file: Optional[str] = None,
        *,
        sink: Optional[AuditSink] = None,
    ):
        if sink is None:
            if log_file is None:
                raise ValueError("AuditLogger requires either log_file or sink")
            sink = FileSink(log_file)
        self._sink = sink
        # Retained for query(): only meaningful when a file is in the sink chain.
        self._log_file: Optional[Path] = Path(log_file) if log_file else None

    async def record(
        self,
        tool_name: str,
        input_params: Dict[str, Any],
        result: Any,
        user_id: str = "system",
        duration_ms: int = 0,
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:
        """
        Emit one audit record to the configured sink.

        Args:
            tool_name:    Name of the MCP tool that was called
            input_params: Tool input parameters (sensitive fields stripped)
            result:       Tool result summary (sensitive fields stripped)
            user_id:      Caller identity
            duration_ms:  Execution time in milliseconds
            success:      Whether the tool call succeeded
            error:        Error message if success=False
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "env": _current_env(),
            "tool": tool_name,
            "user_id": user_id,
            "duration_ms": duration_ms,
            "success": success,
            "input": _sanitize(input_params),
            "result_summary": _summarize_result(_sanitize(result)),
        }
        if error:
            entry["error"] = error

        try:
            await self._sink.write(entry)
        except Exception as exc:
            logger.error("Audit sink write failed: %s", exc)

    async def query(
        self,
        tool_name: Optional[str] = None,
        user_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 100,
    ) -> list[Dict[str, Any]]:
        """
        Read and filter audit log entries.

        Reads only the last MAX_TAIL_LINES lines from the file so that large
        logs never exhaust memory.  Results are returned newest-first and
        capped at `limit`.

        Args:
            tool_name: Filter by tool name (substring match)
            user_id:   Filter by user ID (exact match)
            date_from: ISO date string start (inclusive)
            date_to:   ISO date string end (inclusive)
            limit:     Maximum number of records to return (default 100)

        Returns:
            List of matching audit records (newest first)
        """
        if self._log_file is None:
            logger.debug(
                "Audit query() called but no file-backed sink is configured; returning []"
            )
            return []
        if not self._log_file.exists():
            return []

        # Read only the tail of the file — avoids loading GBs into memory.
        MAX_TAIL_LINES = 1000
        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                tail: deque = deque(f, maxlen=MAX_TAIL_LINES)
        except OSError as exc:
            logger.error("Failed to read audit log: %s", exc)
            return []

        # Process tail in reverse (newest lines last in deque → iterate reversed)
        # Stop as soon as we have `limit` matching records.
        filtered: list[Dict] = []
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Apply filters
            if tool_name and tool_name.lower() not in rec.get("tool", "").lower():
                continue
            if user_id and rec.get("user_id") != user_id:
                continue
            ts = rec.get("timestamp", "")
            if date_from and ts < date_from:
                continue
            if date_to and ts > date_to + "Z":
                continue
            filtered.append(rec)
            if len(filtered) >= limit:
                break

        return filtered


def _current_env() -> str:
    """Return MAXIMO_ENV for stamping into each audit record. Cached implicitly via settings singleton."""
    try:
        from core.settings import get_settings
        return getattr(get_settings(), "MAXIMO_ENV", "unknown") or "unknown"
    except Exception:
        return "unknown"


def _summarize_result(result: Any) -> Any:
    """Return a compact summary of a tool result for audit purposes."""
    if isinstance(result, dict):
        summary = {
            k: v for k, v in result.items()
            if k in {"success", "error", "error_code", "record_count", "totalCount"}
        }
        if "data" in result and isinstance(result["data"], dict):
            # Include key identifiers from data
            for id_field in ("assetnum", "wonum", "itemnum", "ponum", "pmnum"):
                if id_field in result["data"]:
                    summary[id_field] = result["data"][id_field]
        return summary or {"type": type(result).__name__}
    return {"type": type(result).__name__}


# ── Module-level singleton ────────────────────────────────────────────────────

_audit_instance: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    global _audit_instance
    if _audit_instance is None:
        from core.audit_sinks import build_sink_from_settings
        from core.settings import get_settings
        settings = get_settings()
        sink = build_sink_from_settings()
        # Only retain the file path when a file is actually part of the sink
        # — otherwise query() should not pretend the file exists.
        log_file = (
            settings.AUDIT_LOG_FILE
            if getattr(settings, "AUDIT_SINK", "file") in ("file", "both")
            else None
        )
        _audit_instance = AuditLogger(log_file, sink=sink)
    return _audit_instance
