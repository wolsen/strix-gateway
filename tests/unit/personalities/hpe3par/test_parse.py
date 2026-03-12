# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for strix_gateway.personalities.hpe3par.parse."""

from __future__ import annotations

import pytest

from strix_gateway.personalities.hpe3par.errors import (
    Hpe3parInvalidArgError,
    Hpe3parUnknownCommandError,
)
from strix_gateway.personalities.hpe3par.parse import (
    ParsedCommand,
    parse_command,
    optional_flag,
    require_flag,
)


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------

class TestParseCommand:
    def test_simple_command(self):
        pc = parse_command("showsys")
        assert pc.command == "showsys"
        assert pc.positional == []
        assert pc.flags == {}
        assert pc.boolean_flags == set()

    def test_command_with_positional(self):
        pc = parse_command("showvv myvol")
        assert pc.command == "showvv"
        assert pc.positional == ["myvol"]

    def test_command_with_flag(self):
        pc = parse_command("showvlun -host myhost")
        assert pc.command == "showvlun"
        assert pc.flags["host"] == "myhost"

    def test_command_with_boolean_flag(self):
        pc = parse_command("createvv -tpvv vol1 cpg0 1024")
        assert pc.command == "createvv"
        assert "tpvv" in pc.boolean_flags
        assert pc.positional == ["vol1", "cpg0", "1024"]

    def test_removevv_force(self):
        pc = parse_command("removevv -f vol1")
        assert pc.command == "removevv"
        assert "f" in pc.boolean_flags
        assert pc.positional == ["vol1"]

    def test_createvv_multiple_positionals(self):
        pc = parse_command("createvv myvol cpg0 2048")
        assert pc.command == "createvv"
        assert pc.positional == ["myvol", "cpg0", "2048"]

    def test_createhost_with_initiator(self):
        pc = parse_command("createhost myhost iqn.2005-03.com.example:test")
        assert pc.command == "createhost"
        assert pc.positional == ["myhost", "iqn.2005-03.com.example:test"]

    def test_sethost_add(self):
        pc = parse_command("sethost -add iqn.example myhost")
        assert pc.command == "sethost"
        assert "add" in pc.boolean_flags
        assert pc.positional == ["iqn.example", "myhost"]

    def test_createvlun(self):
        pc = parse_command("createvlun vol1 0 myhost")
        assert pc.command == "createvlun"
        assert pc.positional == ["vol1", "0", "myhost"]

    def test_case_insensitive_command(self):
        pc = parse_command("ShowSys")
        assert pc.command == "showsys"

    def test_unknown_command_raises(self):
        with pytest.raises(Hpe3parUnknownCommandError):
            parse_command("badcmd")

    def test_empty_command_raises(self):
        with pytest.raises(Hpe3parInvalidArgError):
            parse_command("")

    def test_showport_with_type_flag(self):
        pc = parse_command("showport -type iscsi")
        assert pc.command == "showport"
        assert pc.flags["type"] == "iscsi"

    def test_growvv(self):
        pc = parse_command("growvv vol1 512")
        assert pc.command == "growvv"
        assert pc.positional == ["vol1", "512"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestRequireFlag:
    def test_returns_value(self):
        pc = ParsedCommand(
            command="showvlun",
            flags={"host": "myhost"},
            boolean_flags=set(),
            positional=[],
        )
        assert require_flag(pc, "host") == "myhost"

    def test_missing_flag_raises(self):
        pc = ParsedCommand(
            command="showvlun",
            flags={},
            boolean_flags=set(),
            positional=[],
        )
        with pytest.raises(Hpe3parInvalidArgError):
            require_flag(pc, "host")


class TestOptionalFlag:
    def test_returns_value(self):
        pc = ParsedCommand(
            command="showport",
            flags={"type": "fc"},
            boolean_flags=set(),
            positional=[],
        )
        assert optional_flag(pc, "type", "all") == "fc"

    def test_returns_default(self):
        pc = ParsedCommand(
            command="showport",
            flags={},
            boolean_flags=set(),
            positional=[],
        )
        assert optional_flag(pc, "type", "all") == "all"
