# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for strix_gateway.personalities.svc.audit and shell audit integration.

Coverage
--------
* redact_argv()       — all sensitive flag patterns + edge cases
* parse_ssh_connection() — env-var parsing
* InvocationRecord    — construction and JSON serialisation
* _CountingWriter     — byte counting accuracy
* SvcAuditLogger      — file creation, JSON Lines format, human-readable format,
                         graceful degradation on bad log dir
* audited_dispatch() — end-to-end: dispatch produces a JSON Lines record with
                         correct fields and the exit_code / stdout_len values
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from strix_gateway.personalities.svc.audit import (
    SENSITIVE_FLAGS,
    InvocationRecord,
    SvcAuditLogger,
    _CountingWriter,
    _format_human,
    parse_ssh_connection,
    redact_argv,
)
from strix_gateway.personalities.svc.audit import audited_dispatch
from strix_gateway.personalities.svc.handlers import SvcContext
from strix_gateway.core.db import Pool, Array, init_db, get_session_factory
from strix_gateway.core.personas import merge_profile

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(**overrides) -> InvocationRecord:
    defaults = dict(
        ts="2026-02-24T00:00:00+00:00",
        req_id="aaaabbbb-cccc-dddd-eeee-ffffffffffff",
        remote_user="svc",
        remote_addr="10.0.0.1",
        remote_port="12345",
        command_raw="svcinfo lssystem",
        argv=["svcinfo", "lssystem"],
        duration_ms=12,
        exit_code=0,
        stdout_len=150,
        stderr_len=0,
    )
    defaults.update(overrides)
    return InvocationRecord(**defaults)


# ---------------------------------------------------------------------------
# redact_argv
# ---------------------------------------------------------------------------

class TestRedactArgv:
    def test_no_sensitive_flags_unchanged(self):
        argv = ["svctask", "mkvdisk", "-name", "vol1", "-size", "10", "-unit", "gb"]
        assert redact_argv(argv) == argv

    def test_password_flag_value_redacted(self):
        result = redact_argv(["login", "-password", "secret123"])
        assert result == ["login", "-password", "***"]

    def test_double_dash_flag_redacted(self):
        result = redact_argv(["cmd", "--password", "s3cr3t"])
        assert result == ["cmd", "--password", "***"]

    def test_chapsecret_redacted(self):
        result = redact_argv(["-chapsecret", "chapval"])
        assert result == ["-chapsecret", "***"]

    def test_chap_secret_underscore_redacted(self):
        result = redact_argv(["-chap_secret", "chapval"])
        assert result == ["-chap_secret", "***"]

    def test_token_redacted(self):
        result = redact_argv(["-token", "tok123"])
        assert result == ["-token", "***"]

    def test_apikey_redacted(self):
        result = redact_argv(["-apikey", "key-abc"])
        assert result == ["-apikey", "***"]

    def test_api_key_underscore_redacted(self):
        result = redact_argv(["-api_key", "key-abc"])
        assert result == ["-api_key", "***"]

    def test_secret_redacted(self):
        result = redact_argv(["-secret", "mysecret"])
        assert result == ["-secret", "***"]

    def test_key_redacted(self):
        result = redact_argv(["-key", "mykey"])
        assert result == ["-key", "***"]

    def test_case_insensitive(self):
        result = redact_argv(["-PASSWORD", "val"])
        assert result == ["-PASSWORD", "***"]

    def test_boolean_sensitive_flag_no_following_value(self):
        # "-password" as boolean (next token is another flag) → not redacted
        result = redact_argv(["-password", "-verbose"])
        assert result == ["-password", "-verbose"]

    def test_value_after_sensitive_is_another_flag_not_redacted(self):
        # Next token starts with "-" so it's a flag, not a value
        result = redact_argv(["-password", "-name", "vol"])
        assert result == ["-password", "-name", "vol"]

    def test_sensitive_flag_at_end_no_crash(self):
        result = redact_argv(["-password"])
        assert result == ["-password"]

    def test_empty_argv(self):
        assert redact_argv([]) == []

    def test_non_sensitive_flag_around_sensitive_preserved(self):
        result = redact_argv(["-name", "vol1", "-password", "pw", "-size", "10"])
        assert result == ["-name", "vol1", "-password", "***", "-size", "10"]

    def test_multiple_sensitive_flags_both_redacted(self):
        result = redact_argv(["-password", "pw1", "-secret", "sec1"])
        assert result == ["-password", "***", "-secret", "***"]

    def test_positional_args_preserved(self):
        result = redact_argv(["lsvdisk", "vol1"])
        assert result == ["lsvdisk", "vol1"]

    def test_sensitive_flags_set_is_non_empty(self):
        assert len(SENSITIVE_FLAGS) >= 5

    def test_privatekey_redacted(self):
        result = redact_argv(["-privatekey", "/etc/id_rsa"])
        assert result == ["-privatekey", "***"]

    def test_passwd_redacted(self):
        result = redact_argv(["-passwd", "abc"])
        assert result == ["-passwd", "***"]


