# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Integration tests covering error paths and validation branches in api/v1.py."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

import apollo_gateway.core.faults as fault_engine
from apollo_gateway.spdk.rpc import SPDKError

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _pool(client, name="p", backend_type="malloc", size_mb=1024, aio_path=None):
    body = {"name": name, "backend_type": backend_type, "size_mb": size_mb}
    if aio_path:
        body["aio_path"] = aio_path
    r = await client.post("/v1/pools", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _volume(client, pool_id, name="v", size_gb=1):
    r = await client.post("/v1/volumes", json={"name": name, "pool_id": pool_id, "size_gb": size_gb})
    assert r.status_code == 201, r.text
    return r.json()


async def _host(client, name="h",
                iqns=None, nqns=None, wwpns=None):
    r = await client.post("/v1/hosts", json={
        "name": name,
        "initiators_iscsi_iqns": iqns or ["iqn.test:h"],
        "initiators_nvme_host_nqns": nqns or ["nqn.test:h"],
        "initiators_fc_wwpns": wwpns or [],
    })
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# POST /v1/pools — validation errors
# ===========================================================================

async def test_create_pool_malloc_requires_size_mb(client: AsyncClient):
    r = await client.post("/v1/pools", json={"name": "p", "backend_type": "malloc"})
    assert r.status_code == 400
    assert "size_mb" in r.json()["detail"]


async def test_create_pool_aio_file_requires_aio_path(client: AsyncClient):
    r = await client.post("/v1/pools", json={"name": "p", "backend_type": "aio_file", "size_mb": 1024})
    assert r.status_code == 400
    assert "aio_path" in r.json()["detail"]


async def test_create_pool_duplicate_name(client: AsyncClient):
    await _pool(client, name="dup")
    r = await client.post("/v1/pools", json={"name": "dup", "backend_type": "malloc", "size_mb": 1024})
    assert r.status_code == 409
    assert "dup" in r.json()["detail"]


async def test_create_pool_spdk_failure(client: AsyncClient, mock_spdk):
    mock_spdk.call.side_effect = SPDKError(-1, "malloc bdev failed")
    r = await client.post("/v1/pools", json={"name": "fail-pool", "backend_type": "malloc", "size_mb": 512})
    assert r.status_code == 500
    assert "SPDK error" in r.json()["detail"]


async def test_create_pool_aio_file_success(client: AsyncClient):
    r = await client.post("/v1/pools", json={
        "name": "aio-pool",
        "backend_type": "aio_file",
        "aio_path": "/tmp/disk.img",
    })
    assert r.status_code == 201
    data = r.json()
    assert data["backend_type"] == "aio_file"
    assert data["aio_path"] == "/tmp/disk.img"


# ===========================================================================
# GET /v1/pools
# ===========================================================================

async def test_list_pools_returns_all(client: AsyncClient):
    await _pool(client, name="p1")
    await _pool(client, name="p2")
    r = await client.get("/v1/pools")
    assert r.status_code == 200
    names = [p["name"] for p in r.json()]
    assert "p1" in names
    assert "p2" in names


# ===========================================================================
# POST /v1/volumes — error paths
# ===========================================================================

async def test_create_volume_pool_not_found(client: AsyncClient):
    r = await client.post("/v1/volumes", json={"name": "v", "pool_id": "nonexistent", "size_gb": 1})
    assert r.status_code == 404


async def test_create_volume_spdk_failure(client: AsyncClient, mock_spdk):
    pool = await _pool(client)
    mock_spdk.call.side_effect = SPDKError(-1, "lvol create failed")
    r = await client.post("/v1/volumes", json={"name": "v", "pool_id": pool["id"], "size_gb": 1})
    assert r.status_code == 500
    # Volume should be in error state in DB
    mock_spdk.call.side_effect = None


# ===========================================================================
# GET /v1/volumes/{id}
# ===========================================================================

async def test_get_volume_not_found(client: AsyncClient):
    r = await client.get("/v1/volumes/does-not-exist")
    assert r.status_code == 404


# ===========================================================================
# DELETE /v1/volumes/{id} — error paths
# ===========================================================================

async def test_delete_volume_not_found(client: AsyncClient):
    r = await client.delete("/v1/volumes/does-not-exist")
    assert r.status_code == 404


async def test_delete_volume_with_active_mapping_rejected(client: AsyncClient):
    pool = await _pool(client, name="del-pool")
    vol = await _volume(client, pool["id"])
    host = await _host(client, name="del-host")
    await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host["id"],
        "persona_protocol": "iscsi", "underlay_protocol": "iscsi",
    })
    r = await client.delete(f"/v1/volumes/{vol['id']}")
    assert r.status_code == 409
    assert "mapping" in r.json()["detail"].lower()


