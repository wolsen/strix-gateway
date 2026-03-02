# FILE: tests/unit/test_cli_svc.py
"""Tests for the SVC CLI command (REST API path)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from apollo_gateway.cli.main import app

runner = CliRunner()


class TestSvcRun:
    def test_requires_subsystem(self):
        result = runner.invoke(app, ["svc", "run", "svcinfo lssystem"])
        # Missing --subsystem should trigger an error
        assert result.exit_code != 0

    def test_prints_stdout_and_exits_0(self):
        mock_svc_run = MagicMock(
            return_value={"stdout": "output\n", "stderr": "", "exit_code": 0}
        )
        with patch("apollo_gateway.cli.client.ApolloClient.svc_run", mock_svc_run):
            result = runner.invoke(
                app,
                ["svc", "run", "--subsystem", "svc-a", "svcinfo lssystem"],
            )
        mock_svc_run.assert_called_once_with("svc-a", "svcinfo lssystem")
        assert result.exit_code == 0
        assert "output" in result.output

    def test_exits_with_facade_exit_code(self):
        mock_svc_run = MagicMock(
            return_value={"stdout": "", "stderr": "not found\n", "exit_code": 1}
        )
        with patch("apollo_gateway.cli.client.ApolloClient.svc_run", mock_svc_run):
            result = runner.invoke(
                app,
                ["svc", "run", "--subsystem", "svc-a", "svcinfo badcmd"],
            )
        assert result.exit_code == 1
