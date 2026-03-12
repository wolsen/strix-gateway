# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Command-line parsing helpers for the HPE 3PAR InForm OS SSH façade.

Unlike IBM SVC (which uses ``verb subcommand``), 3PAR uses compound
command words: ``showvv``, ``createvv``, ``removevv``, etc.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Optional

from strix_gateway.personalities.hpe3par.errors import (
    Hpe3parInvalidArgError,
    Hpe3parUnknownCommandError,
)

KNOWN_COMMANDS = frozenset({
    "showsys",
    "showvv",
    "showcpg",
    "showhost",
    "showvlun",
    "showport",
    "createvv",
    "removevv",
    "growvv",
    "createhost",
    "removehost",
    "sethost",
    "createvlun",
    "removevlun",
})


@dataclass
class ParsedCommand:
    """Parsed representation of a single 3PAR CLI invocation."""

    command: str
    raw_args: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    boolean_flags: set[str] = field(default_factory=set)
    positional: list[str] = field(default_factory=list)


def parse_command(cmd_str: str) -> ParsedCommand:
    """Parse an InForm OS command string into a :class:`ParsedCommand`."""
    try:
        tokens = shlex.split(cmd_str.strip())
    except ValueError as exc:
        raise Hpe3parInvalidArgError(f"bad quoting in command: {exc}") from exc

    if not tokens:
        raise Hpe3parInvalidArgError("empty command")

    command = tokens[0].lower()
    if command not in KNOWN_COMMANDS:
        raise Hpe3parUnknownCommandError(command)

    rest = tokens[1:]
    pc = ParsedCommand(command=command, raw_args=rest)
    _extract_flags(pc, rest)
    return pc


# Boolean flags that never take a value argument.
_BOOLEAN_FLAGS = frozenset({
    "tpvv", "tdvv", "f", "d", "showcols", "nodetach",
    "iscsi", "rcfc", "peer", "fs", "add",
})


def _extract_flags(pc: ParsedCommand, rest: list[str]) -> None:
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-"):
            key = tok.lstrip("-")
            if key in _BOOLEAN_FLAGS:
                pc.boolean_flags.add(key)
                i += 1
            elif i + 1 < len(rest) and not rest[i + 1].startswith("-"):
                pc.flags[key] = rest[i + 1]
                i += 2
            else:
                pc.boolean_flags.add(key)
                i += 1
        else:
            pc.positional.append(tok)
            i += 1


def require_flag(pc: ParsedCommand, flag: str) -> str:
    """Return required flag value or raise :class:`Hpe3parInvalidArgError`."""
    val = pc.flags.get(flag)
    if val is None or val == "":
        raise Hpe3parInvalidArgError(f"-{flag} is required")
    return val


def optional_flag(pc: ParsedCommand, flag: str, default: str = "") -> str:
    """Return optional flag value, defaulting when absent."""
    return pc.flags.get(flag, default) or default
