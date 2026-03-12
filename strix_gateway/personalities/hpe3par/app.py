# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""HPE 3PAR WSAPI sub-application factory.

Creates a self-contained FastAPI sub-app for the 3PAR WSAPI REST API
and registers with :data:`personality_registry` at import time.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import FastAPI
from starlette.requests import Request

from strix_gateway.core.exceptions import CoreError
from strix_gateway.personalities.hpe3par.routes import router as wsapi_router
from strix_gateway.personalities.hpe3par.sessions import WsapiSessionStore
from strix_gateway.personalities.hpe3par.wsapi_errors import wsapi_error_response
from strix_gateway.personalities.registry import personality_registry

if TYPE_CHECKING:
    from strix_gateway.config import Settings

logger = logging.getLogger("strix_gateway.personalities.hpe3par.app")


class Hpe3parAppFactory:
    """Build an HPE 3PAR WSAPI sub-app."""

    route_prefix = "/api/v1"

    def create_app(self, settings: "Settings") -> FastAPI:
        @asynccontextmanager
        async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
            logger.info("HPE 3PAR WSAPI sub-app starting")

            app.state.hpe3par_sessions = WsapiSessionStore()
            app.state.settings = settings

            logger.info("HPE 3PAR WSAPI sub-app ready")
            yield
            logger.info("HPE 3PAR WSAPI sub-app shutting down")

        wsapi_app = FastAPI(
            title="Strix Gateway — HPE 3PAR WSAPI",
            version="1.0.0",
            lifespan=_lifespan,
        )

        @wsapi_app.exception_handler(CoreError)
        async def _handle_core_error(request: Request, exc: CoreError):
            return wsapi_error_response(request, exc)

        wsapi_app.include_router(wsapi_router)

        return wsapi_app


# Auto-register at import time
personality_registry.register("hpe_3par", Hpe3parAppFactory())
