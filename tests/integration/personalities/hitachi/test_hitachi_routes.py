# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Integration tests — Hitachi Configuration Manager API surface.

Simulates the full Cinder driver workflow: auth → create LDEV → map →
unmap → delete → logout.  Uses in-memory SQLite + mocked SPDK.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from strix_gateway.core.db import (
    Array,
    Base,
    Pool,
    TransportEndpoint,
    get_session_factory,
    init_db,
)
from strix_gateway.personalities.hitachi.jobs import JobTracker
from strix_gateway.personalities.hitachi.sessions import SessionStore
from strix_gateway.personalities.hitachi.translate import HitachiIdMapper

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def hitachi_client():
    """AsyncClient wired to the Hitachi sub-app with a seeded Hitachi array."""
    await init_db(TEST_DATABASE_URL)

    factory = get_session_factory()

    # Seed a Hitachi array with pool + endpoints
    async with factory() as session:
        arr = Array(name="hitachi-a", vendor="hitachi")
        session.add(arr)
        await session.flush()

        pool = Pool(
            name="pool0", array_id=arr.id, backend_type="malloc",
            size_mb=8192, vendor_metadata=json.dumps({"pool_id": 0}),
        )
        session.add(pool)

        for proto in ("iscsi", "fc"):
            ep = TransportEndpoint(
                array_id=arr.id, protocol=proto,
                targets=json.dumps(
                    {"target_iqn": "iqn.strix.test"} if proto == "iscsi"
                    else {"target_wwpns": ["50:00:00:00:00:00:00:01"]}
                ),
                addresses=json.dumps(
                    {"portals": ["10.0.0.1:3260"]} if proto == "iscsi"
                    else {}
                ),
            )
            session.add(ep)

        await session.commit()

        array_id = arr.id

    # Build mapper
    async with factory() as session:
        mapper = HitachiIdMapper(array_id)
        await mapper.rebuild(session)
        await session.commit()

    # Build sub-app
    from strix_gateway.personalities.hitachi.routes import router as hitachi_router
    from strix_gateway.core.exceptions import CoreError
    from strix_gateway.personalities.hitachi.errors import hitachi_error_response
    from fastapi import FastAPI
    from starlette.requests import Request

    # Fake array info for scope injection
    from dataclasses import dataclass

    @dataclass
    class FakeArrayInfo:
        id: str
        name: str
        fqdn: str
        vendor: str

    array_info = FakeArrayInfo(
        id=array_id, name="hitachi-a",
        fqdn="hitachi-a.gw01.lab.example", vendor="hitachi",
    )

    hitachi_app = FastAPI()
    hitachi_app.state.hitachi_sessions = SessionStore()
    hitachi_app.state.hitachi_jobs = JobTracker()
    hitachi_app.state.hitachi_mappers = {array_id: mapper}
    hitachi_app.state.spdk_client = MagicMock()
    hitachi_app.state.spdk_client.call = MagicMock(return_value=None)

    @hitachi_app.exception_handler(CoreError)
    async def _handle_core_error(request: Request, exc: CoreError):
        return hitachi_error_response(request, exc)

    # Inject array context into every request
    @hitachi_app.middleware("http")
    async def inject_array(request: Request, call_next):
        request.scope.setdefault("state", {})
        request.scope["state"]["array"] = array_info
        return await call_next(request)

    hitachi_app.include_router(hitachi_router)

    async with AsyncClient(
        transport=ASGITransport(app=hitachi_app),
        base_url="http://test",
    ) as client:
        yield client


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def test_create_and_delete_session(hitachi_client):
    # Create session
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/sessions",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "sessionId" in data
    assert "token" in data

    # Delete session
    sid = data["sessionId"]
    resp = await hitachi_client.delete(
        f"/ConfigurationManager/v1/objects/sessions/{sid}",
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Storage systems
# ---------------------------------------------------------------------------

async def test_list_storages(hitachi_client):
    # Get token
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/sessions",
    )
    token = resp.json()["token"]

    resp = await hitachi_client.get(
        "/ConfigurationManager/v1/objects/storages",
        headers={"Authorization": f"Session {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "data" in data
    assert len(data["data"]) == 1
    assert "storageDeviceId" in data["data"][0]


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

async def test_list_pools(hitachi_client):
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/sessions",
    )
    token = resp.json()["token"]

    resp = await hitachi_client.get(
        "/ConfigurationManager/v1/objects/pools",
        headers={"Authorization": f"Session {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "data" in data
    assert len(data["data"]) >= 1
    assert data["data"][0]["poolId"] == 0


# ---------------------------------------------------------------------------
# Full LDEV lifecycle
# ---------------------------------------------------------------------------

async def test_ldev_create_list_delete(hitachi_client):
    # Auth
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/sessions",
    )
    token = resp.json()["token"]
    headers = {"Authorization": f"Session {token}"}

    # Create LDEV — 1 GB
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/ldevs",
        headers=headers,
        json={"poolId": 0, "byteFormatCapacity": str(1024 * 1024 * 1024)},
    )
    assert resp.status_code == 202, resp.text
    job_data = resp.json()
    assert "jobId" in job_data
    assert job_data["affectedResources"] == [
        "/ConfigurationManager/v1/objects/ldevs/0",
    ]

    # Poll job
    resp = await hitachi_client.get(
        f"/ConfigurationManager/v1/objects/jobs/{job_data['jobId']}",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "Completed"
    assert resp.json()["state"] == "Succeeded"

    # List LDEVs
    resp = await hitachi_client.get(
        "/ConfigurationManager/v1/objects/ldevs",
        headers=headers,
    )
    assert resp.status_code == 200
    ldevs = resp.json()["data"]
    assert len(ldevs) >= 1
    ldev_id = ldevs[0]["ldevId"]
    assert ldev_id == 0

    # Get single LDEV
    resp = await hitachi_client.get(
        f"/ConfigurationManager/v1/objects/ldevs/{ldev_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["ldevId"] == ldev_id
    assert resp.json()["emulationType"] == "OPEN-V"

    # Rename LDEV (Cinder calls PUT /ldevs/{id} with label)
    resp = await hitachi_client.put(
        f"/ConfigurationManager/v1/objects/ldevs/{ldev_id}",
        headers=headers,
        json={"label": "renamed-ldev"},
    )
    assert resp.status_code == 202, resp.text

    # Verify rename persisted
    resp = await hitachi_client.get(
        f"/ConfigurationManager/v1/objects/ldevs/{ldev_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "renamed-ldev"

    # Delete LDEV
    resp = await hitachi_client.delete(
        f"/ConfigurationManager/v1/objects/ldevs/{ldev_id}",
        headers=headers,
    )
    assert resp.status_code == 202

    # LDEV should be gone
    resp = await hitachi_client.get(
        f"/ConfigurationManager/v1/objects/ldevs/{ldev_id}",
        headers=headers,
    )
    assert resp.status_code == 404


async def test_ldev_create_accepts_unit_capacity(hitachi_client):
    resp = await hitachi_client.post("/ConfigurationManager/v1/objects/sessions")
    token = resp.json()["token"]
    headers = {"Authorization": f"Session {token}"}

    # Cinder sends capacities like "1G" for LDEV creation.
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/ldevs",
        headers=headers,
        json={"poolId": 0, "byteFormatCapacity": "1G"},
    )
    assert resp.status_code == 202, resp.text


# ---------------------------------------------------------------------------
# Host groups + WWN
# ---------------------------------------------------------------------------

async def test_host_group_lifecycle(hitachi_client):
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/sessions",
    )
    token = resp.json()["token"]
    headers = {"Authorization": f"Session {token}"}

    # List ports to find FC port
    resp = await hitachi_client.get(
        "/ConfigurationManager/v1/objects/ports",
        headers=headers,
    )
    assert resp.status_code == 200
    ports = resp.json()["data"]
    fc_port = next((p for p in ports if p["portType"] == "FIBRE"), None)
    assert fc_port is not None
    port_id = fc_port["portId"]

    # Create host group
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/host-groups",
        headers=headers,
        json={"portId": port_id, "hostGroupName": "compute-01"},
    )
    assert resp.status_code in (200, 201, 202)

    # List host groups
    resp = await hitachi_client.get(
        f"/ConfigurationManager/v1/objects/host-groups?portId={port_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    hgs = resp.json()["data"]
    assert len(hgs) >= 1
    hg_id = hgs[0]["hostGroupId"]

    # Add WWN
    resp = await hitachi_client.post(
        f"/ConfigurationManager/v1/objects/host-groups/{hg_id}/wwns",
        headers=headers,
        json={"hostWwn": "10:00:00:00:00:00:00:99"},
    )
    assert resp.status_code == 202

    # List WWNs
    resp = await hitachi_client.get(
        f"/ConfigurationManager/v1/objects/host-groups/{hg_id}/wwns",
        headers=headers,
    )
    assert resp.status_code == 200
    wwns = resp.json()["data"]
    assert any(w["hostWwn"] == "10:00:00:00:00:00:00:99" for w in wwns)

    # Delete host group
    resp = await hitachi_client.delete(
        f"/ConfigurationManager/v1/objects/host-groups/{hg_id}",
        headers=headers,
    )
    assert resp.status_code in (200, 202)


async def test_iscsi_host_group_includes_iscsi_name(hitachi_client):
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/sessions",
    )
    token = resp.json()["token"]
    headers = {"Authorization": f"Session {token}"}

    # Select iSCSI port and create host group without explicit iscsiName.
    resp = await hitachi_client.get(
        "/ConfigurationManager/v1/objects/ports",
        headers=headers,
    )
    assert resp.status_code == 200
    ports = resp.json()["data"]
    iscsi_port = next((p for p in ports if p["portType"] == "ISCSI"), None)
    assert iscsi_port is not None
    port_id = iscsi_port["portId"]

    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/host-groups",
        headers=headers,
        json={"portId": port_id, "hostGroupName": "iscsi-target-01"},
    )
    assert resp.status_code in (200, 201, 202), resp.text

    resp = await hitachi_client.get(
        f"/ConfigurationManager/v1/objects/host-groups?portId={port_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    host_groups = resp.json()["data"]
    created = next((h for h in host_groups if h["hostGroupName"] == "iscsi-target-01"), None)
    assert created is not None
    assert created["iscsiName"] == "iqn.strix.test"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

async def test_job_not_found(hitachi_client):
    resp = await hitachi_client.post(
        "/ConfigurationManager/v1/objects/sessions",
    )
    token = resp.json()["token"]

    resp = await hitachi_client.get(
        "/ConfigurationManager/v1/objects/jobs/99999",
        headers={"Authorization": f"Session {token}"},
    )
    assert resp.status_code == 404