# ---------------------------------------------------------------------------
# parse_ssh_connection
# ---------------------------------------------------------------------------

class TestParseSshConnection:
    def test_ssh_connection_set(self, monkeypatch):
        monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 54321 172.17.0.2 22")
        monkeypatch.delenv("SSH_CLIENT", raising=False)
        addr, port = parse_ssh_connection()
        assert addr == "10.0.0.1"
        assert port == "54321"

    def test_ssh_client_fallback(self, monkeypatch):
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
        monkeypatch.setenv("SSH_CLIENT", "192.168.1.1 9999 22")
        addr, port = parse_ssh_connection()
        assert addr == "192.168.1.1"
        assert port == "9999"

    def test_neither_set_returns_none(self, monkeypatch):
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
        monkeypatch.delenv("SSH_CLIENT", raising=False)
        addr, port = parse_ssh_connection()
        assert addr is None
        assert port is None

    def test_ssh_connection_takes_priority(self, monkeypatch):
        monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 111 5.6.7.8 22")
        monkeypatch.setenv("SSH_CLIENT", "9.9.9.9 999 22")
        addr, port = parse_ssh_connection()
        assert addr == "1.2.3.4"

    def test_malformed_ssh_connection_returns_none(self, monkeypatch):
        # Too few parts
        monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1")
        monkeypatch.delenv("SSH_CLIENT", raising=False)
        addr, port = parse_ssh_connection()
        assert addr is None
        assert port is None


# ---------------------------------------------------------------------------
# _CountingWriter
# ---------------------------------------------------------------------------

class TestCountingWriter:
    def test_counts_ascii_bytes(self):
        buf = io.StringIO()
        cw = _CountingWriter(buf)
        cw.write("hello")
        assert cw.byte_count == 5
        assert buf.getvalue() == "hello"

    def test_counts_utf8_multibyte(self):
        buf = io.StringIO()
        cw = _CountingWriter(buf)
        # "é" is 2 bytes in UTF-8
        cw.write("café")
        assert cw.byte_count == 5  # c(1) + a(1) + f(1) + é(2) = 5

    def test_multiple_writes_accumulate(self):
        buf = io.StringIO()
        cw = _CountingWriter(buf)
        cw.write("abc")
        cw.write("de")
        assert cw.byte_count == 5

    def test_empty_write(self):
        buf = io.StringIO()
        cw = _CountingWriter(buf)
        cw.write("")
        assert cw.byte_count == 0

    def test_writelines(self):
        buf = io.StringIO()
        cw = _CountingWriter(buf)
        cw.writelines(["ab", "cd"])
        assert cw.byte_count == 4
        assert buf.getvalue() == "abcd"

    def test_passthrough_content(self):
        buf = io.StringIO()
        cw = _CountingWriter(buf)
        cw.write("test content")
        assert buf.getvalue() == "test content"

    def test_flush_delegates(self):
        class _FakeBuf:
            flushed = False
            byte_count = 0
            def write(self, s): return len(s)
            def flush(self): self.flushed = True
        fb = _FakeBuf()
        cw = _CountingWriter(fb)
        cw.flush()
        assert fb.flushed


# ---------------------------------------------------------------------------
# InvocationRecord
# ---------------------------------------------------------------------------

class TestInvocationRecord:
    def test_json_serialisable(self):
        record = _make_record()
        obj = json.loads(json.dumps(
            record.__dict__,  # dataclasses.asdict loses Optional type info so use __dict__
            default=str,
        ))
        assert obj["exit_code"] == 0
        assert obj["remote_addr"] == "10.0.0.1"

    def test_error_field_defaults_none(self):
        record = _make_record()
        assert record.error is None

    def test_error_field_set(self):
        record = _make_record(error="rejected: no SSH_ORIGINAL_COMMAND")
        assert record.error == "rejected: no SSH_ORIGINAL_COMMAND"

    def test_all_required_fields_present(self):
        record = _make_record()
        for field in (
            "ts", "req_id", "remote_user", "remote_addr", "remote_port",
            "command_raw", "argv", "duration_ms", "exit_code",
            "stdout_len", "stderr_len",
        ):
            assert hasattr(record, field)


# ---------------------------------------------------------------------------
# _format_human
# ---------------------------------------------------------------------------

