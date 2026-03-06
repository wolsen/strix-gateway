# FILE: tests/integration/test_vhost_middleware.py
"""Integration tests for vhost middleware and API endpoints.

Uses a real FastAPI test client with the VhostMiddleware active.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from apollo_gateway.core.db import Array, init_db, get_session_factory
from apollo_gateway.main import _ensure_default_array
from apollo_gateway.middleware.vhost import VhostMiddleware
from apollo_gateway.tls.vhost import VhostRegistry

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def _build_app(*, require_match: bool = True) -> FastAPI:
    """Build a minimal FastAPI app with vhost middleware for testing."""
    from apollo_gateway.api import vhost as vhost_api

    test_app = FastAPI()
    test_app.add_middleware(VhostMiddleware, require_match=require_match)
    test_app.include_router(vhost_api.router)

    @test_app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @test_app.get("/v1/test-context")
    async def test_context(request: Request):
        """Test endpoint that returns the resolved array from vhost."""
        from starlette.responses import JSONResponse

        arr = getattr(request.state, "array", None)
        if arr is not None:
            return JSONResponse({
                "matched": True,
                "array_name": arr.name,
                "array_id": arr.id,
                "fqdn": arr.fqdn,
            })
        return JSONResponse({"matched": False})

    return test_app


@pytest_asyncio.fixture
async def vhost_app():
    """App with vhost middleware (require_match=True)."""
    await init_db(TEST_DATABASE_URL)
    await _ensure_default_array(get_session_factory())

    # Create an additional array
    factory = get_session_factory()
    async with factory() as session:
        session.add(Array(name="pure-a", vendor="pure"))
        await session.commit()

    app = _build_app(require_match=True)

    # Build and attach registry
    registry = VhostRegistry("lab.example", hostname_override="gw01")
    await registry.rebuild(get_session_factory())
    app.state.vhost_registry = registry

    return app


@pytest_asyncio.fixture
async def permissive_app():
    """App with vhost middleware (require_match=False)."""
    await init_db(TEST_DATABASE_URL)
    await _ensure_default_array(get_session_factory())

    app = _build_app(require_match=False)

    registry = VhostRegistry("lab.example", hostname_override="gw01")
    await registry.rebuild(get_session_factory())
    app.state.vhost_registry = registry

    return app


class TestVhostMiddleware:
    async def test_matched_host_sets_array(self, vhost_app):
        async with AsyncClient(
            transport=ASGITransport(app=vhost_app),
            base_url="http://test",
            headers={"Host": "pure-a.gw01.lab.example"},
        ) as client:
            resp = await client.get("/v1/test-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched"] is True
        assert data["array_name"] == "pure-a"

    async def test_default_array_matched(self, vhost_app):
        async with AsyncClient(
            transport=ASGITransport(app=vhost_app),
            base_url="http://test",
            headers={"Host": "default.gw01.lab.example"},
        ) as client:
            resp = await client.get("/v1/test-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched"] is True
        assert data["array_name"] == "default"

    async def test_unknown_host_returns_404_when_required(self, vhost_app):
        async with AsyncClient(
            transport=ASGITransport(app=vhost_app),
            base_url="http://test",
            headers={"Host": "unknown.gw01.lab.example"},
        ) as client:
            resp = await client.get("/v1/test-context")
        assert resp.status_code == 404
        assert "Unknown host" in resp.json()["detail"]

    async def test_unknown_host_passes_through_when_not_required(self, permissive_app):
        async with AsyncClient(
            transport=ASGITransport(app=permissive_app),
            base_url="http://test",
            headers={"Host": "unknown.gw01.lab.example"},
        ) as client:
            resp = await client.get("/v1/test-context")
        assert resp.status_code == 200
        assert resp.json()["matched"] is False

    async def test_healthz_bypasses_vhost(self, vhost_app):
        async with AsyncClient(
            transport=ASGITransport(app=vhost_app),
            base_url="http://test",
            headers={"Host": "unknown.gw01.lab.example"},
        ) as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_host_header_case_insensitive(self, vhost_app):
        async with AsyncClient(
            transport=ASGITransport(app=vhost_app),
            base_url="http://test",
            headers={"Host": "PURE-A.GW01.LAB.EXAMPLE"},
        ) as client:
            resp = await client.get("/v1/test-context")
        assert resp.status_code == 200
        assert resp.json()["array_name"] == "pure-a"

    async def test_host_header_with_port_stripped(self, vhost_app):
        async with AsyncClient(
            transport=ASGITransport(app=vhost_app),
            base_url="http://test",
            headers={"Host": "pure-a.gw01.lab.example:443"},
        ) as client:
            resp = await client.get("/v1/test-context")
        assert resp.status_code == 200
        assert resp.json()["array_name"] == "pure-a"


class TestVhostApiEndpoints:
    async def test_list_vhosts(self, vhost_app):
        async with AsyncClient(
            transport=ASGITransport(app=vhost_app),
            base_url="http://test",
            headers={"Host": "default.gw01.lab.example"},
        ) as client:
            resp = await client.get("/v1/vhosts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vhost_enabled"] is True
        names = [m["array_name"] for m in data["mappings"]]
        assert "default" in names
        assert "pure-a" in names

    async def test_list_vhosts_when_disabled(self):
        """When no registry is on app.state, return vhost_enabled=False."""
        from apollo_gateway.api import vhost as vhost_api

        test_app = FastAPI()
        test_app.include_router(vhost_api.router)

        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/v1/vhosts")
        assert resp.status_code == 200
        assert resp.json()["vhost_enabled"] is False

    async def test_tls_ca_returns_404_when_disabled(self):
        """GET /v1/tls/ca returns 404 when TLS is not enabled."""
        from apollo_gateway.api import vhost as vhost_api

        test_app = FastAPI()
        test_app.include_router(vhost_api.router)

        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/v1/tls/ca")
        assert resp.status_code == 404
