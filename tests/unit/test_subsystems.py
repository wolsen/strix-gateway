# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for array creation, scoping, and CRUD via the REST API.

Validates that:
  - Two arrays can have pools / volumes with the same names without collision.
  - Pool listing is filtered by array.
  - Volumes inherit array from their pool.
"""

from __future__ import annotations

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Array CRUD via REST API
# ---------------------------------------------------------------------------

class TestArrayCRUD:
    async def test_create_array(self, client):
        resp = await client.post("/v1/arrays", json={
            "name": "test-arr",
            "vendor": "generic",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-arr"
        assert data["vendor"] == "generic"

    async def test_create_duplicate_name_returns_409(self, client):
        await client.post("/v1/arrays", json={"name": "dup-arr"})
        resp = await client.post("/v1/arrays", json={"name": "dup-arr"})
        assert resp.status_code == 409

    async def test_list_arrays_includes_default(self, client):
        resp = await client.get("/v1/arrays")
        assert resp.status_code == 200
        names = [a["name"] for a in resp.json()]
        assert "default" in names

    async def test_get_array_by_id(self, client):
        resp = await client.post("/v1/arrays", json={"name": "my-arr"})
        arr_id = resp.json()["id"]
        resp2 = await client.get(f"/v1/arrays/{arr_id}")
        assert resp2.status_code == 200
        assert resp2.json()["name"] == "my-arr"

    async def test_get_array_by_name(self, client):
        await client.post("/v1/arrays", json={"name": "named-arr"})
        resp = await client.get("/v1/arrays/named-arr")
        assert resp.status_code == 200
        assert resp.json()["name"] == "named-arr"

    async def test_get_nonexistent_returns_404(self, client):
        resp = await client.get("/v1/arrays/does-not-exist")
        assert resp.status_code == 404

    async def test_delete_array(self, client):
        resp = await client.post("/v1/arrays", json={"name": "to-delete"})
        arr_id = resp.json()["id"]
        resp2 = await client.delete(f"/v1/arrays/{arr_id}")
        assert resp2.status_code == 204

    async def test_cannot_delete_default(self, client):
        resp = await client.get("/v1/arrays/default")
        arr_id = resp.json()["id"]
        resp2 = await client.delete(f"/v1/arrays/{arr_id}")
        assert resp2.status_code == 409

    async def test_delete_with_pools_returns_409(self, client):
        # Create array, then create a pool and attach it to the array
        arr_resp = await client.post("/v1/arrays", json={"name": "has-pool"})
        arr_id = arr_resp.json()["id"]
        pool_resp = await client.post("/v1/pools", json={
            "name": "p1",
            "backend_type": "malloc",
            "size_mb": 1024,
        })
        pool_id = pool_resp.json()["id"]
        # Attach pool to the non-default array
        attach = await client.post(f"/v1/arrays/{arr_id}/pools/{pool_id}")
        assert attach.status_code == 200
        resp = await client.delete(f"/v1/arrays/{arr_id}")
        assert resp.status_code == 409

    async def test_capabilities_endpoint(self, client):
        await client.post("/v1/arrays", json={
            "name": "cap-arr",
            "vendor": "ibm_svc",
        })
        resp = await client.get("/v1/arrays/cap-arr/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor"] == "ibm_svc"
        assert "effective_profile" in data
        assert data["effective_profile"]["model"] == "SVC-SAFER-FAKE-9000"


# ---------------------------------------------------------------------------
# Pool scoping (same name, different arrays)
# ---------------------------------------------------------------------------

class TestPoolScoping:
    async def _create_array(self, client, name: str) -> str:
        resp = await client.post("/v1/arrays", json={"name": name})
        assert resp.status_code == 201
        return resp.json()["id"]

    async def _create_pool(
        self, client, name: str, array: str | None = None,
    ) -> dict:
        """Create a pool (under default array) and optionally attach it to *array*."""
        resp = await client.post("/v1/pools", json={
            "name": name,
            "backend_type": "malloc",
            "size_mb": 512,
        })
        assert resp.status_code == 201, resp.text
        pool = resp.json()
        if array is not None:
            attach = await client.post(
                f"/v1/arrays/{array}/pools/{pool['id']}"
            )
            assert attach.status_code == 200, attach.text
            pool = attach.json()
        return pool

    async def test_same_pool_name_in_two_arrays(self, client):
        """Pool names only need to be unique within an array."""
        await self._create_array(client, "arr-x")
        await self._create_array(client, "arr-y")

        # Create "gold" in default, then move to arr-x
        p1 = await self._create_pool(client, "gold", "arr-x")
        # "gold" no longer in default — create another, move to arr-y
        p2 = await self._create_pool(client, "gold", "arr-y")

        assert p1["id"] != p2["id"]
        assert p1["name"] == p2["name"] == "gold"

    async def test_duplicate_pool_name_within_array_returns_409(self, client):
        """Creating two pools with the same name in the same (default) array fails."""
        await self._create_pool(client, "gold")
        resp = await client.post("/v1/pools", json={
            "name": "gold",
            "backend_type": "malloc",
            "size_mb": 512,
        })
        assert resp.status_code == 409

    async def test_list_pools_filtered_by_array(self, client):
        await self._create_array(client, "filter-a")
        await self._create_array(client, "filter-b")
        await self._create_pool(client, "gold", "filter-a")
        await self._create_pool(client, "silver", "filter-b")

        resp_a = await client.get("/v1/pools?array=filter-a")
        assert resp_a.status_code == 200
        names_a = [p["name"] for p in resp_a.json()]
        assert "gold" in names_a
        assert "silver" not in names_a

    async def test_pool_created_in_default_when_array_omitted(self, client):
        resp = await client.post("/v1/pools", json={
            "name": "auto-pool",
            "backend_type": "malloc",
            "size_mb": 256,
        })
        assert resp.status_code == 201
        data = resp.json()
        # array_id should be set (the default array's id)
        assert data["array_id"] is not None


# ---------------------------------------------------------------------------
# Volume scoping
# ---------------------------------------------------------------------------

class TestVolumeScoping:
    async def _setup(self, client):
        """Create two arrays each with a pool, return pool IDs."""
        r_a = await client.post("/v1/arrays", json={"name": "vs-a"})
        r_b = await client.post("/v1/arrays", json={"name": "vs-b"})
        arr_a_id = r_a.json()["id"]
        arr_b_id = r_b.json()["id"]

        pool_a = (await client.post("/v1/pools", json={
            "name": "pool-a", "backend_type": "malloc", "size_mb": 512,
        })).json()
        await client.post(f"/v1/arrays/{arr_a_id}/pools/{pool_a['id']}")

        pool_b = (await client.post("/v1/pools", json={
            "name": "pool-b", "backend_type": "malloc", "size_mb": 512,
        })).json()
        await client.post(f"/v1/arrays/{arr_b_id}/pools/{pool_b['id']}")

        return pool_a["id"], pool_b["id"]

    async def test_volumes_inherit_array_from_pool(self, client):
        pool_a_id, _ = await self._setup(client)
        resp = await client.post("/v1/volumes", json={
            "name": "vol1", "pool_id": pool_a_id, "size_gb": 1,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["array_id"] is not None

    async def test_list_volumes_filtered_by_array(self, client):
        pool_a_id, pool_b_id = await self._setup(client)
        await client.post("/v1/volumes", json={
            "name": "vol-a", "pool_id": pool_a_id, "size_gb": 1,
        })
        await client.post("/v1/volumes", json={
            "name": "vol-b", "pool_id": pool_b_id, "size_gb": 1,
        })

        resp = await client.get("/v1/volumes?array=vs-a")
        names = [v["name"] for v in resp.json()]
        assert "vol-a" in names
        assert "vol-b" not in names
