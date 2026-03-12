# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""WSAPI error formatting for HPE 3PAR.

Maps :class:`CoreError` subclasses to the WSAPI JSON error envelope::

    {"code": <int>, "desc": "<description>"}

Real 3PAR WSAPI error codes are documented in the Web Services API
Developer Guide; we map to the most commonly seen codes.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from strix_gateway.core.exceptions import (
    AlreadyExistsError,
    BackendError,
    CapabilityDisabledError,
    CoreError,
    InvalidStateError,
    NotFoundError,
    ResourceInUseError,
    ValidationError,
)

logger = logging.getLogger("strix_gateway.personalities.hpe3par.wsapi_errors")

# WSAPI error codes (subset):
# 17 = INV_INPUT  (bad parameter)
# 22 = INV_INPUT_EXCEEDS_RANGE
# 73 = NON_EXISTENT_VOL
# 69 = NON_EXISTENT_HOST
# 75 = NON_EXISTENT_VLUN
# 78 = NON_EXISTENT_CPG
# 29 = EXISTENT_VLUN
# 68 = EXISTENT_HOST
# 19 = EXISTENT_OBJECT
# 500 = INTERNAL_SERVER_ERROR

_ERROR_MAP: dict[type[CoreError], tuple[int, int]] = {
    # exception class → (HTTP status, WSAPI error code)
    NotFoundError: (404, 73),
    AlreadyExistsError: (409, 19),
    InvalidStateError: (409, 22),
    ResourceInUseError: (409, 22),
    ValidationError: (400, 17),
    CapabilityDisabledError: (400, 17),
    BackendError: (500, 500),
}


def wsapi_error_response(request: Request, exc: CoreError) -> JSONResponse:
    """Build a WSAPI-shaped error JSONResponse."""
    status, code = _ERROR_MAP.get(type(exc), (500, 500))
    body = {
        "code": code,
        "desc": str(exc),
    }
    logger.debug("WSAPI error code=%d: %s", code, exc)
    return JSONResponse(status_code=status, content=body)
