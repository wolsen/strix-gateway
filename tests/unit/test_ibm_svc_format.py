# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for apollo_gateway.personalities.svc.format."""

from __future__ import annotations

import pytest

from apollo_gateway.personalities.svc.format import format_delim, format_table


# ---------------------------------------------------------------------------
# format_table
# ---------------------------------------------------------------------------

class TestFormatTable:
    def test_empty_list_returns_empty_string(self):
        assert format_table([]) == ""

    def test_single_row_with_header(self):
        rows = [{"id": "1", "name": "vol1", "status": "online"}]
        output = format_table(rows)
        lines = output.splitlines()
        assert len(lines) == 2
        assert lines[0] == "id\tname\tstatus"
        assert lines[1] == "1\tvol1\tonline"

    def test_multiple_rows(self):
        rows = [
            {"id": "1", "name": "vol1"},
            {"id": "2", "name": "vol2"},
        ]
        output = format_table(rows)
        lines = output.splitlines()
        assert lines[0] == "id\tname"
        assert lines[1] == "1\tvol1"
        assert lines[2] == "2\tvol2"

    def test_column_order_follows_first_dict(self):
        rows = [{"z": "3", "a": "1", "m": "2"}]
        header = format_table(rows).splitlines()[0]
        assert header == "z\ta\tm"

    def test_missing_key_in_later_row_emits_empty_cell(self):
        rows = [
            {"id": "1", "name": "vol1", "extra": "yes"},
            {"id": "2", "name": "vol2"},          # "extra" absent
        ]
        lines = format_table(rows).splitlines()
        assert lines[2] == "2\tvol2\t"

    def test_non_string_values_stringified(self):
        rows = [{"id": 42, "size": 1024}]
        lines = format_table(rows).splitlines()
        assert lines[1] == "42\t1024"


# ---------------------------------------------------------------------------
# format_delim
# ---------------------------------------------------------------------------

class TestFormatDelim:
    def test_default_delim_exclamation(self):
        fields = {"id": "abc", "name": "vol1"}
        output = format_delim(fields)
        assert output == "id!abc\nname!vol1"

    def test_custom_delim(self):
        fields = {"id": "abc"}
        assert format_delim(fields, delim=":") == "id:abc"

    def test_pipe_delim(self):
        fields = {"a": "1", "b": "2"}
        assert format_delim(fields, delim="|") == "a|1\nb|2"

    def test_empty_dict(self):
        assert format_delim({}) == ""

    def test_value_with_spaces(self):
        fields = {"name": "my volume"}
        assert format_delim(fields) == "name!my volume"

    def test_preserves_insertion_order(self):
        fields = {"z": "3", "a": "1"}
        lines = format_delim(fields).splitlines()
        assert lines[0].startswith("z!")
        assert lines[1].startswith("a!")
