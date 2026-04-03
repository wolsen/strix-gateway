# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Integration tests — HPE 3PAR WSAPI REST API surface.

Simulates the full Cinder 3PAR WSAPI driver workflow: auth → create volume →
map → unmap → delete → logout.  Uses in-memory SQLite + mocked SPDK.
Follows the same standalone sub-app pattern as the Hitachi integration tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from strix_gateway.core.db import (
    Array,
    Base,
    Pool,
    TransportEndpoint,
    get_session_factory,
    init_db,
)
from strix_gateway.config import Settings
from strix_gateway.core.exceptions import CoreError
from strix_gateway.personalities.hpe3par.routes import router as wsapi_router
from strix_gateway.personalities.hpe3par.sessions import WsapiSessionStore
from strix_gateway.personalities.hpe3par.wsapi_errors import wsapi_error_response

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

SESSION_HEADER = "X-HP3PAR-WSAPI-SessionKey"


@dataclass
class FakeArrayInfo:
    id: str
    name: str
    fqdn: str
    vendor: str


@pytest_asyncio.fixture
async def wsapi_client():
    """AsyncClient wired to the 3PAR WSAPI sub-app with a seeded array."""
    await init_db(TEST_DATABASE_URL)

    factory = get_session_factory()

    # Seed a 3PAR array with CPG pool + transport endpoints
    async with factory() as session:
        arr = Array(name="3par-wsapi", vendor="hpe_3par")
        session.add(arr)
        await session.flush()

        pool = Pool(
            name="cpg0",
            array_id=arr.id,
            backend_type="malloc",
            size_mb=8192,
        )
        session.add(pool)

        for proto in ("iscsi", "fc"):
            ep = TransportEndpoint(
                array_id=arr.id,
                protocol=proto,
                targets=json.dumps(
                    {"target_iqn": "iqn.strix.3par.test"}
                    if proto == "iscsi"
                    else {"target_wwpns": ["50:00:00:00:00:00:00:03"]}
                ),
                addresses=json.dumps(
                    {"portals": ["10.0.0.3:3260"]}
                    if proto == "iscsi"
                    else {}
                ),
            )
            session.add(ep)

        await session.commit()
        array_id = arr.id

    array_info = FakeArrayInfo(
        id=array_id,
        name="3par-wsapi",
        fqdn="3par-wsapi.gw01.lab.example",
        vendor="hpe_3par",
    )

    # Build standalone WSAPI sub-app
    wsapi_app = FastAPI()
    wsapi_app.state.hpe3par_sessions = WsapiSessionStore()
    wsapi_app.state.settings = Settings()
    wsapi_app.state.spdk_client = MagicMock()
    wsapi_app.state.spdk_client.call = MagicMock(return_value=None)

    @wsapi_app.exception_handler(CoreError)
    async def _handle_core_error(request: Request, exc: CoreError):
        return wsapi_error_response(request, exc)

    @wsapi_app.middleware("http")
    async def inject_array(request: Request, call_next):
        request.scope.setdefault("state", {})
        request.scope["state"]["array"] = array_info
        return await call_next(request)

    wsapi_app.include_router(wsapi_router)

    async with AsyncClient(
        transport=ASGITransport(app=wsapi_app),
        base_url="http://test",
    ) as client:
        yield client


pytestmark = pytest.mark.asyncio


async def _auth(client: AsyncClient) -> str:
    """Authenticate and return the session key."""
    resp = await client.post(
        "/api/v1/credentials",
        json={"user": "admin", "password": "secret"},
    )
    assert resp.status_code == 201, resp.text
    key = resp.json()["key"]
    assert key
    return key


# ---------------------------------------------------------------------------
# Auth (credentials)
# ---------------------------------------------------------------------------


async def test_create_and_delete_session(wsapi_client):
    key = await _auth(wsapi_client)

    resp = await wsapi_client.delete(f"/api/v1/credentials/{key}")
    assert resp.status_code == 200


async def test_invalid_session_rejected(wsapi_client):
    resp = await wsapi_client.get(
        "/api/v1/system",
        headers={SESSION_HEADER: "bogus-key"},
    )
    assert resp.status_code == 400


