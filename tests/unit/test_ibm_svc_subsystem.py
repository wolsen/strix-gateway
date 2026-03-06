# FILE: tests/unit/test_ibm_svc_subsystem.py
"""Tests for IBM SVC facade array isolation.

Validates that:
  - Two SvcContexts for different arrays see only their own pools / volumes
  - POST /v1/svc/run returns a valid response for a known array
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from apollo_gateway.compat.ibm_svc.handlers import SvcContext
from apollo_gateway.compat.ibm_svc.handlers import dispatch
from apollo_gateway.core.db import Pool, Array, Volume, init_db, get_session_factory
from apollo_gateway.core.models import VolumeStatus
from apollo_gateway.core.personas import merge_profile

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def session_factory():
    await init_db(TEST_DATABASE_URL)
    return get_session_factory()


@pytest.fixture
def mock_spdk():
    client = MagicMock()
    client.call = MagicMock(return_value=None)
    return client


async def _make_array(session_factory, name: str, vendor: str = "ibm_svc") -> Array:
    async with session_factory() as s:
        arr = Array(
            name=name,
            vendor=vendor,
            profile="{}",
        )
        s.add(arr)
        await s.commit()
        await s.refresh(arr)
        return arr


async def _make_pool(session_factory, name: str, array_id: str) -> Pool:
    async with session_factory() as s:
        p = Pool(name=name, backend_type="malloc", size_mb=1024, array_id=array_id)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return p


def _make_ctx(session, spdk, arr: Array) -> SvcContext:
    profile_data = json.loads(arr.profile)
    profile = merge_profile(arr.vendor, profile_data)
    return SvcContext(
        session=session,
        spdk=spdk,
        array_id=arr.id,
        array_name=arr.name,
        effective_profile=profile.model_dump(),
    )


async def run(cmd: str, ctx: SvcContext) -> tuple[int, str]:
    buf = io.StringIO()
    err = io.StringIO()
    code = await dispatch(cmd, ctx, stdout=buf, stderr=err)
    return code, buf.getvalue()


# ---------------------------------------------------------------------------
# Array isolation tests
# ---------------------------------------------------------------------------

class TestArrayIsolation:
    async def test_lsmdiskgrp_shows_only_own_pools(self, session_factory, mock_spdk):
        arr_a = await _make_array(session_factory, "svc-a")
        arr_b = await _make_array(session_factory, "svc-b")
        await _make_pool(session_factory, "pool-a", arr_a.id)
        await _make_pool(session_factory, "pool-b", arr_b.id)

        async with session_factory() as session:
            ctx_a = _make_ctx(session, mock_spdk, arr_a)
            code, out = await run("svcinfo lsmdiskgrp", ctx_a)
        assert code == 0
        assert "pool-a" in out
        assert "pool-b" not in out

    async def test_lsmdiskgrp_svc_b_shows_only_its_pools(self, session_factory, mock_spdk):
        arr_a = await _make_array(session_factory, "svc-a")
        arr_b = await _make_array(session_factory, "svc-b")
        await _make_pool(session_factory, "pool-a", arr_a.id)
        await _make_pool(session_factory, "pool-b", arr_b.id)

        async with session_factory() as session:
            ctx_b = _make_ctx(session, mock_spdk, arr_b)
            code, out = await run("svcinfo lsmdiskgrp", ctx_b)
        assert code == 0
        assert "pool-b" in out
        assert "pool-a" not in out

    async def test_lsvdisk_shows_only_own_volumes(self, session_factory, mock_spdk):
        arr_a = await _make_array(session_factory, "svc-a")
        arr_b = await _make_array(session_factory, "svc-b")
        pool_a = await _make_pool(session_factory, "pool-a", arr_a.id)
        pool_b = await _make_pool(session_factory, "pool-b", arr_b.id)

        async with session_factory() as s:
            vol_a = Volume(name="vol-a", array_id=arr_a.id, pool_id=pool_a.id,
                          size_mb=64, status="available")
            vol_b = Volume(name="vol-b", array_id=arr_b.id, pool_id=pool_b.id,
                          size_mb=64, status="available")
            s.add_all([vol_a, vol_b])
            await s.commit()

        async with session_factory() as session:
            ctx_a = _make_ctx(session, mock_spdk, arr_a)
            _, out = await run("svcinfo lsvdisk", ctx_a)
        assert "vol-a" in out
        assert "vol-b" not in out

    async def test_mkvdisk_scoped_to_array(self, session_factory, mock_spdk):
        arr_a = await _make_array(session_factory, "svc-a")
        arr_b = await _make_array(session_factory, "svc-b")
        # Both have a pool named "gold"
        await _make_pool(session_factory, "gold", arr_a.id)
        await _make_pool(session_factory, "gold", arr_b.id)

        async with session_factory() as session:
            ctx_a = _make_ctx(session, mock_spdk, arr_a)
            code, out = await run(
                "svctask mkvdisk -name testvol -size 1 -unit gb -mdiskgrp gold", ctx_a
            )
        assert code == 0

        # Verify volume belongs to arr_a
        async with session_factory() as s:
            vol = (await s.execute(
                select(Volume).where(Volume.name == "testvol")
            )).scalar_one_or_none()
        assert vol is not None
        assert vol.array_id == arr_a.id

    async def test_mkvdisk_in_array_a_not_visible_from_b(self, session_factory, mock_spdk):
        arr_a = await _make_array(session_factory, "svc-a")
        arr_b = await _make_array(session_factory, "svc-b")
        await _make_pool(session_factory, "pool-a", arr_a.id)
        # svc-b has no pool

        async with session_factory() as session:
            ctx_a = _make_ctx(session, mock_spdk, arr_a)
            await run("svctask mkvdisk -name vol-a -size 1 -unit gb -mdiskgrp pool-a", ctx_a)

        async with session_factory() as session:
            ctx_b = _make_ctx(session, mock_spdk, arr_b)
            _, out = await run("svcinfo lsvdisk", ctx_b)
        assert "vol-a" not in out

    async def test_lssystem_shows_array_name(self, session_factory, mock_spdk):
        arr = await _make_array(session_factory, "my-svc")
        async with session_factory() as session:
            ctx = _make_ctx(session, mock_spdk, arr)
            code, out = await run("svcinfo lssystem", ctx)
        assert code == 0
        assert "name!my-svc" in out


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------

class TestSvcRunEndpoint:
    async def test_svcinfo_lssystem_returns_200(self, client):
        """POST /v1/svc/run with a valid command against the default array."""
        resp = await client.post(
            "/v1/svc/run",
            json={"array": "default", "command": "svcinfo lssystem"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "stdout" in data
        assert "stderr" in data
        assert "exit_code" in data
        assert data["exit_code"] == 0

    async def test_unknown_array_returns_404(self, client):
        """POST /v1/svc/run with a nonexistent array returns 404."""
        resp = await client.post(
            "/v1/svc/run",
            json={"array": "does-not-exist", "command": "svcinfo lssystem"},
        )
        assert resp.status_code == 404
