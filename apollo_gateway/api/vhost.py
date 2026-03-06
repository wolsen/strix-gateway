# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Vhost mapping and TLS management API endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from apollo_gateway.config import settings
from apollo_gateway.core.db import get_session_factory

logger = logging.getLogger("apollo_gateway.api.vhost")

router = APIRouter(prefix="/v1", tags=["vhost", "tls"])


@router.get("/vhosts")
async def list_vhosts(request: Request):
    """List all array → FQDN mappings."""
    registry = getattr(request.app.state, "vhost_registry", None)
    if registry is None:
        return {"vhost_enabled": False, "mappings": []}

    mappings = registry.all_mappings()
    return {
        "vhost_enabled": True,
        "domain": settings.vhost_domain,
        "tls_mode": settings.tls_mode,
        "mappings": [
            {
                "array_name": info.name,
                "array_id": info.id,
                "fqdn": info.fqdn,
            }
            for info in mappings.values()
        ],
    }


@router.post("/tls/sync")
async def sync_tls(request: Request):
    """Trigger a TLS certificate re-sync for all current arrays."""
    from apollo_gateway.api.arrays import _refresh_vhost_state

    await _refresh_vhost_state(request)
    return {"status": "ok", "detail": "TLS assets synchronized"}


@router.get("/tls/ca")
async def get_ca_cert(request: Request):
    """Return the internal CA certificate in PEM format."""
    mgr = getattr(request.app.state, "tls_manager", None)
    if mgr is None:
        raise HTTPException(status_code=404, detail="TLS not enabled")

    ca_path = mgr.ca_crt_path
    if not ca_path.exists():
        raise HTTPException(status_code=404, detail="CA certificate not found")

    return PlainTextResponse(
        ca_path.read_text(), media_type="application/x-pem-file"
    )
