# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Canonical typed exceptions for the core service layer.

These exceptions are vendor-agnostic.  API routes and personality façades
translate them into transport-specific error responses (HTTP status codes,
SVC CMMVC error strings, etc.).
"""

from __future__ import annotations


class CoreError(Exception):
    """Base for all core service exceptions."""


class NotFoundError(CoreError):
    """Requested resource does not exist."""

    def __init__(self, resource: str, identifier: str) -> None:
        self.resource = resource
        self.identifier = identifier
        super().__init__(f"{resource} '{identifier}' not found")


class AlreadyExistsError(CoreError):
    """Resource already exists (unique-constraint violation)."""

    def __init__(self, resource: str, identifier: str) -> None:
        self.resource = resource
        self.identifier = identifier
        super().__init__(f"{resource} '{identifier}' already exists")


class InvalidStateError(CoreError):
    """Operation not allowed in the current resource state."""

    def __init__(self, resource: str, identifier: str, current_state: str, detail: str = "") -> None:
        self.resource = resource
        self.identifier = identifier
        self.current_state = current_state
        msg = f"{resource} '{identifier}' is in state '{current_state}'"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class CapabilityDisabledError(CoreError):
    """Feature is disabled in the array capability profile."""

    def __init__(self, feature: str, resource_type: str) -> None:
        self.feature = feature
        self.resource_type = resource_type
        super().__init__(
            f"{resource_type} not supported: '{feature}' is disabled "
            f"in array capability profile"
        )


class ValidationError(CoreError):
    """Input validation failure."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ResourceInUseError(CoreError):
    """Resource cannot be deleted because it has dependants."""

    def __init__(self, resource: str, identifier: str, detail: str) -> None:
        self.resource = resource
        self.identifier = identifier
        super().__init__(f"{resource} '{identifier}' is in use: {detail}")


class BackendError(CoreError):
    """SPDK or other backend operation failed."""

    def __init__(self, detail: str, cause: Exception | None = None) -> None:
        self.detail = detail
        self.__cause__ = cause
        super().__init__(f"SPDK error: {detail}")
