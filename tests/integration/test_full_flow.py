# FILE: tests/integration/test_full_flow.py
"""Integration tests — full create/map/connection-info flow for both protocols.

The SPDK client is mocked so no SPDK daemon is needed.  The tests exercise the
complete HTTP → DB → ensure_* path and verify the connection-info response
shapes match the OpenStack Cinder initialize_connection format.
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
        "size_mb": 1024,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_host(
    client: AsyncClient,
    name: str = "test-host",
    iqn: str | None = "iqn.1993-08.org.debian:test",
    nqn: str | None = "nqn.2014-08.org.nvmexpress:uuid:test",
) -> dict:
    resp = await client.post("/v1/hosts", json={"name": name, "iqn": iqn, "nqn": nqn})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_mapping(client: AsyncClient, volume_id: str, host_id: str, protocol: str) -> dict:
    resp = await client.post("/v1/mappings", json={
        "volume_id": volume_id,
        "host_id": host_id,
        "protocol": protocol,
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
    mapping = await _create_mapping(client, volume["id"], host["id"], "iscsi")

    assert mapping["protocol"] == "iscsi"
    assert mapping["lun_id"] == 0
    assert mapping["ns_id"] is None

    resp = await client.get(f"/v1/mappings/{mapping['id']}/connection-info")
    assert resp.status_code == 200, resp.text

    info = resp.json()
    assert info["driver_volume_type"] == "iscsi"
    data = info["data"]
    assert "target_iqn" in data
    assert data["target_iqn"].startswith("iqn.2026-02.lunacysystems.apollo:")
    assert "target_portal" in data
    assert data["target_lun"] == 0
    assert data["access_mode"] == "rw"
    assert data["discard"] is True


# ---------------------------------------------------------------------------
# NVMe-oF TCP full flow
# ---------------------------------------------------------------------------

async def test_nvmef_full_flow(client: AsyncClient):
    pool = await _create_pool(client, "nvmef-pool")
    volume = await _create_volume(client, pool["id"])
    host = await _create_host(client, name="nvmef-host")
    mapping = await _create_mapping(client, volume["id"], host["id"], "nvmeof_tcp")

    assert mapping["protocol"] == "nvmeof_tcp"
    assert mapping["ns_id"] == 1
    assert mapping["lun_id"] is None

    resp = await client.get(f"/v1/mappings/{mapping['id']}/connection-info")
    assert resp.status_code == 200, resp.text

    info = resp.json()
    assert info["driver_volume_type"] == "nvmeof"
    data = info["data"]
    assert "target_nqn" in data
    assert data["target_nqn"].startswith("nqn.2026-02.io.lunacysystems:apollo:")
    assert data["transport_type"] == "tcp"
    assert "target_portal" in data
    assert data["ns_id"] == 1
    assert data["access_mode"] == "rw"


# ---------------------------------------------------------------------------
# LUN allocation: two volumes mapped to the same host via iSCSI
# ---------------------------------------------------------------------------

async def test_lun_allocation_sequential(client: AsyncClient):
    pool = await _create_pool(client, "lun-pool")
    vol1 = await _create_volume(client, pool["id"], "lun-vol-1")
    vol2 = await _create_volume(client, pool["id"], "lun-vol-2")
    host = await _create_host(client, name="lun-host")

    m1 = await _create_mapping(client, vol1["id"], host["id"], "iscsi")
    m2 = await _create_mapping(client, vol2["id"], host["id"], "iscsi")

    assert m1["lun_id"] == 0
    assert m2["lun_id"] == 1

    # Both share the same export container
    assert m1["export_container_id"] == m2["export_container_id"]


# ---------------------------------------------------------------------------
# NSID allocation: two volumes mapped to the same host via NVMe-oF
# ---------------------------------------------------------------------------

async def test_nsid_allocation_sequential(client: AsyncClient):
    pool = await _create_pool(client, "ns-pool")
    vol1 = await _create_volume(client, pool["id"], "ns-vol-1")
    vol2 = await _create_volume(client, pool["id"], "ns-vol-2")
    host = await _create_host(client, name="ns-host")

    m1 = await _create_mapping(client, vol1["id"], host["id"], "nvmeof_tcp")
    m2 = await _create_mapping(client, vol2["id"], host["id"], "nvmeof_tcp")

    assert m1["ns_id"] == 1
    assert m2["ns_id"] == 2

    assert m1["export_container_id"] == m2["export_container_id"]


# ---------------------------------------------------------------------------
# Volume lifecycle
# ---------------------------------------------------------------------------

async def test_volume_lifecycle(client: AsyncClient):
    pool = await _create_pool(client, "lc-pool")
    vol = await _create_volume(client, pool["id"])
    assert vol["status"] == "available"

    # Extend
    resp = await client.post(f"/v1/volumes/{vol['id']}/extend", json={"new_size_mb": 2048})
    assert resp.status_code == 200
    assert resp.json()["size_mb"] == 2048
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
        "size_mb": 512,
    })
    assert resp.status_code == 500

    # Clear fault
    await client.delete("/admin/faults/create_volume")

    # Should succeed now
    resp = await client.post("/v1/volumes", json={
        "name": "should-succeed",
        "pool_id": pool["id"],
        "size_mb": 512,
    })
    assert resp.status_code == 201
