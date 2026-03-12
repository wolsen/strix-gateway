# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for strix_gateway.personalities.hpe3par.format."""

from __future__ import annotations

from strix_gateway.personalities.hpe3par.format import format_detail, format_table


class TestFormatTable:
    def test_empty_list_returns_empty(self):
        assert format_table([]) == ""

    def test_single_row(self):
        rows = [{"Id": "0", "Name": "vol1", "Size_MiB": "1024"}]
        output = format_table(rows)
        lines = output.splitlines()
        # Header + separator + 1 data row
        assert len(lines) == 3
        assert "Id" in lines[0]
        assert "Name" in lines[0]
        assert "---" in lines[1] or "- " in lines[1]
        assert "vol1" in lines[2]

    def test_multiple_rows(self):
        rows = [
            {"Id": "0", "Name": "vol1"},
            {"Id": "1", "Name": "vol2"},
        ]
        output = format_table(rows)
        lines = output.splitlines()
        assert len(lines) == 4  # header + separator + 2 data rows
        assert "vol1" in lines[2]
        assert "vol2" in lines[3]

    def test_column_alignment(self):
        rows = [
            {"Name": "short", "Value": "1"},
            {"Name": "a_longer_name", "Value": "2"},
        ]
        output = format_table(rows)
        lines = output.splitlines()
        # Columns are padded so header and data alignment matches
        header_parts = lines[0].split()
        assert "Name" in header_parts
        assert "Value" in header_parts


class TestFormatDetail:
    def test_basic_detail(self):
        fields = {"Name": "sys1", "Model": "3PAR"}
        output = format_detail(fields)
        assert "Name" in output
        assert "sys1" in output
        assert "Model" in output
        assert "3PAR" in output

    def test_detail_uses_colon_separator(self):
        fields = {"Key": "val"}
        output = format_detail(fields)
        assert ":" in output

    def test_empty_fields(self):
        assert format_detail({}) == ""
