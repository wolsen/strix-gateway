# FILE: tests/integration/test_full_flow.py
"""Integration tests — full create/map/attachments flow.

The SPDK client is mocked so no SPDK daemon is needed.  The tests exercise the
complete HTTP → DB → ensure_* path.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_pool(client: AsyncClient, name: str = "test-pool") -> dict:
    resp = await client.post("/v1/pools", json={
        "name": name,
        "backend_type": "malloc",
        "size_mb": 4096,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_volume(client: AsyncClient, pool_id: str, name: str = "test-vol") -> dict:
    resp = await client.post("/v1/volumes", json={
        "name": name,
        "pool_id": pool_id,
        "size_gb": 1,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_host(
    client: AsyncClient,
    name: str = "test-host",
    iqns: list[str] | None = None,
    nqns: list[str] | None = None,
    wwpns: list[str] | None = None,
) -> dict:
    resp = await client.post("/v1/hosts", json={
        "name": name,
        "initiators_iscsi_iqns": iqns or ["iqn.1993-08.org.debian:test"],
        "initiators_nvme_host_nqns": nqns or ["nqn.2014-08.org.nvmexpress:uuid:test"],
        "initiators_fc_wwpns": wwpns or [],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_mapping(
    client: AsyncClient, volume_id: str, host_id: str,
    persona_protocol: str = "iscsi", underlay_protocol: str = "iscsi",
) -> dict:
    resp = await client.post("/v1/mappings", json={
        "volume_id": volume_id,
        "host_id": host_id,
        "persona_protocol": persona_protocol,
        "underlay_protocol": underlay_protocol,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# iSCSI full flow
# ---------------------------------------------------------------------------

async def test_iscsi_full_flow(client: AsyncClient):
    pool = await _create_pool(client, "iscsi-pool")
    volume = await _create_volume(client, pool["id"])
    host = await _create_host(client)
    mapping = await _create_mapping(client, volume["id"], host["id"])

    assert mapping["lun_id"] == 0
    assert mapping["desired_state"] == "attached"

    # Verify host attachments endpoint
    resp = await client.get(f"/v1/hosts/{host['id']}/attachments")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["attachments"]) >= 1


# ---------------------------------------------------------------------------
# LUN allocation: two volumes mapped to the same host
# ---------------------------------------------------------------------------

async def test_lun_allocation_sequential(client: AsyncClient):
    pool = await _create_pool(client, "lun-pool")
    vol1 = await _create_volume(client, pool["id"], "lun-vol-1")
    vol2 = await _create_volume(client, pool["id"], "lun-vol-2")
    host = await _create_host(client, name="lun-host")

    m1 = await _create_mapping(client, vol1["id"], host["id"])
    m2 = await _create_mapping(client, vol2["id"], host["id"])

    assert m1["lun_id"] == 0
    assert m2["lun_id"] == 1


# ---------------------------------------------------------------------------
# Volume lifecycle
# ---------------------------------------------------------------------------

async def test_volume_lifecycle(client: AsyncClient):
    pool = await _create_pool(client, "lc-pool")
    vol = await _create_volume(client, pool["id"])
    assert vol["status"] == "available"

    # Extend
    resp = await client.post(f"/v1/volumes/{vol['id']}/extend", json={"new_size_gb": 2})
    assert resp.status_code == 200
    assert resp.json()["size_gb"] == 2
    assert resp.json()["status"] == "available"

    # Delete
    resp = await client.delete(f"/v1/volumes/{vol['id']}")
    assert resp.status_code == 204

    # Confirm gone
    resp = await client.get(f"/v1/volumes/{vol['id']}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------

async def test_fault_injection_create_volume(client: AsyncClient):
    pool = await _create_pool(client, "fault-pool")

    # Inject fault
    resp = await client.post("/admin/faults", json={
        "operation": "create_volume",
        "error_message": "simulated volume creation failure",
    })
    assert resp.status_code == 201

    resp = await client.post("/v1/volumes", json={
        "name": "should-fail",
        "pool_id": pool["id"],
        "size_gb": 1,
    })
    assert resp.status_code == 500

    # Clear fault
    await client.delete("/admin/faults/create_volume")

    # Should succeed now
    resp = await client.post("/v1/volumes", json={
        "name": "should-succeed",
        "pool_id": pool["id"],
        "size_gb": 1,
    })
    assert resp.status_code == 201
