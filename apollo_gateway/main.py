# FILE: apollo_gateway/main.py
"""Apollo Gateway FastAPI application entrypoint."""

from __future__ import annotations

import logging
import logging.config
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from apollo_gateway.api import admin, v1
from apollo_gateway.config import settings
from apollo_gateway.core.db import get_session_factory, init_db
from apollo_gateway.core.faults import FaultInjectionError
from apollo_gateway.core.reconcile import reconcile
from apollo_gateway.spdk.rpc import SPDKClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("apollo_gateway.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Apollo Gateway starting — initialising database")
    await init_db(settings.database_url)

    logger.info("Connecting to SPDK at %s", settings.spdk_socket_path)
    spdk_client = SPDKClient(settings.spdk_socket_path)
    app.state.spdk_client = spdk_client

    logger.info("Running startup reconciliation")
    try:
        await reconcile(spdk_client, get_session_factory(), settings)
    except Exception as exc:
        # Reconcile errors are non-fatal at startup (SPDK may not be ready yet)
        logger.warning("Reconciliation encountered errors: %s", exc)

    logger.info("Apollo Gateway ready")
    yield

    logger.info("Apollo Gateway shutting down")


app = FastAPI(
    title="Apollo Gateway",
    description="Virtual Storage Device control-plane by Lunacy Systems",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(v1.router)
app.include_router(admin.router)


@app.exception_handler(FaultInjectionError)
async def fault_injection_handler(request: Request, exc: FaultInjectionError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": f"fault injected: {exc}"})


@app.get("/healthz", tags=["health"])
async def healthz():
    return {"status": "ok"}