class TestFormatHuman:
    def test_contains_key_fields(self):
        record = _make_record()
        line = _format_human(record)
        assert "svc@10.0.0.1:12345" in line
        assert "svcinfo lssystem" in line
        assert "exit=0" in line
        assert "12ms" in line
        assert "out=150B" in line
        assert "err=0B" in line

    def test_short_req_id_used(self):
        record = _make_record(req_id="abcd1234-5678-90ab-cdef-000000000000")
        line = _format_human(record)
        assert "[abcd1234]" in line

    def test_error_field_included(self):
        record = _make_record(error="rejected: no SSH_ORIGINAL_COMMAND", command_raw="")
        line = _format_human(record)
        assert "ERROR=rejected" in line

    def test_unknown_addr_shown(self):
        record = _make_record(remote_addr=None, remote_port=None)
        line = _format_human(record)
        assert "unknown" in line


# ---------------------------------------------------------------------------
# SvcAuditLogger
# ---------------------------------------------------------------------------

class TestSvcAuditLogger:
    def test_configure_creates_directory(self, tmp_path):
        log_dir = tmp_path / "audit_test"
        audit = SvcAuditLogger()
        audit.configure(log_dir=log_dir)
        assert log_dir.is_dir()

    def test_emit_writes_jsonl(self, tmp_path):
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)
        audit.emit(_make_record())
        jsonl_path = tmp_path / SvcAuditLogger.JSONL_NAME
        assert jsonl_path.exists()
        line = jsonl_path.read_text().strip()
        obj = json.loads(line)
        assert obj["exit_code"] == 0
        assert obj["remote_addr"] == "10.0.0.1"
        assert obj["command_raw"] == "svcinfo lssystem"

    def test_jsonl_has_all_fields(self, tmp_path):
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)
        audit.emit(_make_record())
        obj = json.loads((tmp_path / SvcAuditLogger.JSONL_NAME).read_text().strip())
        required = {
            "ts", "req_id", "remote_user", "remote_addr", "remote_port",
            "command_raw", "argv", "duration_ms", "exit_code",
            "stdout_len", "stderr_len",
        }
        assert required <= obj.keys()

    def test_emit_writes_human_log(self, tmp_path):
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)
        audit.emit(_make_record())
        text_path = tmp_path / SvcAuditLogger.TEXT_NAME
        assert text_path.exists()
        content = text_path.read_text()
        assert "svcinfo lssystem" in content
        assert "exit=0" in content

    def test_multiple_emits_append(self, tmp_path):
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)
        audit.emit(_make_record(command_raw="svcinfo lssystem"))
        audit.emit(_make_record(command_raw="svctask mkhost -name h1"))
        lines = (tmp_path / SvcAuditLogger.JSONL_NAME).read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["command_raw"] == "svcinfo lssystem"
        assert json.loads(lines[1])["command_raw"] == "svctask mkhost -name h1"

    def test_not_ready_before_configure(self, tmp_path):
        audit = SvcAuditLogger()
        # emit before configure — should not raise, should not write
        audit.emit(_make_record())
        assert not (tmp_path / SvcAuditLogger.JSONL_NAME).exists()

    def test_graceful_degradation_bad_dir(self):
        audit = SvcAuditLogger()
        # Use a path under /proc which is not writable
        audit.configure(log_dir=Path("/proc/1/fdinfo/nonexistent_audit"))
        assert not audit._ready
        # emit should not raise
        audit.emit(_make_record())

    def test_error_record_contains_error_field(self, tmp_path):
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)
        audit.emit(_make_record(error="rejected: no SSH_ORIGINAL_COMMAND", command_raw=""))
        obj = json.loads((tmp_path / SvcAuditLogger.JSONL_NAME).read_text().strip())
        assert obj["error"] == "rejected: no SSH_ORIGINAL_COMMAND"

    def test_sensitive_value_redacted_in_argv(self, tmp_path):
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)
        audit.emit(_make_record(
            command_raw="login -password secret",
            argv=redact_argv(["login", "-password", "secret"]),
        ))
        obj = json.loads((tmp_path / SvcAuditLogger.JSONL_NAME).read_text().strip())
        assert "***" in obj["argv"]
        assert "secret" not in obj["argv"]


# ---------------------------------------------------------------------------
# audited_dispatch integration
# ---------------------------------------------------------------------------

import json as _json


