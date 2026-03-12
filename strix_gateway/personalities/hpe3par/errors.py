# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""HPE 3PAR CLI error types.

Error messages are modelled after real InForm OS CLI output so that the
Cinder ``hpe_3par_ssh`` driver can pattern-match on them.
"""

from __future__ import annotations

from strix_gateway.core.exceptions import (
    AlreadyExistsError,
    BackendError,
    CoreError,
    InvalidStateError,
    NotFoundError,
    ResourceInUseError,
)


class Hpe3parError(Exception):
    """Base 3PAR CLI error with exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        self.exit_code = exit_code
        super().__init__(message)


class Hpe3parNotFoundError(Hpe3parError):
    """Object does not exist (Cinder checks for this text)."""

    def __init__(self, resource: str) -> None:
        super().__init__(f"Error: {resource} does not exist")


class Hpe3parAlreadyExistsError(Hpe3parError):
    """Object already exists."""

    def __init__(self, resource: str) -> None:
        super().__init__(f"Error: {resource} already exists")


class Hpe3parInvalidArgError(Hpe3parError):
    """Invalid or missing argument."""

    def __init__(self, msg: str) -> None:
        super().__init__(f"Error: {msg}")


class Hpe3parUnknownCommandError(Hpe3parError):
    """Unknown CLI command."""

    def __init__(self, cmd: str) -> None:
        super().__init__(f"Error: unknown command '{cmd}'")


def core_to_3par(exc: CoreError) -> Hpe3parError:
    """Translate a :class:`CoreError` into the closest 3PAR CLI error."""
    if isinstance(exc, NotFoundError):
        return Hpe3parNotFoundError(str(exc))
    if isinstance(exc, AlreadyExistsError):
        return Hpe3parAlreadyExistsError(str(exc))
    if isinstance(exc, (InvalidStateError, ResourceInUseError)):
        return Hpe3parInvalidArgError(str(exc))
    if isinstance(exc, BackendError):
        return Hpe3parError(str(exc))
    return Hpe3parError(str(exc))
