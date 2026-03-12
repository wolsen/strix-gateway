# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Hitachi Configuration Manager error formatting.

Maps canonical :class:`CoreError` types to Hitachi-style error JSON:

.. code-block:: json

   {
     "errorSource": "/ConfigurationManager/v1/objects/ldevs",
     "messageId": "KART30000-E",
     "message": "...",
     "solution": "...",
     "statusCode": 404
   }
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

logger = logging.getLogger("strix_gateway.personalities.hitachi.errors")


_ERROR_MAP: dict[type[CoreError], tuple[int, str, str]] = {
    # exception class → (HTTP status, messageId, solution hint)
    NotFoundError: (404, "KART30000-E", "Check the resource ID or path."),
    AlreadyExistsError: (409, "KART30079-E", "The resource already exists."),
    InvalidStateError: (409, "KART30010-E", "Wait until the resource is ready."),
    ResourceInUseError: (409, "KART30010-E", "Remove dependants first."),
    ValidationError: (400, "KART30003-E", "Check the request parameters."),
    CapabilityDisabledError: (422, "KART30003-E", "Feature is not available."),
    BackendError: (500, "KART30900-E", "Retry later or contact an administrator."),
}


def hitachi_error_response(request: Request, exc: CoreError) -> JSONResponse:
    """Build a Hitachi-shaped error JSONResponse from a :class:`CoreError`."""
    status, message_id, solution = _ERROR_MAP.get(
        type(exc), (500, "KART30900-E", "Unexpected error.")
    )
    body = {
        "errorSource": str(request.url.path),
        "messageId": message_id,
        "message": str(exc),
        "solution": solution,
        "statusCode": status,
    }
    logger.debug("Hitachi error %s: %s", message_id, exc)
    return JSONResponse(status_code=status, content=body)
