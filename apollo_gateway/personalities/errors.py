# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Shared error normalisation for storage personalities.

Maps canonical core exceptions to personality-appropriate error types.
Vendor personalities can register custom mappings (e.g. SVC CMMVC codes).
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Type

from fastapi import HTTPException

from apollo_gateway.core.exceptions import (
    AlreadyExistsError,
    BackendError,
    CapabilityDisabledError,
    CoreError,
    InvalidStateError,
    NotFoundError,
    ResourceInUseError,
    ValidationError,
)

logger = logging.getLogger("apollo_gateway.personalities.errors")


class PersonalityError(Exception):
    """Base for personality-layer errors."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        self.exit_code = exit_code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Core → HTTP translation (for REST-based personalities)
# ---------------------------------------------------------------------------

_HTTP_MAP: dict[Type[CoreError], int] = {
    NotFoundError: 404,
    AlreadyExistsError: 409,
    InvalidStateError: 409,
    ResourceInUseError: 409,
    ValidationError: 400,
    CapabilityDisabledError: 422,
    BackendError: 500,
}


def core_to_http(exc: CoreError) -> HTTPException:
    """Convert a :class:`CoreError` to a :class:`fastapi.HTTPException`."""
    status_code = _HTTP_MAP.get(type(exc), 500)
    return HTTPException(status_code=status_code, detail=str(exc))


def http_error_handler(func: Callable) -> Callable:
    """Decorator: catches :class:`CoreError` and re-raises as HTTP exceptions."""
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except CoreError as exc:
            raise core_to_http(exc) from exc
    return wrapper
