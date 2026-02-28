# FILE: tests/unit/test_ibm_svc_subsystem.py
"""Tests for IBM SVC facade subsystem isolation.

Validates that:
  - Two SvcContexts for different subsystems see only their own pools / volumes
  - run_svc_command() returns (stdout, stderr, exit_code) correctly
  - run_svc_command() returns exit_code=1 for an unknown subsystem
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from apollo_gateway.compat.ibm_svc.handlers import SvcContext
from apollo_gateway.compat.ibm_svc.shell import dispatch
from apollo_gateway.core.db import Pool, Subsystem, Volume, init_db, get_session_factory
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


async def _make_subsystem(session_factory, name: str, persona: str = "ibm_svc") -> Subsystem:
    async with session_factory() as s:
        sub = Subsystem(
            name=name,
            persona=persona,
            protocols_enabled='["iscsi","nvmeof_tcp"]',
            capability_profile="{}",
        )
        s.add(sub)
        await s.commit()
        await s.refresh(sub)
        return sub


async def _make_pool(session_factory, name: str, subsystem_id: str) -> Pool:
    async with session_factory() as s:
        p = Pool(name=name, backend_type="malloc", size_mb=1024, subsystem_id=subsystem_id)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return p


def _make_ctx(session, spdk, sub: Subsystem) -> SvcContext:
    profile = merge_profile(sub.persona, json.loads(sub.capability_profile))
    return SvcContext(
        session=session,
        spdk=spdk,
        subsystem_id=sub.id,
        subsystem_name=sub.name,
        effective_profile=profile.model_dump(),
        protocols_enabled=json.loads(sub.protocols_enabled),
    )


async def run(cmd: str, ctx: SvcContext) -> tuple[int, str]:
    buf = io.StringIO()
    err = io.StringIO()
    code = await dispatch(cmd, ctx, stdout=buf, stderr=err)
    return code, buf.getvalue()


# ---------------------------------------------------------------------------
# Subsystem isolation tests
# ---------------------------------------------------------------------------

class TestSubsystemIsolation:
    async def test_lsmdiskgrp_shows_only_own_pools(self, session_factory, mock_spdk):
        sub_a = await _make_subsystem(session_factory, "svc-a")
        sub_b = await _make_subsystem(session_factory, "svc-b")
        await _make_pool(session_factory, "pool-a", sub_a.id)
        await _make_pool(session_factory, "pool-b", sub_b.id)

        async with session_factory() as session:
            ctx_a = _make_ctx(session, mock_spdk, sub_a)
            code, out = await run("svcinfo lsmdiskgrp", ctx_a)
        assert code == 0
        assert "pool-a" in out
        assert "pool-b" not in out

    async def test_lsmdiskgrp_svc_b_shows_only_its_pools(self, session_factory, mock_spdk):
        sub_a = await _make_subsystem(session_factory, "svc-a")
        sub_b = await _make_subsystem(session_factory, "svc-b")
        await _make_pool(session_factory, "pool-a", sub_a.id)
        await _make_pool(session_factory, "pool-b", sub_b.id)

        async with session_factory() as session:
            ctx_b = _make_ctx(session, mock_spdk, sub_b)
            code, out = await run("svcinfo lsmdiskgrp", ctx_b)
        assert code == 0
        assert "pool-b" in out
        assert "pool-a" not in out

    async def test_lsvdisk_shows_only_own_volumes(self, session_factory, mock_spdk):
        sub_a = await _make_subsystem(session_factory, "svc-a")
        sub_b = await _make_subsystem(session_factory, "svc-b")
        pool_a = await _make_pool(session_factory, "pool-a", sub_a.id)
        pool_b = await _make_pool(session_factory, "pool-b", sub_b.id)

        async with session_factory() as s:
            vol_a = Volume(name="vol-a", subsystem_id=sub_a.id, pool_id=pool_a.id,
                          size_mb=64, status="available")
            vol_b = Volume(name="vol-b", subsystem_id=sub_b.id, pool_id=pool_b.id,
                          size_mb=64, status="available")
            s.add_all([vol_a, vol_b])
            await s.commit()

        async with session_factory() as session:
            ctx_a = _make_ctx(session, mock_spdk, sub_a)
            _, out = await run("svcinfo lsvdisk", ctx_a)
        assert "vol-a" in out
        assert "vol-b" not in out

    async def test_mkvdisk_scoped_to_subsystem(self, session_factory, mock_spdk):
        sub_a = await _make_subsystem(session_factory, "svc-a")
        sub_b = await _make_subsystem(session_factory, "svc-b")
        # Both have a pool named "gold"
        await _make_pool(session_factory, "gold", sub_a.id)
        await _make_pool(session_factory, "gold", sub_b.id)

        async with session_factory() as session:
            ctx_a = _make_ctx(session, mock_spdk, sub_a)
            code, out = await run(
                "svctask mkvdisk -name testvol -size 1 -unit gb -mdiskgrp gold", ctx_a
            )
        assert code == 0

        # Verify volume belongs to sub_a
        async with session_factory() as s:
            vol = (await s.execute(
                select(Volume).where(Volume.name == "testvol")
            )).scalar_one_or_none()
        assert vol is not None
        assert vol.subsystem_id == sub_a.id

    async def test_mkvdisk_in_subsystem_a_not_visible_from_b(self, session_factory, mock_spdk):
        sub_a = await _make_subsystem(session_factory, "svc-a")
        sub_b = await _make_subsystem(session_factory, "svc-b")
        await _make_pool(session_factory, "pool-a", sub_a.id)
        # svc-b has no pool

        async with session_factory() as session:
            ctx_a = _make_ctx(session, mock_spdk, sub_a)
            await run("svctask mkvdisk -name vol-a -size 1 -unit gb -mdiskgrp pool-a", ctx_a)

        async with session_factory() as session:
            ctx_b = _make_ctx(session, mock_spdk, sub_b)
            _, out = await run("svcinfo lsvdisk", ctx_b)
        assert "vol-a" not in out

    async def test_lssystem_shows_subsystem_name(self, session_factory, mock_spdk):
        sub = await _make_subsystem(session_factory, "my-svc")
        async with session_factory() as session:
            ctx = _make_ctx(session, mock_spdk, sub)
            code, out = await run("svcinfo lssystem", ctx)
        assert code == 0
        assert "name!my-svc" in out


# ---------------------------------------------------------------------------
# run_svc_command (programmatic helper)
# ---------------------------------------------------------------------------

class TestRunSvcCommand:
    async def test_unknown_subsystem_returns_exit_1(self, session_factory):
        # run_svc_command is synchronous, but since we're in an async test we
        # test the async internals directly.
        # Patch init_db to be a no-op so we don't reinitialize the test engine.
        from unittest.mock import AsyncMock, MagicMock, patch
        from apollo_gateway.compat.ibm_svc.shell import _run_svc_command_async

        mock_settings = MagicMock()
        mock_settings.database_url = TEST_DATABASE_URL

        async def _noop_init_db(url):
            pass

        with patch("apollo_gateway.core.db.init_db", side_effect=_noop_init_db), \
             patch("apollo_gateway.spdk.rpc.SPDKClient", return_value=MagicMock()):
            stdout, stderr, code = await _run_svc_command_async(
                "svcinfo lssystem", "does-not-exist", mock_settings
            )
        assert code == 1
        assert "not found" in stderr

    async def test_known_subsystem_returns_exit_0(self, session_factory):
        from unittest.mock import AsyncMock, MagicMock, patch
        from apollo_gateway.compat.ibm_svc.shell import _run_svc_command_async

        await _make_subsystem(session_factory, "test-sub")

        mock_settings = MagicMock()
        mock_settings.database_url = TEST_DATABASE_URL

        async def _noop_init_db(url):
            pass

        with patch("apollo_gateway.core.db.init_db", side_effect=_noop_init_db), \
             patch("apollo_gateway.spdk.rpc.SPDKClient", return_value=MagicMock()):
            stdout, stderr, code = await _run_svc_command_async(
                "svcinfo lssystem", "test-sub", mock_settings
            )
        assert code == 0
        assert "name!test-sub" in stdout