async def test_delete_volume_spdk_failure(client: AsyncClient, mock_spdk):
    pool = await _pool(client, name="spdk-del-pool")
    vol = await _volume(client, pool["id"])
    mock_spdk.call.side_effect = SPDKError(-1, "delete lvol failed")
    r = await client.delete(f"/v1/volumes/{vol['id']}")
    assert r.status_code == 500
    mock_spdk.call.side_effect = None


# ===========================================================================
# POST /v1/volumes/{id}/extend — error paths
# ===========================================================================

async def test_extend_volume_not_found(client: AsyncClient):
    r = await client.post("/v1/volumes/missing/extend", json={"new_size_gb": 2})
    assert r.status_code == 404


async def test_extend_volume_size_not_larger(client: AsyncClient):
    pool = await _pool(client, name="ext-pool")
    vol = await _volume(client, pool["id"], size_gb=1)
    r = await client.post(f"/v1/volumes/{vol['id']}/extend", json={"new_size_gb": 1})
    assert r.status_code == 400


async def test_extend_volume_spdk_failure(client: AsyncClient, mock_spdk):
    pool = await _pool(client, name="ext-fail-pool")
    vol = await _volume(client, pool["id"], size_gb=1)
    mock_spdk.call.side_effect = SPDKError(-1, "resize failed")
    r = await client.post(f"/v1/volumes/{vol['id']}/extend", json={"new_size_gb": 2})
    assert r.status_code == 500
    mock_spdk.call.side_effect = None


# ===========================================================================
# GET /v1/hosts
# ===========================================================================

async def test_list_hosts(client: AsyncClient):
    await _host(client, name="h1", iqns=["iqn.test:h1"], nqns=["nqn.test:h1"])
    await _host(client, name="h2", iqns=["iqn.test:h2"], nqns=["nqn.test:h2"])
    r = await client.get("/v1/hosts")
    assert r.status_code == 200
    names = [h["name"] for h in r.json()]
    assert "h1" in names
    assert "h2" in names


# ===========================================================================
# POST /v1/mappings — error paths
# ===========================================================================

async def test_create_mapping_volume_not_found(client: AsyncClient):
    host = await _host(client)
    r = await client.post("/v1/mappings", json={
        "volume_id": "missing", "host_id": host["id"],
        "persona_protocol": "iscsi", "underlay_protocol": "iscsi",
    })
    assert r.status_code == 404


async def test_create_mapping_volume_wrong_status(client: AsyncClient, mock_spdk):
    pool = await _pool(client, name="status-pool")
    vol = await _volume(client, pool["id"])
    # Trigger an extend failure so volume transitions to error state
    mock_spdk.call.side_effect = SPDKError(-1, "resize failed")
    r = await client.post(f"/v1/volumes/{vol['id']}/extend", json={"new_size_gb": 2})
    assert r.status_code == 500
    mock_spdk.call.side_effect = None

    host = await _host(client, name="status-host")
    r = await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host["id"],
        "persona_protocol": "iscsi", "underlay_protocol": "iscsi",
    })
    assert r.status_code == 409


