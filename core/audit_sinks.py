"""
core/audit_sinks.py — Pluggable audit-record sinks.

The audit logger writes one structured record per tool call. Where those
records *go* is configurable per deployment:

    file   — JSONL on the local filesystem with size-based rotation.
             Suitable for local dev and for containers where a sidecar
             scrapes the file (Fluent Bit / Vector / CloudWatch Agent).
    stdout — JSONL to stdout. Cloud-native default — the container runtime
             captures it and ships to whatever sink the platform provides
             (CloudWatch, GCP Logging, Loki, Datadog, etc.). No local disk.
    both   — Fan out to file AND stdout. Useful for migration windows.

Selected by AUDIT_SINK in settings. Backward-compatible default is "file".

Operators with bespoke needs (S3, Kafka, Splunk HEC, ...) can subclass
AuditSink and instantiate AuditLogger with their sink directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from abc import ABC, abstractmethod
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable

logger = logging.getLogger(__name__)


class AuditSink(ABC):
    """Abstract base — implement write() and (optionally) close()."""

    @abstractmethod
    async def write(self, record: Dict[str, Any]) -> None:
        ...

    async def close(self) -> None:
        return None


class FileSink(AuditSink):
    """
    JSONL file sink with size-based rotation.

    Uses logging.handlers.RotatingFileHandler under the hood for proven
    rename-on-rotate behavior; an asyncio.Lock serializes coroutine writes
    so concurrent records don't interleave on a partial line boundary.
    """

    def __init__(self, path: str, max_bytes: int = 50_000_000, backup_count: int = 5):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        # Private logger so app logging config never reroutes audit data.
        self._logger = logging.getLogger(f"audit.file.{id(self)}")
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        self._handler = RotatingFileHandler(
            str(self._path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(self._handler)

    async def write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, default=str)
        async with self._lock:
            try:
                self._logger.info(line)
            except OSError as exc:
                logger.error("FileSink write failed (%s): %s", self._path, exc)

    async def close(self) -> None:
        try:
            self._handler.close()
            self._logger.removeHandler(self._handler)
        except Exception:
            pass


class StdoutSink(AuditSink):
    """
    JSONL to stdout — relies on the container runtime / supervisor to
    capture and forward. No file I/O, no rotation needed.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, default=str)
        async with self._lock:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except OSError as exc:
                logger.error("StdoutSink write failed: %s", exc)


class CompositeSink(AuditSink):
    """
    Fan out to multiple sinks. A failure in one sink is logged but never
    blocks delivery to the others — audit must be best-effort durable
    across destinations.
    """

    def __init__(self, *sinks: AuditSink) -> None:
        if not sinks:
            raise ValueError("CompositeSink requires at least one child sink")
        self._sinks: tuple[AuditSink, ...] = sinks

    async def write(self, record: Dict[str, Any]) -> None:
        for sink in self._sinks:
            try:
                await sink.write(record)
            except Exception as exc:
                logger.error("Audit sink %s failed: %s", type(sink).__name__, exc)

    async def close(self) -> None:
        for sink in self._sinks:
            try:
                await sink.close()
            except Exception:
                pass

    @property
    def children(self) -> Iterable[AuditSink]:
        return self._sinks


def build_sink_from_settings() -> AuditSink:
    """Construct the configured AuditSink. Defaults to FileSink for backward compat."""
    from core.settings import get_settings

    s = get_settings()
    mode = getattr(s, "AUDIT_SINK", "file")

    if mode == "stdout":
        return StdoutSink()

    file_sink = FileSink(
        s.AUDIT_LOG_FILE,
        max_bytes=getattr(s, "AUDIT_FILE_MAX_BYTES", 50_000_000),
        backup_count=getattr(s, "AUDIT_FILE_BACKUP_COUNT", 5),
    )
    if mode == "both":
        return CompositeSink(file_sink, StdoutSink())
    return file_sink