@pytest_asyncio.fixture
async def audit_ctx(tmp_path):
    """SvcContext with in-memory DB (default array + pool0 pre-created) + mock SPDK."""
    await init_db(TEST_DATABASE_URL)
    factory = get_session_factory()
    mock_spdk = MagicMock()
    mock_spdk.call = MagicMock(return_value=None)

    async with factory() as s:
        arr = Array(
            name="default",
            vendor="generic",
            profile="{}",
        )
        s.add(arr)
        await s.flush()
        p = Pool(name="pool0", backend_type="malloc", size_mb=10240, array_id=arr.id)
        s.add(p)
        await s.commit()
        arr_id = arr.id
        arr_name = arr.name
        arr_vendor = arr.vendor
        arr_profile = arr.profile

    profile = merge_profile(arr_vendor, _json.loads(arr_profile))
    async with factory() as session:
        yield SvcContext(
            session=session,
            spdk=mock_spdk,
            array_id=arr_id,
            array_name=arr_name,
            effective_profile=profile.model_dump(),
        ), tmp_path


class TestAuditedDispatch:
    async def test_produces_jsonl_record(self, audit_ctx):
        ctx, tmp_path = audit_ctx
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)

        exit_code = await audited_dispatch(
            "svcinfo lssystem",
            ctx,
            audit,
            remote_user="svc",
            remote_addr="10.0.0.1",
            remote_port="54321",
        )

        assert exit_code == 0
        jsonl_path = tmp_path / SvcAuditLogger.JSONL_NAME
        assert jsonl_path.exists()
        obj = json.loads(jsonl_path.read_text().strip())

        assert obj["command_raw"] == "svcinfo lssystem"
        assert obj["exit_code"] == 0
        assert obj["remote_user"] == "svc"
        assert obj["remote_addr"] == "10.0.0.1"
        assert obj["remote_port"] == "54321"
        assert obj["duration_ms"] >= 0
        assert obj["stdout_len"] > 0        # lssystem always emits output
        assert obj["stderr_len"] == 0
        assert obj["argv"] == ["svcinfo", "lssystem"]
        assert "req_id" in obj
        assert "ts" in obj

    async def test_error_command_produces_record_with_exit_1(self, audit_ctx):
        ctx, tmp_path = audit_ctx
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)

        exit_code = await audited_dispatch(
            "svcinfo lsvdisk nosuchvolume",
            ctx,
            audit,
        )

        assert exit_code == 1
        obj = json.loads((tmp_path / SvcAuditLogger.JSONL_NAME).read_text().strip())
        assert obj["exit_code"] == 1
        assert obj["stderr_len"] > 0    # error message was printed

    async def test_stdout_len_matches_actual_output(self, audit_ctx):
        ctx, tmp_path = audit_ctx
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)

        # Capture real stdout to measure independently
        buf = io.StringIO()
        real_stdout = sys.stdout
        sys.stdout = buf
        try:
            exit_code = await audited_dispatch("svcinfo lssystem", ctx, audit)
        finally:
            sys.stdout = real_stdout

        captured = buf.getvalue()
        obj = json.loads((tmp_path / SvcAuditLogger.JSONL_NAME).read_text().strip())
        assert obj["stdout_len"] == len(captured.encode("utf-8"))

    async def test_argv_redacted_in_record(self, audit_ctx):
        """A command containing a sensitive flag has its value redacted in the record."""
        ctx, tmp_path = audit_ctx
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)

        await audited_dispatch(
            "svctask mkvdisk -name vol1 -size 1 -unit gb -mdiskgrp pool0 -password hunter2",
            ctx,
            audit,
        )

        obj = json.loads((tmp_path / SvcAuditLogger.JSONL_NAME).read_text().strip())
        assert "hunter2" not in obj["argv"]
        assert "***" in obj["argv"]
        # command_raw is NOT redacted
        assert "hunter2" in obj["command_raw"]

    async def test_human_log_written(self, audit_ctx):
        ctx, tmp_path = audit_ctx
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)

        await audited_dispatch("svcinfo lssystem", ctx, audit,
                                remote_addr="127.0.0.1", remote_port="9999")

        text = (tmp_path / SvcAuditLogger.TEXT_NAME).read_text()
        assert "127.0.0.1:9999" in text
        assert "svcinfo lssystem" in text
        assert "exit=0" in text

    async def test_req_id_is_uuid4_format(self, audit_ctx):
        ctx, tmp_path = audit_ctx
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)

        await audited_dispatch("svcinfo lssystem", ctx, audit)

        obj = json.loads((tmp_path / SvcAuditLogger.JSONL_NAME).read_text().strip())
        import uuid
        # Should not raise
        parsed = uuid.UUID(obj["req_id"])
        assert parsed.version == 4

    async def test_duration_ms_is_non_negative_int(self, audit_ctx):
        ctx, tmp_path = audit_ctx
        audit = SvcAuditLogger()
        audit.configure(log_dir=tmp_path)

        await audited_dispatch("svcinfo lssystem", ctx, audit)

        obj = json.loads((tmp_path / SvcAuditLogger.JSONL_NAME).read_text().strip())
        assert isinstance(obj["duration_ms"], int)
        assert obj["duration_ms"] >= 0
