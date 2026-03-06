# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Command-line parsing helpers for the IBM SVC SSH façade.

SSH_ORIGINAL_COMMAND format
---------------------------
    svcinfo <subcommand> [args...]
    svctask <subcommand> [args...]

Flags follow POSIX-style ``-flag value`` conventions:

    svcinfo lsvdisk vol1 -delim !
    svctask mkvdisk -name vol1 -size 1 -unit gb -mdiskgrp pool0

Boolean flags (no value) are stored in ``ParsedCommand.flags`` with value
``""``.  Positional arguments appear after all flags have been consumed.

The special ``-delim <char>`` flag is extracted into ``ParsedCommand.delim``
and *not* placed in ``flags``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Optional

from apollo_gateway.compat.ibm_svc.errors import SvcInvalidArgError, SvcUnknownCommandError

VALID_VERBS = frozenset({"svcinfo", "svctask"})


@dataclass
class ParsedCommand:
    """Parsed representation of a single SVC CLI invocation."""

    verb: str           # "svcinfo" or "svctask"
    subcommand: str     # e.g. "lsvdisk"
    raw_args: list[str] = field(default_factory=list)
    delim: Optional[str] = None          # from -delim <char>
    flags: dict[str, str] = field(default_factory=dict)   # -name vol1 → {"name": "vol1"}
    positional: list[str] = field(default_factory=list)   # bare words after flag pairs


def parse_ssh_command(cmd_str: str) -> ParsedCommand:
    """Parse *cmd_str* (value of SSH_ORIGINAL_COMMAND) into a ParsedCommand.

    Raises
    ------
    SvcUnknownCommandError
        If the verb is not ``svcinfo`` or ``svctask``, or if fewer than two
        tokens are present.
    SvcInvalidArgError
        On shell quoting errors or malformed ``-delim`` usage.
    """
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
    """Populate *pc.flags*, *pc.positional*, and *pc.delim* from *rest*."""
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
                # Flag with a value: -name vol1
                pc.flags[key] = rest[i + 1]
                i += 2
            else:
                # Boolean flag: -readonly (no value)
                pc.flags[key] = ""
                i += 1
        else:
            pc.positional.append(tok)
            i += 1


def require_flag(pc: ParsedCommand, flag: str) -> str:
    """Return the value of *flag* or raise :class:`SvcInvalidArgError`.

    Raises if the flag is absent or was provided without a value.
    """
    val = pc.flags.get(flag)
    if val is None or val == "":
        raise SvcInvalidArgError(f"-{flag} is required")
    return val


def optional_flag(pc: ParsedCommand, flag: str, default: str = "") -> str:
    """Return the value of *flag*, falling back to *default* if absent."""
    return pc.flags.get(flag, default) or default
