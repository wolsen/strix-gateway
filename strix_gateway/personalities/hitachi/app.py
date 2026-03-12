# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Hitachi VSP sub-application factory.

Creates a self-contained FastAPI sub-app with all Hitachi Configuration
Manager routes, error handling, and shared state (session store, job
tracker, ID mappers).

Registers with :data:`personality_registry` at import time.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import FastAPI
from starlette.requests import Request

from strix_gateway.core.db import Array, get_session_factory
from strix_gateway.core.exceptions import CoreError
from strix_gateway.personalities.hitachi.errors import hitachi_error_response
from strix_gateway.personalities.hitachi.jobs import JobTracker
from strix_gateway.personalities.hitachi.routes import router as hitachi_router
from strix_gateway.personalities.hitachi.sessions import SessionStore
from strix_gateway.personalities.hitachi.translate import HitachiIdMapper
from strix_gateway.personalities.registry import personality_registry

if TYPE_CHECKING:
    from strix_gateway.config import Settings

from sqlalchemy import select

logger = logging.getLogger("strix_gateway.personalities.hitachi.app")


class HitachiAppFactory:
    """Build a Hitachi Configuration Manager sub-app."""

    route_prefix = "/ConfigurationManager"

    def create_app(self, settings: "Settings") -> FastAPI:
        @asynccontextmanager
        async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
            logger.info("Hitachi sub-app starting")

            # Shared state
            app.state.hitachi_sessions = SessionStore()
            app.state.hitachi_jobs = JobTracker()
            app.state.settings = settings

            # Build ID mappers for each Hitachi array
            mappers: dict[str, HitachiIdMapper] = {}
            sf = get_session_factory()
            async with sf() as session:
                result = await session.execute(
                    select(Array).where(Array.vendor == "hitachi")
                )
                arrays = list(result.scalars().all())
                for arr in arrays:
                    mapper = HitachiIdMapper(arr.id)
                    await mapper.rebuild(session)
                    mappers[arr.id] = mapper
                    logger.info(
                        "Hitachi mapper for array=%s serial=%s",
                        arr.name, mapper.storage_device_id,
                    )
                await session.commit()

            app.state.hitachi_mappers = mappers
            logger.info("Hitachi sub-app ready (%d arrays)", len(mappers))

            yield

            logger.info("Hitachi sub-app shutting down")

        hitachi_app = FastAPI(
            title="Strix Gateway — Hitachi Configuration Manager",
            version="1.0.0",
            lifespan=_lifespan,
        )

        # Register Hitachi error handler for CoreError
        @hitachi_app.exception_handler(CoreError)
        async def _handle_core_error(request: Request, exc: CoreError):
            return hitachi_error_response(request, exc)

        hitachi_app.include_router(hitachi_router)

        return hitachi_app


# Auto-register at import time
personality_registry.register("hitachi", HitachiAppFactory())