async def test_create_mapping_host_not_found(client: AsyncClient):
    pool = await _pool(client, name="hm-pool")
    vol = await _volume(client, pool["id"])
    r = await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": "missing",
        "persona_protocol": "iscsi", "underlay_protocol": "iscsi",
    })
    assert r.status_code == 404


async def test_create_mapping_spdk_export_failure(client: AsyncClient, mock_spdk):
    pool = await _pool(client, name="exp-fail-pool")
    vol = await _volume(client, pool["id"])
    host = await _host(client, name="exp-fail-host")
    mock_spdk.call.side_effect = SPDKError(-1, "create target failed")
    r = await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host["id"],
        "persona_protocol": "iscsi", "underlay_protocol": "iscsi",
    })
    assert r.status_code == 500
    mock_spdk.call.side_effect = None


async def test_create_mapping_spdk_attach_noop_empty_targets(client: AsyncClient, mock_spdk):
    """With empty transport-endpoint targets, iSCSI mapping is a no-op."""
    pool = await _pool(client, name="attach-fail-pool")
    vol = await _volume(client, pool["id"])
    host = await _host(client, name="attach-fail-host")

    # Even with a selective side_effect, there is nothing to attach to
    # when the transport endpoint has no target_iqn configured.
    call_count = 0

    def side_effect(method, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if method == "iscsi_create_target_node":
            raise SPDKError(-1, "create target failed")
        return None

    mock_spdk.call.side_effect = side_effect
    r = await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host["id"],
        "persona_protocol": "iscsi", "underlay_protocol": "iscsi",
    })
    # No target_iqn on endpoint → ensure_iscsi_mapping is a no-op → 201
    assert r.status_code == 201
    mock_spdk.call.side_effect = None


# ===========================================================================
# DELETE /v1/mappings/{id} — error paths
# ===========================================================================

async def test_delete_mapping_not_found(client: AsyncClient):
    r = await client.delete("/v1/mappings/does-not-exist")
    assert r.status_code == 404


async def test_delete_mapping_spdk_noop_empty_targets(client: AsyncClient, mock_spdk):
    """With empty NVMe-oF targets, delete mapping skips SPDK cleanup."""
    pool = await _pool(client, name="dm-pool")
    vol = await _volume(client, pool["id"])
    host = await _host(client, name="dm-host")
    m = (await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host["id"],
        "persona_protocol": "nvmeof_tcp", "underlay_protocol": "nvmeof_tcp",
    })).json()

    mock_spdk.call.side_effect = SPDKError(-1, "remove ns failed")
    r = await client.delete(f"/v1/mappings/{m['id']}")
    # No subsystem_nqn → cleanup is a no-op → 204
    assert r.status_code == 204
    mock_spdk.call.side_effect = None


# ===========================================================================
# DELETE /v1/mappings/{id} — iSCSI path
# ===========================================================================

async def test_delete_iscsi_mapping_success(client: AsyncClient, mock_spdk):
    """Deleting an iSCSI mapping removes the target node and restores volume status."""
    pool = await _pool(client, name="iscsi-del-pool")
    vol = await _volume(client, pool["id"])
    host = await _host(client, name="iscsi-del-host")
    m = (await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host["id"],
        "persona_protocol": "iscsi", "underlay_protocol": "iscsi",
    })).json()

    mock_spdk.call.side_effect = None
    r = await client.delete(f"/v1/mappings/{m['id']}")
    assert r.status_code == 204

    # Volume should be back to available
    r = await client.get(f"/v1/volumes/{vol['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "available"


async def test_delete_iscsi_mapping_noop_empty_targets(client: AsyncClient, mock_spdk):
    """With empty iSCSI targets, SPDK cleanup is skipped → 204."""
    pool = await _pool(client, name="iscsi-del-fail-pool")
    vol = await _volume(client, pool["id"])
    host = await _host(client, name="iscsi-del-fail-host")
    m = (await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host["id"],
        "persona_protocol": "iscsi", "underlay_protocol": "iscsi",
    })).json()

    mock_spdk.call.side_effect = SPDKError(-1, "delete target failed")
    r = await client.delete(f"/v1/mappings/{m['id']}")
    # No target_iqn → cleanup is a no-op → 204
    assert r.status_code == 204
    mock_spdk.call.side_effect = None


