# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Typed errors for the SVC SSH façade, mapped to exit codes and stable stderr messages.

Contract (checked by Cinder drivers):
  - Not found      → exit 1, stderr contains "not found"
  - Already exists → exit 1, stderr contains "already exists"
  - Unknown cmd    → exit 1 (any message)
  - Success        → exit 0
"""

from __future__ import annotations


class SvcError(Exception):
    """Base class for all SVC façade errors."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class SvcNotFoundError(SvcError):
    """Resource not found. stderr MUST contain the word 'not found'."""

    def __init__(self, resource: str) -> None:
        super().__init__(f"CMMVC5753E {resource} not found")


class SvcAlreadyExistsError(SvcError):
    """Resource already exists. stderr MUST contain 'already exists'."""

    def __init__(self, resource: str) -> None:
        super().__init__(f"CMMVC6035E {resource} already exists")


class SvcUnknownCommandError(SvcError):
    """Unknown top-level verb or subcommand."""

    def __init__(self, cmd: str) -> None:
        super().__init__(f"CMMVC5753E unknown command: {cmd}")


class SvcInvalidArgError(SvcError):
    """Missing or invalid command argument."""

    def __init__(self, msg: str) -> None:
        super().__init__(f"CMMVC5707E {msg}")
