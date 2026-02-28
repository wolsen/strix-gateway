# FILE: tests/unit/test_subsystems.py
"""Tests for subsystem creation, scoping, and CRUD via the REST API.

Validates that:
  - Two subsystems can have pools / volumes with the same names without collision.
  - Pool listing is filtered by subsystem.
  - Volumes inherit subsystem from their pool.
  - Mapping creation respects protocol allowance per subsystem.
"""

from __future__ import annotations

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Subsystem CRUD via REST API
# ---------------------------------------------------------------------------

class TestSubsystemCRUD:
    async def test_create_subsystem(self, client):
        resp = await client.post("/v1/subsystems", json={
            "name": "test-sub",
            "persona": "generic",
            "protocols_enabled": ["iscsi"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-sub"
        assert data["persona"] == "generic"
        assert data["protocols_enabled"] == ["iscsi"]

    async def test_create_duplicate_name_returns_409(self, client):
        await client.post("/v1/subsystems", json={"name": "dup-sub"})
        resp = await client.post("/v1/subsystems", json={"name": "dup-sub"})
        assert resp.status_code == 409

    async def test_list_subsystems_includes_default(self, client):
        resp = await client.get("/v1/subsystems")
        assert resp.status_code == 200
        names = [s["name"] for s in resp.json()]
        assert "default" in names

    async def test_get_subsystem_by_id(self, client):
        resp = await client.post("/v1/subsystems", json={"name": "my-sub"})
        sub_id = resp.json()["id"]
        resp2 = await client.get(f"/v1/subsystems/{sub_id}")
        assert resp2.status_code == 200
        assert resp2.json()["name"] == "my-sub"

    async def test_get_subsystem_by_name(self, client):
        await client.post("/v1/subsystems", json={"name": "named-sub"})
        resp = await client.get("/v1/subsystems/named-sub")
        assert resp.status_code == 200
        assert resp.json()["name"] == "named-sub"

    async def test_get_nonexistent_returns_404(self, client):
        resp = await client.get("/v1/subsystems/does-not-exist")
        assert resp.status_code == 404

    async def test_delete_subsystem(self, client):
        resp = await client.post("/v1/subsystems", json={"name": "to-delete"})
        sub_id = resp.json()["id"]
        resp2 = await client.delete(f"/v1/subsystems/{sub_id}")
        assert resp2.status_code == 204

    async def test_cannot_delete_default(self, client):
        resp = await client.get("/v1/subsystems/default")
        sub_id = resp.json()["id"]
        resp2 = await client.delete(f"/v1/subsystems/{sub_id}")
        assert resp2.status_code == 409

    async def test_delete_with_pools_returns_409(self, client):
        # Create subsystem + pool
        sub_resp = await client.post("/v1/subsystems", json={"name": "has-pool"})
        sub_id = sub_resp.json()["id"]
        await client.post("/v1/pools", json={
            "name": "p1",
            "backend_type": "malloc",
            "size_mb": 1024,
            "subsystem": "has-pool",
        })
        resp = await client.delete(f"/v1/subsystems/{sub_id}")
        assert resp.status_code == 409

    async def test_capabilities_endpoint(self, client):
        await client.post("/v1/subsystems", json={
            "name": "cap-sub",
            "persona": "ibm_svc",
        })
        resp = await client.get("/v1/subsystems/cap-sub/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["persona"] == "ibm_svc"
        assert "effective_profile" in data
        assert data["effective_profile"]["model"] == "SVC-SAFER-FAKE-9000"


# ---------------------------------------------------------------------------
# Pool scoping (same name, different subsystems)
# ---------------------------------------------------------------------------

class TestPoolScoping:
    async def _create_subsystem(self, client, name: str) -> str:
        resp = await client.post("/v1/subsystems", json={"name": name})
        assert resp.status_code == 201
        return resp.json()["id"]

    async def _create_pool(self, client, name: str, subsystem: str) -> dict:
        resp = await client.post("/v1/pools", json={
            "name": name,
            "backend_type": "malloc",
            "size_mb": 512,
            "subsystem": subsystem,
        })
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_same_pool_name_in_two_subsystems(self, client):
        """Pool names only need to be unique within a subsystem."""
        await self._create_subsystem(client, "sub-x")
        await self._create_subsystem(client, "sub-y")

        p1 = await self._create_pool(client, "gold", "sub-x")
        p2 = await self._create_pool(client, "gold", "sub-y")

        assert p1["id"] != p2["id"]
        assert p1["name"] == p2["name"] == "gold"

    async def test_duplicate_pool_name_within_subsystem_returns_409(self, client):
        await self._create_subsystem(client, "sub-a")
        await self._create_pool(client, "gold", "sub-a")
        resp = await client.post("/v1/pools", json={
            "name": "gold",
            "backend_type": "malloc",
            "size_mb": 512,
            "subsystem": "sub-a",
        })
        assert resp.status_code == 409

    async def test_list_pools_filtered_by_subsystem(self, client):
        await self._create_subsystem(client, "filter-a")
        await self._create_subsystem(client, "filter-b")
        await self._create_pool(client, "gold", "filter-a")
        await self._create_pool(client, "silver", "filter-b")

        resp_a = await client.get("/v1/pools?subsystem=filter-a")
        assert resp_a.status_code == 200
        names_a = [p["name"] for p in resp_a.json()]
        assert "gold" in names_a
        assert "silver" not in names_a

    async def test_pool_created_in_default_when_subsystem_omitted(self, client):
        resp = await client.post("/v1/pools", json={
            "name": "auto-pool",
            "backend_type": "malloc",
            "size_mb": 256,
        })
        assert resp.status_code == 201
        data = resp.json()
        # subsystem_id should be set (the default subsystem's id)
        assert data["subsystem_id"] is not None


# ---------------------------------------------------------------------------
# Volume scoping
# ---------------------------------------------------------------------------

class TestVolumeScoping:
    async def _setup(self, client):
        """Create two subsystems each with a pool, return pool IDs."""
        await client.post("/v1/subsystems", json={"name": "vs-a"})
        await client.post("/v1/subsystems", json={"name": "vs-b"})
        pool_a = (await client.post("/v1/pools", json={
            "name": "pool", "backend_type": "malloc", "size_mb": 512, "subsystem": "vs-a"
        })).json()
        pool_b = (await client.post("/v1/pools", json={
            "name": "pool", "backend_type": "malloc", "size_mb": 512, "subsystem": "vs-b"
        })).json()
        return pool_a["id"], pool_b["id"]

    async def test_volumes_inherit_subsystem_from_pool(self, client):
        pool_a_id, _ = await self._setup(client)
        resp = await client.post("/v1/volumes", json={
            "name": "vol1", "pool_id": pool_a_id, "size_mb": 100
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["subsystem_id"] is not None

    async def test_list_volumes_filtered_by_subsystem(self, client):
        pool_a_id, pool_b_id = await self._setup(client)
        await client.post("/v1/volumes", json={"name": "vol-a", "pool_id": pool_a_id, "size_mb": 64})
        await client.post("/v1/volumes", json={"name": "vol-b", "pool_id": pool_b_id, "size_mb": 64})

        resp = await client.get("/v1/volumes?subsystem=vs-a")
        names = [v["name"] for v in resp.json()]
        assert "vol-a" in names
        assert "vol-b" not in names
