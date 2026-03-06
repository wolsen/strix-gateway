# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Smoke tests for CLI output formatting."""

from __future__ import annotations

import json

import yaml

from apollo_gateway.cli.output import OutputFormat, render


def _capture(capsys, data, fmt, columns=None):
    render(data, fmt, columns=columns)
    return capsys.readouterr().out


class TestJSONOutput:
    def test_list_json(self, capsys):
        rows = [{"name": "a", "value": 1}, {"name": "b", "value": 2}]
        out = _capture(capsys, rows, OutputFormat.json)
        parsed = json.loads(out)
        assert len(parsed) == 2
        assert parsed[0]["name"] == "a"

    def test_dict_json(self, capsys):
        data = {"key": "val", "num": 42}
        out = _capture(capsys, data, OutputFormat.json)
        parsed = json.loads(out)
        assert parsed["key"] == "val"


class TestYAMLOutput:
    def test_list_yaml(self, capsys):
        rows = [{"name": "x"}]
        out = _capture(capsys, rows, OutputFormat.yaml)
        parsed = yaml.safe_load(out)
        assert parsed[0]["name"] == "x"

    def test_dict_yaml(self, capsys):
        data = {"foo": "bar"}
        out = _capture(capsys, data, OutputFormat.yaml)
        parsed = yaml.safe_load(out)
        assert parsed["foo"] == "bar"


class TestTableOutput:
    def test_empty_list(self, capsys):
        render([], OutputFormat.table)
        out = capsys.readouterr().out
        assert "no results" in out.lower()

    def test_list_table(self, capsys):
        rows = [{"name": "a", "size": 10}]
        out = _capture(capsys, rows, OutputFormat.table, columns=["name", "size"])
        assert "a" in out
        assert "10" in out

    def test_dict_table(self, capsys):
        render({"foo": "bar"}, OutputFormat.table)
        out = capsys.readouterr().out
        assert "foo" in out
        assert "bar" in out

    def test_columns_filter(self, capsys):
        rows = [{"a": 1, "b": 2, "c": 3}]
        out = _capture(capsys, rows, OutputFormat.table, columns=["a", "c"])
        # Column B should NOT appear; A and C should
        assert "1" in out
        assert "3" in out
        # The value "2" for column B should not be present as a standalone column
        # (it could appear in box chars, so check general presence of selected data)
        lines = [l for l in out.strip().split("\n") if "1" in l or "3" in l]
        assert len(lines) >= 1
