# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Strix Gateway FastAPI application entrypoint."""

from __future__ import annotations

import logging
import logging.config
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.requests import Request

from strix_gateway.api import admin, v1
from strix_gateway.api import arrays as arrays_api
from strix_gateway.api import vhost as vhost_api
from strix_gateway.config import settings
from strix_gateway.core.db import Array, TransportEndpoint, get_session_factory, init_db
from strix_gateway.core.faults import FaultInjectionError
from strix_gateway.core.reconcile import reconcile
from strix_gateway.personalities.generic.personality import GenericPersonality
from strix_gateway.spdk.rpc import SPDKClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("strix_gateway.main")


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
    logger.info("Strix Gateway starting — initialising database")
    await init_db(settings.database_url)

    logger.info("Ensuring default array")
    await _ensure_default_array(get_session_factory())

    logger.info("Connecting to SPDK at %s", settings.spdk_socket_path)
    spdk_client = SPDKClient(settings.spdk_socket_path)
    app.state.spdk_client = spdk_client

    # Instantiate the default personality (capability holder)
    personality = GenericPersonality()
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
        from strix_gateway.tls.vhost import VhostRegistry

        registry = VhostRegistry(settings.vhost_domain, settings.vhost_hostname_override)
        await registry.rebuild(get_session_factory())
        app.state.vhost_registry = registry
        logger.info("Vhost registry initialised")

        # Pick up TLSManager and SNIRouter from server.py bootstrap (if available)
        try:
            import strix_gateway.server as _server_mod

            app.state.tls_manager = getattr(_server_mod, "_tls_manager", None)
            app.state.sni_router = getattr(_server_mod, "_sni_router", None)
        except Exception:
            pass

    # Build personality sub-apps for registered vendors.  This runs regardless
    # of vhost mode so that vendor REST APIs are reachable in both vhost
    # (Host-header dispatch) and non-vhost (path-prefix dispatch) topologies.
    import strix_gateway.personalities.hitachi.app  # noqa: F401 — registers factory
    from strix_gateway.personalities.registry import personality_registry

    papps: dict[str, Any] = {}
    for vendor in personality_registry.vendors():
        factory = personality_registry.get(vendor)
        if factory is not None:
            papp = factory.create_app(settings)
            # Share core runtime clients needed by personality routes.
            papp.state.spdk_client = spdk_client
            papp.state.settings = settings
            await papp.router.startup()
            papps[vendor] = papp
            logger.info("Built personality sub-app for vendor=%s", vendor)
    app.state.personality_apps = papps
    logger.info("Personality sub-apps ready: %s", list(papps.keys()) or "(none)")

    # Build vendor route-prefix map for non-vhost personality dispatch.
    vendor_prefixes: dict[str, str] = {}
    for vendor in personality_registry.vendors():
        factory = personality_registry.get(vendor)
        prefix = getattr(factory, "route_prefix", "")
        if prefix:
            vendor_prefixes[prefix] = vendor
    app.state.vendor_route_prefixes = vendor_prefixes

    # PersonalityDispatcher forwards requests to sub-apps without rewriting
    # scope['app'], so request.app resolves to the main app.  Populate Hitachi
    # state here so personality routes can access shared stores/mappers.
    if "hitachi" in papps:
        from strix_gateway.personalities.hitachi.jobs import JobTracker
        from strix_gateway.personalities.hitachi.sessions import SessionStore
        from strix_gateway.personalities.hitachi.translate import HitachiIdMapper

        app.state.hitachi_sessions = SessionStore()
        app.state.hitachi_jobs = JobTracker()
        hitachi_mappers: dict[str, HitachiIdMapper] = {}
        sf = get_session_factory()
        async with sf() as session:
            result = await session.execute(select(Array).where(Array.vendor == "hitachi"))
            for arr in result.scalars().all():
                mapper = HitachiIdMapper(arr.id)
                await mapper.rebuild(session)
                hitachi_mappers[arr.id] = mapper
                logger.info("Hitachi mapper for array=%s serial=%s", arr.name, mapper.storage_device_id)
        app.state.hitachi_mappers = hitachi_mappers

    logger.info("Strix Gateway ready")
    yield

    logger.info("Strix Gateway shutting down")


app = FastAPI(
    title="Strix Gateway",
    description="Virtual Storage Device control-plane by Lunacy Systems",
    version="0.2.0",
    lifespan=lifespan,
)

# PersonalityDispatcher is always active — dispatches to vendor sub-apps
# via vhost array context (when available) or path-prefix matching.
from strix_gateway.middleware.personality_dispatch import PersonalityDispatcher

app.add_middleware(PersonalityDispatcher)

# Vhost middleware (only when enabled)
if settings.vhost_enabled:
    from strix_gateway.middleware.vhost import VhostMiddleware

    # VhostMiddleware is added second → outer middleware → runs first,
    # sets scope["state"]["array"], then PersonalityDispatcher dispatches.
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