# ===========================================================================
# DELETE /v1/volumes/{id} — volume without bdev_name (covers line 170→179)
# ===========================================================================

async def test_delete_volume_without_bdev_name(client: AsyncClient, mock_spdk):
    """A volume that was created in error state (no bdev) can still be deleted."""
    pool = await _pool(client, name="no-bdev-pool")
    # Force volume creation to fail so bdev_name is None and status is error
    mock_spdk.call.side_effect = SPDKError(-1, "lvol create failed")
    r = await client.post("/v1/volumes", json={"name": "no-bdev-vol", "pool_id": pool["id"], "size_gb": 1})
    assert r.status_code == 500
    mock_spdk.call.side_effect = None

    # Find the volume in error state by listing (it exists in DB with error status)
    # We need its id — fetch it from the DB directly via another volume creation that works
    # Actually, the volume was committed with error status but the id was not returned.
    # We'll need to find it via the pool's volumes. Let's create a working volume first
    # to confirm the pool works, then check. Actually, the error volume was committed
    # to DB, let's use sqlalchemy to find it.
    from apollo_gateway.core.db import get_session_factory, Volume
    from sqlalchemy import select
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Volume).where(Volume.name == "no-bdev-vol")
        )
        vol = result.scalar_one()
        assert vol.status == "error"
        assert vol.bdev_name is None
        vol_id = vol.id

    r = await client.delete(f"/v1/volumes/{vol_id}")
    assert r.status_code == 204


# ===========================================================================
# DELETE /v1/mappings — nvmeof success path (volume returns to available)
# ===========================================================================

async def test_delete_nvmeof_mapping_success_restores_volume(client: AsyncClient, mock_spdk):
    """Deleting the last NVMe-oF mapping restores volume to available."""
    pool = await _pool(client, name="nvme-del-pool")
    vol = await _volume(client, pool["id"])
    host = await _host(client, name="nvme-del-host")
    m = (await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host["id"],
        "persona_protocol": "nvmeof_tcp", "underlay_protocol": "nvmeof_tcp",
    })).json()

    mock_spdk.call.side_effect = None
    r = await client.delete(f"/v1/mappings/{m['id']}")
    assert r.status_code == 204

    r = await client.get(f"/v1/volumes/{vol['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "available"


async def test_delete_mapping_volume_stays_in_use_with_remaining(client: AsyncClient, mock_spdk):
    """When other mappings remain, volume stays in_use after deleting one mapping."""
    pool = await _pool(client, name="multi-map-pool")
    vol = await _volume(client, pool["id"])
    host1 = await _host(client, name="multi-host-1", iqns=["iqn.test:h1a"], nqns=["nqn.test:h1a"])
    host2 = await _host(client, name="multi-host-2", iqns=["iqn.test:h2a"], nqns=["nqn.test:h2a"])

    m1 = (await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host1["id"],
        "persona_protocol": "nvmeof_tcp", "underlay_protocol": "nvmeof_tcp",
    })).json()
    m2 = (await client.post("/v1/mappings", json={
        "volume_id": vol["id"], "host_id": host2["id"],
        "persona_protocol": "nvmeof_tcp", "underlay_protocol": "nvmeof_tcp",
    })).json()

    # Delete first mapping — second still exists so volume stays in_use
    mock_spdk.call.side_effect = None
    r = await client.delete(f"/v1/mappings/{m1['id']}")
    assert r.status_code == 204

    r = await client.get(f"/v1/volumes/{vol['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "in_use"
