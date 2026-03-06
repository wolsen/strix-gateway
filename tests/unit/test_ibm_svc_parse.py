# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for apollo_gateway.compat.ibm_svc.parse."""

from __future__ import annotations

import pytest

from apollo_gateway.compat.ibm_svc.errors import SvcInvalidArgError, SvcUnknownCommandError
from apollo_gateway.compat.ibm_svc.parse import (
    ParsedCommand,
    parse_ssh_command,
    require_flag,
    optional_flag,
)


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------

class TestParseSshCommand:
    def test_svcinfo_lssystem(self):
        pc = parse_ssh_command("svcinfo lssystem")
        assert pc.verb == "svcinfo"
        assert pc.subcommand == "lssystem"
        assert pc.raw_args == []
        assert pc.flags == {}
        assert pc.positional == []
        assert pc.delim is None

    def test_svctask_mkhost(self):
        pc = parse_ssh_command("svctask mkhost -name myhost")
        assert pc.verb == "svctask"
        assert pc.subcommand == "mkhost"
        assert pc.flags == {"name": "myhost"}
        assert pc.positional == []

    def test_svcinfo_lsvdisk_with_name(self):
        pc = parse_ssh_command("svcinfo lsvdisk vol1")
        assert pc.verb == "svcinfo"
        assert pc.subcommand == "lsvdisk"
        assert pc.positional == ["vol1"]

    def test_svcinfo_lsvdisk_with_delim(self):
        pc = parse_ssh_command("svcinfo lsvdisk vol1 -delim !")
        assert pc.delim == "!"
        assert pc.positional == ["vol1"]
        assert "delim" not in pc.flags

    def test_svctask_mkvdisk_full(self):
        pc = parse_ssh_command(
            "svctask mkvdisk -name vol1 -size 10 -unit gb -mdiskgrp pool0"
        )
        assert pc.verb == "svctask"
        assert pc.subcommand == "mkvdisk"
        assert pc.flags["name"] == "vol1"
        assert pc.flags["size"] == "10"
        assert pc.flags["unit"] == "gb"
        assert pc.flags["mdiskgrp"] == "pool0"

    def test_svctask_mkvdiskhostmap(self):
        pc = parse_ssh_command("svctask mkvdiskhostmap -host myhost myvdisk")
        assert pc.flags["host"] == "myhost"
        assert pc.positional == ["myvdisk"]

    def test_verb_case_insensitive(self):
        pc = parse_ssh_command("SVCINFO lssystem")
        assert pc.verb == "svcinfo"

    def test_subcommand_case_insensitive(self):
        pc = parse_ssh_command("svcinfo LSVDISK")
        assert pc.subcommand == "lsvdisk"

    def test_quoted_args(self):
        pc = parse_ssh_command('svctask mkhost -name "my host"')
        assert pc.flags["name"] == "my host"

    def test_addhostport_iscsiname(self):
        pc = parse_ssh_command(
            "svctask addhostport -host h1 -iscsiname iqn.2001-04.example.com:host1"
        )
        assert pc.flags["host"] == "h1"
        assert pc.flags["iscsiname"] == "iqn.2001-04.example.com:host1"

    def test_expandvdisksize_positional(self):
        pc = parse_ssh_command("svctask expandvdisksize -size 5 -unit gb myvol")
        assert pc.flags["size"] == "5"
        assert pc.flags["unit"] == "gb"
        assert pc.positional == ["myvol"]


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestParseSshCommandErrors:
    def test_unknown_verb_raises(self):
        with pytest.raises(SvcUnknownCommandError):
            parse_ssh_command("login root")

    def test_too_few_tokens_raises(self):
        with pytest.raises(SvcUnknownCommandError):
            parse_ssh_command("svcinfo")

    def test_empty_string_raises(self):
        with pytest.raises(SvcUnknownCommandError):
            parse_ssh_command("")

    def test_bad_quoting_raises(self):
        with pytest.raises(SvcInvalidArgError):
            parse_ssh_command("svcinfo lsvdisk 'unclosed")

    def test_delim_without_value_raises(self):
        with pytest.raises(SvcInvalidArgError):
            parse_ssh_command("svcinfo lsvdisk vol1 -delim")


# ---------------------------------------------------------------------------
# Helper: require_flag / optional_flag
# ---------------------------------------------------------------------------

class TestFlagHelpers:
    def _pc(self, flags: dict) -> ParsedCommand:
        return ParsedCommand(verb="svcinfo", subcommand="test", flags=flags)

    def test_require_flag_present(self):
        pc = self._pc({"name": "foo"})
        assert require_flag(pc, "name") == "foo"

    def test_require_flag_missing_raises(self):
        pc = self._pc({})
        with pytest.raises(SvcInvalidArgError):
            require_flag(pc, "name")

    def test_require_flag_empty_raises(self):
        pc = self._pc({"name": ""})
        with pytest.raises(SvcInvalidArgError):
            require_flag(pc, "name")

    def test_optional_flag_present(self):
        pc = self._pc({"unit": "gb"})
        assert optional_flag(pc, "unit") == "gb"

    def test_optional_flag_absent_default(self):
        pc = self._pc({})
        assert optional_flag(pc, "unit", "mb") == "mb"

    def test_optional_flag_absent_empty_default(self):
        pc = self._pc({})
        assert optional_flag(pc, "unit") == ""
