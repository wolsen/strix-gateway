# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Typed CLI errors mapped to exit codes.

Exit code conventions:
    0  success
    1  user / validation error
    2  API error (HTTP non-2xx)
    3  unexpected error / exception
"""

from __future__ import annotations


class CLIError(Exception):
    """Base class for all CLI errors."""

    exit_code: int = 3

    def __init__(self, message: str, exit_code: int | None = None):
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class ValidationError(CLIError):
    """User input or topology validation error (exit 1)."""

    exit_code = 1


class APIError(CLIError):
    """API returned a non-2xx status code (exit 2)."""

    exit_code = 2

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {detail}", exit_code=2)


class UnexpectedError(CLIError):
    """Unexpected exception (exit 3)."""

    exit_code = 3
