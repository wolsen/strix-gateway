# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Integration tests for vhost isolation with personality dispatch.

Verifies:
- Admin routes 404 on vendor vhosts
- /healthz works on all vhosts
- Vendor personality routes only respond on vendor vhost
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from strix_gateway.middleware.personality_dispatch import PersonalityDispatcher
from strix_gateway.middleware.vhost import VhostMiddleware
from strix_gateway.tls.vhost import ArrayInfo, VhostRegistry

pytestmark = pytest.mark.asyncio


def _build_isolated_app() -> FastAPI:
    """Build a minimal app with VhostMiddleware + PersonalityDispatcher."""
    main_app = FastAPI()

    @main_app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @main_app.get("/admin/status")
    async def admin_status():
        return {"admin": True}

    @main_app.get("/v1/pools")
    async def list_pools():
        return {"pools": []}

    # Vendor sub-app
    vendor_app = FastAPI()

    @vendor_app.get("/ConfigurationManager/v1/objects/sessions")
    async def vendor_sessions():
        return {"data": "vendor-only"}

    main_app.state.personality_apps = {"hitachi": vendor_app}
    main_app.state.vhost_registry = None  # Set per-test

    return main_app


@pytest_asyncio.fixture
async def isolated_app():
    app = _build_isolated_app()

    # Build a fake vhost registry with one hitachi array
    registry = MagicMock(spec=VhostRegistry)

    hitachi_info = ArrayInfo(
        id="arr-h1", name="hitachi-a",
        fqdn="hitachi-a.gw01.lab.example", vendor="hitachi",
    )

    def lookup(host: str):
        if host == "hitachi-a.gw01.lab.example":
            return hitachi_info
        return None

    registry.lookup = lookup
    app.state.vhost_registry = registry

    # Add middleware: PersonalityDispatcher first (inner), VhostMiddleware second (outer)
    # VhostMiddleware runs first → sets array → PersonalityDispatcher dispatches
    app.add_middleware(PersonalityDispatcher)
    app.add_middleware(VhostMiddleware, require_match=False)

    return app


async def test_healthz_on_vendor_vhost(isolated_app):
    async with AsyncClient(
        transport=ASGITransport(app=isolated_app),
        base_url="http://test",
        headers={"Host": "hitachi-a.gw01.lab.example"},
    ) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_healthz_on_base_hostname(isolated_app):
    async with AsyncClient(
        transport=ASGITransport(app=isolated_app),
        base_url="http://test",
        headers={"Host": "gw01.lab.example"},
    ) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200


async def test_admin_on_base_hostname(isolated_app):
    async with AsyncClient(
        transport=ASGITransport(app=isolated_app),
        base_url="http://test",
        headers={"Host": "gw01.lab.example"},
    ) as client:
        resp = await client.get("/admin/status")
    assert resp.status_code == 200
    assert resp.json()["admin"] is True


async def test_admin_404_on_vendor_vhost(isolated_app):
    """Admin routes should not be served on vendor vhosts."""
    async with AsyncClient(
        transport=ASGITransport(app=isolated_app),
        base_url="http://test",
        headers={"Host": "hitachi-a.gw01.lab.example"},
    ) as client:
        resp = await client.get("/admin/status")
    # The vendor sub-app doesn't have /admin/status, so it should 404
    assert resp.status_code == 404


async def test_vendor_route_on_vendor_vhost(isolated_app):
    async with AsyncClient(
        transport=ASGITransport(app=isolated_app),
        base_url="http://test",
        headers={"Host": "hitachi-a.gw01.lab.example"},
    ) as client:
        resp = await client.get(
            "/ConfigurationManager/v1/objects/sessions",
        )
    assert resp.status_code == 200
    assert resp.json()["data"] == "vendor-only"


async def test_vendor_route_404_on_base_hostname(isolated_app):
    """Vendor personality routes should not exist on the base hostname."""
    async with AsyncClient(
        transport=ASGITransport(app=isolated_app),
        base_url="http://test",
        headers={"Host": "gw01.lab.example"},
    ) as client:
        resp = await client.get(
            "/ConfigurationManager/v1/objects/sessions",
        )
    assert resp.status_code == 404
