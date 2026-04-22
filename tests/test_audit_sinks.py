"""
tests/test_audit_sinks.py — Coverage for the pluggable audit sink layer (G8).

Audit is the only durable record of who-did-what against Maximo through this
service. The sink layer is the seam between record() and the storage; this
file pins:
  - FileSink rotates at the configured byte budget and keeps the configured
    backup count
  - StdoutSink emits JSONL to stdout
  - CompositeSink fans out and isolates per-sink failures
  - build_sink_from_settings() respects AUDIT_SINK
  - AuditLogger.query() returns [] when no file-backed sink is configured
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List

import pytest

from core.audit_sinks import (
    AuditSink,
    CompositeSink,
    FileSink,
    StdoutSink,
)


def _run(coro):
    return asyncio.run(coro)


# ── FileSink ────────────────────────────────────────────────────────────────

class TestFileSink:
    def test_writes_jsonl_line(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        sink = FileSink(str(path))
        try:
            _run(sink.write({"tool": "list_assets", "user_id": "alice"}))
        finally:
            _run(sink.close())

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec == {"tool": "list_assets", "user_id": "alice"}

    def test_writes_multiple_records(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        sink = FileSink(str(path))
        try:
            for i in range(5):
                _run(sink.write({"i": i}))
        finally:
            _run(sink.close())

        lines = path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(l)["i"] for l in lines] == [0, 1, 2, 3, 4]

    def test_creates_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "audit.jsonl"
        sink = FileSink(str(path))
        try:
            _run(sink.write({"x": 1}))
        finally:
            _run(sink.close())
        assert path.exists()

    def test_rotates_at_max_bytes(self, tmp_path):
        """Write enough bytes to trigger rotation; verify backup files appear
        and the live file gets recycled."""
        path = tmp_path / "audit.jsonl"
        # Tiny budget so a few records force rotation.
        sink = FileSink(str(path), max_bytes=200, backup_count=3)
        try:
            for i in range(50):
                _run(sink.write({"i": i, "pad": "x" * 50}))
        finally:
            _run(sink.close())

        # Backup files use the standard logging.handlers naming: audit.jsonl.1, .2, ...
        backups = sorted(p.name for p in tmp_path.iterdir())
        rotated = [b for b in backups if b.startswith("audit.jsonl.")]
        assert rotated, f"expected rotated backups, got {backups}"
        # backup_count=3 caps the retained backups at 3.
        assert len(rotated) <= 3

    def test_serializes_non_json_values_via_default_str(self, tmp_path):
        """`json.dumps(..., default=str)` lets datetime / Path / Decimal pass through."""
        from datetime import datetime, timezone
        path = tmp_path / "audit.jsonl"
        sink = FileSink(str(path))
        try:
            _run(sink.write({
                "ts": datetime(2026, 4, 17, tzinfo=timezone.utc),
                "p": Path("/tmp/x"),
            }))
        finally:
            _run(sink.close())

        rec = json.loads(path.read_text(encoding="utf-8"))
        assert "2026-04-17" in rec["ts"]
        assert "x" in rec["p"]

    def test_logger_does_not_propagate_to_root(self, tmp_path):
        """Audit data must NOT route through the application's root logger
        (otherwise it ends up in stdout / logfiles tagged as INFO)."""
        path = tmp_path / "audit.jsonl"
        sink = FileSink(str(path))
        try:
            assert sink._logger.propagate is False
        finally:
            _run(sink.close())


# ── StdoutSink ──────────────────────────────────────────────────────────────

class TestStdoutSink:
    def test_writes_jsonl_to_stdout(self, capsys):
        sink = StdoutSink()
        _run(sink.write({"tool": "list_assets"}))
        captured = capsys.readouterr()
        assert captured.out.strip() == json.dumps({"tool": "list_assets"})

    def test_each_write_is_newline_delimited(self, capsys):
        sink = StdoutSink()
        _run(sink.write({"i": 1}))
        _run(sink.write({"i": 2}))
        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        assert [json.loads(l)["i"] for l in lines] == [1, 2]


# ── CompositeSink ───────────────────────────────────────────────────────────

class _RecordingSink(AuditSink):
    def __init__(self, *, raises: bool = False):
        self.records: List[Dict[str, Any]] = []
        self.closed = False
        self._raises = raises

    async def write(self, record):
        if self._raises:
            raise RuntimeError("simulated sink failure")
        self.records.append(record)

    async def close(self):
        self.closed = True


class TestCompositeSink:
    def test_requires_at_least_one_child(self):
        with pytest.raises(ValueError, match="at least one"):
            CompositeSink()

    def test_fans_out_to_all_children(self):
        a, b, c = _RecordingSink(), _RecordingSink(), _RecordingSink()
        comp = CompositeSink(a, b, c)
        _run(comp.write({"x": 1}))
        for s in (a, b, c):
            assert s.records == [{"x": 1}]

    def test_failure_in_one_does_not_block_others(self, caplog):
        good_a = _RecordingSink()
        bad = _RecordingSink(raises=True)
        good_b = _RecordingSink()
        comp = CompositeSink(good_a, bad, good_b)
        _run(comp.write({"x": 1}))
        # Both healthy sinks still received the record.
        assert good_a.records == [{"x": 1}]
        assert good_b.records == [{"x": 1}]
        # Failure was logged, not raised.
        assert any("Audit sink" in m for m in caplog.messages)

    def test_close_cascades_to_children(self):
        a, b = _RecordingSink(), _RecordingSink()
        comp = CompositeSink(a, b)
        _run(comp.close())
        assert a.closed and b.closed

    def test_close_isolates_per_child_failures(self):
        class _BadClose(_RecordingSink):
            async def close(self):
                raise RuntimeError("close boom")
        good = _RecordingSink()
        bad = _BadClose()
        comp = CompositeSink(bad, good)
        # Should not raise, and the healthy sink still gets closed.
        _run(comp.close())
        assert good.closed is True

    def test_children_property_returns_originals(self):
        a, b = _RecordingSink(), _RecordingSink()
        comp = CompositeSink(a, b)
        assert tuple(comp.children) == (a, b)


# ── build_sink_from_settings ────────────────────────────────────────────────

@pytest.fixture
def fake_settings(monkeypatch, tmp_path):
    state = SimpleNamespace(
        AUDIT_SINK="file",
        AUDIT_LOG_FILE=str(tmp_path / "audit.jsonl"),
        AUDIT_FILE_MAX_BYTES=50_000_000,
        AUDIT_FILE_BACKUP_COUNT=5,
    )
    fake = ModuleType("core.settings")
    fake.get_settings = lambda: state  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.settings", fake)
    return state


class TestBuildSinkFromSettings:
    def test_default_is_file_sink(self, fake_settings):
        from core.audit_sinks import build_sink_from_settings
        sink = build_sink_from_settings()
        try:
            assert isinstance(sink, FileSink)
        finally:
            _run(sink.close())

    def test_stdout_mode_returns_stdout_sink(self, fake_settings):
        from core.audit_sinks import build_sink_from_settings
        fake_settings.AUDIT_SINK = "stdout"
        sink = build_sink_from_settings()
        assert isinstance(sink, StdoutSink)

    def test_both_mode_returns_composite_with_file_then_stdout(self, fake_settings):
        from core.audit_sinks import build_sink_from_settings
        fake_settings.AUDIT_SINK = "both"
        sink = build_sink_from_settings()
        try:
            assert isinstance(sink, CompositeSink)
            children = list(sink.children)
            assert isinstance(children[0], FileSink)
            assert isinstance(children[1], StdoutSink)
        finally:
            _run(sink.close())

    def test_file_sink_uses_settings_byte_budget(self, fake_settings, tmp_path):
        from core.audit_sinks import build_sink_from_settings
        fake_settings.AUDIT_FILE_MAX_BYTES = 12345
        fake_settings.AUDIT_FILE_BACKUP_COUNT = 7
        sink = build_sink_from_settings()
        try:
            assert sink._handler.maxBytes == 12345
            assert sink._handler.backupCount == 7
        finally:
            _run(sink.close())


# ── AuditLogger integration with sinks (the part that matters for query()) ──

class TestAuditLoggerQuery:
    def test_query_returns_empty_when_no_file_backed_sink(self, fake_settings):
        from core.audit import AuditLogger
        logger = AuditLogger(sink=StdoutSink())
        assert _run(logger.query()) == []

    def test_query_reads_file_backed_records(self, fake_settings, tmp_path):
        from core.audit import AuditLogger
        path = tmp_path / "audit.jsonl"
        logger = AuditLogger(str(path))
        try:
            _run(logger.record(
                tool_name="list_assets", input_params={"siteid": "B"},
                result={"member": [], "totalCount": 0}, user_id="alice",
            ))
            _run(logger.record(
                tool_name="get_asset", input_params={"assetnum": "P-1"},
                result={"data": {"assetnum": "P-1"}}, user_id="bob",
            ))
        finally:
            _run(logger._sink.close())

        records = _run(logger.query())
        assert len(records) == 2
        # Newest first.
        assert records[0]["tool"] == "get_asset"
        assert records[0]["user_id"] == "bob"
        assert records[1]["tool"] == "list_assets"

    def test_query_filters_by_tool_name_substring(self, fake_settings, tmp_path):
        from core.audit import AuditLogger
        path = tmp_path / "audit.jsonl"
        logger = AuditLogger(str(path))
        try:
            for tool in ("list_assets", "get_asset", "create_workorder"):
                _run(logger.record(tool_name=tool, input_params={}, result={}))
        finally:
            _run(logger._sink.close())

        records = _run(logger.query(tool_name="asset"))
        names = {r["tool"] for r in records}
        assert names == {"list_assets", "get_asset"}

    def test_query_filters_by_user_id_exact(self, fake_settings, tmp_path):
        from core.audit import AuditLogger
        path = tmp_path / "audit.jsonl"
        logger = AuditLogger(str(path))
        try:
            _run(logger.record(tool_name="x", input_params={}, result={}, user_id="alice"))
            _run(logger.record(tool_name="x", input_params={}, result={}, user_id="bob"))
        finally:
            _run(logger._sink.close())

        records = _run(logger.query(user_id="alice"))
        assert len(records) == 1
        assert records[0]["user_id"] == "alice"

    def test_query_respects_limit(self, fake_settings, tmp_path):
        from core.audit import AuditLogger
        path = tmp_path / "audit.jsonl"
        logger = AuditLogger(str(path))
        try:
            for i in range(10):
                _run(logger.record(tool_name=f"t{i}", input_params={}, result={}))
        finally:
            _run(logger._sink.close())

        records = _run(logger.query(limit=3))
        assert len(records) == 3

    def test_query_returns_empty_when_log_file_missing(self, fake_settings, tmp_path):
        from core.audit import AuditLogger
        # File path that has not been written to yet.
        path = tmp_path / "no_such_audit.jsonl"
        logger = AuditLogger(str(path))
        # Don't write anything; close() removes nothing.
        assert _run(logger.query()) == []


class TestAuditLoggerSink:
    def test_log_file_only_constructor_builds_file_sink(self, tmp_path):
        from core.audit import AuditLogger
        path = tmp_path / "audit.jsonl"
        logger = AuditLogger(str(path))
        try:
            assert isinstance(logger._sink, FileSink)
        finally:
            _run(logger._sink.close())

    def test_sink_only_constructor(self):
        from core.audit import AuditLogger
        sink = StdoutSink()
        logger = AuditLogger(sink=sink)
        assert logger._sink is sink
        assert logger._log_file is None

    def test_neither_arg_rejected(self):
        from core.audit import AuditLogger
        with pytest.raises(ValueError, match="log_file or sink"):
            AuditLogger()

    def test_record_includes_env_field(self, fake_settings, tmp_path):
        """G14: every audit record must be stamped with MAXIMO_ENV."""
        from core.audit import AuditLogger
        fake_settings.MAXIMO_ENV = "staging"
        path = tmp_path / "audit.jsonl"
        logger = AuditLogger(str(path))
        try:
            _run(logger.record(tool_name="x", input_params={}, result={}))
        finally:
            _run(logger._sink.close())

        rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert rec["env"] == "staging"

    def test_record_redacts_sensitive_input_fields(self, fake_settings, tmp_path):
        from core.audit import AuditLogger
        path = tmp_path / "audit.jsonl"
        logger = AuditLogger(str(path))
        try:
            _run(logger.record(
                tool_name="x",
                input_params={"username": "alice", "password": "p455w0rd",
                              "api_key": "AKIA...", "siteid": "BEDFORD"},
                result={},
            ))
        finally:
            _run(logger._sink.close())

        rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert rec["input"]["password"] == "***REDACTED***"
        assert rec["input"]["api_key"] == "***REDACTED***"
        assert rec["input"]["username"] == "alice"
        assert rec["input"]["siteid"] == "BEDFORD"

    def test_record_swallows_sink_exceptions(self, fake_settings, caplog):
        """A broken sink must not bubble exceptions back into tool execution."""
        from core.audit import AuditLogger

        class _BoomSink(AuditSink):
            async def write(self, record):
                raise RuntimeError("sink down")

        logger = AuditLogger(sink=_BoomSink())
        # Must not raise.
        _run(logger.record(tool_name="x", input_params={}, result={}))
        assert any("Audit sink write failed" in m for m in caplog.messages)
