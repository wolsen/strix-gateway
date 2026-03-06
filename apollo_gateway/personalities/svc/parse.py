# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Command-line parsing helpers for the SVC SSH façade."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Optional

from apollo_gateway.personalities.svc.errors import (
    SvcInvalidArgError,
    SvcUnknownCommandError,
)

VALID_VERBS = frozenset({"svcinfo", "svctask"})


@dataclass
class ParsedCommand:
    """Parsed representation of a single SVC CLI invocation."""

    verb: str
    subcommand: str
    raw_args: list[str] = field(default_factory=list)
    delim: Optional[str] = None
    flags: dict[str, str] = field(default_factory=dict)
    positional: list[str] = field(default_factory=list)


def parse_ssh_command(cmd_str: str) -> ParsedCommand:
    """Parse SSH_ORIGINAL_COMMAND text into a ParsedCommand."""
    try:
        tokens = shlex.split(cmd_str.strip())
    except ValueError as exc:
        raise SvcInvalidArgError(f"bad quoting in command: {exc}") from exc

    if len(tokens) < 2:
        raise SvcUnknownCommandError(repr(cmd_str))

    verb = tokens[0].lower()
    if verb not in VALID_VERBS:
        raise SvcUnknownCommandError(verb)

    subcommand = tokens[1].lower()
    rest = tokens[2:]

    pc = ParsedCommand(verb=verb, subcommand=subcommand, raw_args=rest)
    _extract_flags(pc, rest)
    return pc


def _extract_flags(pc: ParsedCommand, rest: list[str]) -> None:
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-"):
            key = tok.lstrip("-")
            if key == "delim":
                if i + 1 >= len(rest):
                    raise SvcInvalidArgError("-delim requires a delimiter character")
                pc.delim = rest[i + 1]
                i += 2
            elif i + 1 < len(rest) and not rest[i + 1].startswith("-"):
                pc.flags[key] = rest[i + 1]
                i += 2
            else:
                pc.flags[key] = ""
                i += 1
        else:
            pc.positional.append(tok)
            i += 1


def require_flag(pc: ParsedCommand, flag: str) -> str:
    """Return required flag value or raise SvcInvalidArgError."""
    val = pc.flags.get(flag)
    if val is None or val == "":
        raise SvcInvalidArgError(f"-{flag} is required")
    return val


def optional_flag(pc: ParsedCommand, flag: str, default: str = "") -> str:
    """Return optional flag value, defaulting when absent."""
    return pc.flags.get(flag, default) or default
