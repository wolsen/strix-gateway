# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for SVC CLI behavior in SSH-only façade mode."""

from __future__ import annotations

from typer.testing import CliRunner

from apollo_gateway.cli.main import app

runner = CliRunner()


class TestSvcCli:
    def test_svc_run_command_removed(self):
        result = runner.invoke(app, ["svc", "run", "svcinfo lssystem"])
        assert result.exit_code != 0
        assert "No such command" in result.output
