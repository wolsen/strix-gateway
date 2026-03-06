# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Apollo Gateway FastAPI application entrypoint."""

from __future__ import annotations

import logging
import logging.config
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.requests import Request

from apollo_gateway.api import admin, v1
from apollo_gateway.api import arrays as arrays_api
from apollo_gateway.api import vhost as vhost_api
from apollo_gateway.config import settings
from apollo_gateway.core.db import Array, TransportEndpoint, get_session_factory, init_db
from apollo_gateway.core.faults import FaultInjectionError
from apollo_gateway.core.reconcile import reconcile
from apollo_gateway.personalities.generic.personality import GenericPersonality
from apollo_gateway.spdk.rpc import SPDKClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("apollo_gateway.main")


async def _ensure_default_array(session_factory) -> None:
    """Create the 'default' array (with transport endpoints) if it does not exist."""
    async with session_factory() as session:
        result = await session.execute(select(Array).where(Array.name == "default"))
        if result.scalar_one_or_none() is None:
            default = Array(
                name="default",
                vendor="generic",
                profile="{}",
            )
            session.add(default)
            await session.flush()  # populate default.id

            # Seed default transport endpoints so mapping creation can resolve them
            for proto in ("iscsi", "nvmeof_tcp", "fc"):
                ep = TransportEndpoint(
                    array_id=default.id,
                    protocol=proto,
                    targets="[]",
                    addresses="[]",
                    auth="{}",
                )
                session.add(ep)

            await session.commit()
            logger.info("Created default array with transport endpoints")
        else:
            logger.debug("Default array already exists")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Apollo Gateway starting — initialising database")
    await init_db(settings.database_url)

    logger.info("Ensuring default array")
    await _ensure_default_array(get_session_factory())

    logger.info("Connecting to SPDK at %s", settings.spdk_socket_path)
    spdk_client = SPDKClient(settings.spdk_socket_path)
    app.state.spdk_client = spdk_client

    # Instantiate the active personality
    personality = GenericPersonality(settings)
    personality.spdk = spdk_client
    app.state.personality = personality
    logger.info("Personality: %s", type(personality).__name__)

    logger.info("Running startup reconciliation")
    try:
        await reconcile(spdk_client, get_session_factory(), settings)
    except Exception as exc:
        # Reconcile errors are non-fatal at startup (SPDK may not be ready yet)
        logger.warning("Reconciliation encountered errors: %s", exc)

    # Vhost registry bootstrap (when vhost mode enabled)
    if settings.vhost_enabled and settings.vhost_domain:
        from apollo_gateway.tls.vhost import VhostRegistry

        registry = VhostRegistry(settings.vhost_domain, settings.vhost_hostname_override)
        await registry.rebuild(get_session_factory())
        app.state.vhost_registry = registry
        logger.info("Vhost registry initialised")

        # Pick up TLSManager and SNIRouter from server.py bootstrap (if available)
        try:
            import apollo_gateway.server as _server_mod

            app.state.tls_manager = getattr(_server_mod, "_tls_manager", None)
            app.state.sni_router = getattr(_server_mod, "_sni_router", None)
        except Exception:
            pass

    logger.info("Apollo Gateway ready")
    yield

    logger.info("Apollo Gateway shutting down")


app = FastAPI(
    title="Apollo Gateway",
    description="Virtual Storage Device control-plane by Lunacy Systems",
    version="0.2.0",
    lifespan=lifespan,
)

# Vhost middleware (only when enabled)
if settings.vhost_enabled:
    from apollo_gateway.middleware.vhost import VhostMiddleware

    app.add_middleware(VhostMiddleware, require_match=settings.vhost_require_match)

app.include_router(v1.router)
app.include_router(admin.router)
app.include_router(arrays_api.router)
app.include_router(vhost_api.router)


@app.exception_handler(FaultInjectionError)
async def fault_injection_handler(request: Request, exc: FaultInjectionError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": f"fault injected: {exc}"})


@app.get("/healthz", tags=["health"])
async def healthz():
    return {"status": "ok"}