async def test_missing_session_header_rejected(wsapi_client):
    resp = await wsapi_client.get("/api/v1/system")
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


async def test_get_system(wsapi_client):
    key = await _auth(wsapi_client)
    resp = await wsapi_client.get(
        "/api/v1/system",
        headers={SESSION_HEADER: key},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "name" in data
    assert "totalCapacityMiB" in data
    assert "freeCapacityMiB" in data


# ---------------------------------------------------------------------------
# CPGs (pools)
# ---------------------------------------------------------------------------


async def test_list_cpgs(wsapi_client):
    key = await _auth(wsapi_client)
    resp = await wsapi_client.get(
        "/api/v1/cpgs",
        headers={SESSION_HEADER: key},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "members" in data
    assert "total" in data
    assert data["total"] >= 1
    assert any(cpg["name"] == "cpg0" for cpg in data["members"])


async def test_get_cpg_by_name(wsapi_client):
    key = await _auth(wsapi_client)
    resp = await wsapi_client.get(
        "/api/v1/cpgs/cpg0",
        headers={SESSION_HEADER: key},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "cpg0"


async def test_get_nonexistent_cpg(wsapi_client):
    key = await _auth(wsapi_client)
    resp = await wsapi_client.get(
        "/api/v1/cpgs/nosuch",
        headers={SESSION_HEADER: key},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


async def test_list_ports(wsapi_client):
    key = await _auth(wsapi_client)
    resp = await wsapi_client.get(
        "/api/v1/ports",
        headers={SESSION_HEADER: key},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "members" in data
    assert data["total"] >= 1


# ---------------------------------------------------------------------------
# Full volume + host + VLUN lifecycle
# ---------------------------------------------------------------------------


async def test_volume_host_vlun_lifecycle(wsapi_client):
    """Full Cinder 3PAR WSAPI workflow: volume → host → VLUN → teardown."""
    key = await _auth(wsapi_client)
    headers = {SESSION_HEADER: key}

    # Create volume
    resp = await wsapi_client.post(
        "/api/v1/volumes",
        headers=headers,
        json={"name": "vol01", "cpg": "cpg0", "sizeMiB": 1024},
    )
    assert resp.status_code == 201, resp.text

    # List volumes
    resp = await wsapi_client.get("/api/v1/volumes", headers=headers)
    assert resp.status_code == 200
    vols = resp.json()["members"]
    assert any(v["name"] == "vol01" for v in vols)

    # Get single volume
    resp = await wsapi_client.get("/api/v1/volumes/vol01", headers=headers)
    assert resp.status_code == 200
    vol = resp.json()
    assert vol["name"] == "vol01"
    assert vol["sizeMiB"] == 1024

    # Create host with iSCSI
    resp = await wsapi_client.post(
        "/api/v1/hosts",
        headers=headers,
        json={
            "name": "host01",
            "iSCSINames": ["iqn.2024-01.com.example:host01"],
        },
    )
    assert resp.status_code == 201, resp.text

    # List hosts
    resp = await wsapi_client.get("/api/v1/hosts", headers=headers)
    assert resp.status_code == 200
    hosts = resp.json()["members"]
    assert any(h["name"] == "host01" for h in hosts)

    # Get host detail
    resp = await wsapi_client.get("/api/v1/hosts/host01", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "host01"

    # Create VLUN
    resp = await wsapi_client.post(
        "/api/v1/vluns",
        headers=headers,
        json={"volumeName": "vol01", "hostname": "host01"},
    )
    assert resp.status_code == 201, resp.text
    vlun = resp.json()
    lun = vlun["lun"]

    # List VLUNs
    resp = await wsapi_client.get("/api/v1/vluns", headers=headers)
    assert resp.status_code == 200
    vluns = resp.json()["members"]
    assert any(
        v["volumeName"] == "vol01" and v["hostname"] == "host01"
        for v in vluns
    )

    # Delete VLUN
    resp = await wsapi_client.delete(
        f"/api/v1/vluns/vol01,{lun},host01",
        headers=headers,
    )
    assert resp.status_code == 200

    # VLUN should be gone
    resp = await wsapi_client.get("/api/v1/vluns", headers=headers)
    assert resp.status_code == 200
    vluns = resp.json()["members"]
    assert not any(
        v["volumeName"] == "vol01" and v["hostname"] == "host01"
        for v in vluns
    )

    # Delete volume
    resp = await wsapi_client.delete("/api/v1/volumes/vol01", headers=headers)
    assert resp.status_code == 200

    # Volume should be gone
    resp = await wsapi_client.get("/api/v1/volumes/vol01", headers=headers)
    assert resp.status_code == 404

    # Delete host
    resp = await wsapi_client.delete("/api/v1/hosts/host01", headers=headers)
    assert resp.status_code == 200


async def test_grow_volume(wsapi_client):
    """Grow an existing volume via PUT /volumes/{name}."""
    key = await _auth(wsapi_client)
    headers = {SESSION_HEADER: key}

    # Create volume
    resp = await wsapi_client.post(
        "/api/v1/volumes",
        headers=headers,
        json={"name": "grow_vol", "cpg": "cpg0", "sizeMiB": 1024},
    )
    assert resp.status_code == 201

    # Grow volume
    resp = await wsapi_client.put(
        "/api/v1/volumes/grow_vol",
        headers=headers,
        json={"action": "growvv", "sizeMiB": 512},
    )
    assert resp.status_code == 200

    # Verify new size
    resp = await wsapi_client.get("/api/v1/volumes/grow_vol", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["sizeMiB"] == 1536

    # Cleanup
    resp = await wsapi_client.delete("/api/v1/volumes/grow_vol", headers=headers)
    assert resp.status_code == 200


async def test_fc_host_lifecycle(wsapi_client):
    """FC host creation with WWPNs and modify (add port)."""
    key = await _auth(wsapi_client)
    headers = {SESSION_HEADER: key}

    # Create FC host
    resp = await wsapi_client.post(
        "/api/v1/hosts",
        headers=headers,
        json={
            "name": "fchost01",
            "FCWWNs": ["10:00:00:00:00:00:00:AA", "10:00:00:00:00:00:00:BB"],
        },
    )
    assert resp.status_code == 201

    # Add another WWPN via modify
    resp = await wsapi_client.put(
        "/api/v1/hosts/fchost01",
        headers=headers,
        json={
            "pathOperation": 1,
            "FCWWNs": ["10:00:00:00:00:00:00:CC"],
        },
    )
    assert resp.status_code == 200

    # Cleanup
    resp = await wsapi_client.delete("/api/v1/hosts/fchost01", headers=headers)
    assert resp.status_code == 200


async def test_duplicate_volume_rejected(wsapi_client):
    """Creating a volume with the same name twice returns 409."""
    key = await _auth(wsapi_client)
    headers = {SESSION_HEADER: key}

    resp = await wsapi_client.post(
        "/api/v1/volumes",
        headers=headers,
        json={"name": "dup_vol", "cpg": "cpg0", "sizeMiB": 512},
    )
    assert resp.status_code == 201

    resp = await wsapi_client.post(
        "/api/v1/volumes",
        headers=headers,
        json={"name": "dup_vol", "cpg": "cpg0", "sizeMiB": 512},
    )
    assert resp.status_code == 409

    # Cleanup
    resp = await wsapi_client.delete("/api/v1/volumes/dup_vol", headers=headers)
    assert resp.status_code == 200


async def test_nonexistent_volume_returns_404(wsapi_client):
    key = await _auth(wsapi_client)
    resp = await wsapi_client.get(
        "/api/v1/volumes/nosuch",
        headers={SESSION_HEADER: key},
    )
    assert resp.status_code == 404


async def test_nonexistent_host_returns_404(wsapi_client):
    key = await _auth(wsapi_client)
    resp = await wsapi_client.get(
        "/api/v1/hosts/nosuch",
        headers={SESSION_HEADER: key},
    )
    assert resp.status_code == 404
