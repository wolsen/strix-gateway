# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Integration-style tests for the IBM SVC shell dispatcher.

These tests:
  - Set up an in-memory SQLite database.
  - Use a mock SPDKClient (returns None for all calls, which is safe for all
    ensure_* idempotent functions in apollo_gateway.spdk.ensure).
  - Call ``dispatch(cmd, ctx)`` directly, bypassing SSH / OS environment.
  - Assert that DB state is mutated correctly and that stdout output matches
    the IBM SVC wire format expected by Cinder drivers.

Flow validated
--------------
  1. svctask mkvdisk   → creates Volume in DB with status=available
  2. svctask mkhost    → creates Host in DB
  3. svctask addhostport → updates Host.initiators_iscsi_iqns
  4. svctask mkvdiskhostmap → creates Mapping, assigns lun_id, volume=in_use
  5. svcinfo lsvdisk   → reports volume status=online, capacity correct
  6. svcinfo lshost    → reports host with iscsi port
  7. svcinfo lshostvdiskmap → lists mapping with correct lun
  8. svcinfo lsvdiskhostmap → same mapping from vdisk perspective
  9. svctask rmvdiskhostmap → removes mapping, volume=available
  10. svctask rmvdisk   → deletes volume from DB
  11. svctask rmhost    → deletes host from DB
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apollo_gateway.personalities.svc.handlers import SvcContext, dispatch
from apollo_gateway.core.db import (
    Base,
    Host,
    Mapping,
    Pool,
    Array,
    Volume,
    init_db,
    get_session_factory,
)
from apollo_gateway.core.models import VolumeStatus
from apollo_gateway.core.personas import merge_profile

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def session_factory():
    """Fresh in-memory DB for each test function."""
    await init_db(TEST_DATABASE_URL)
    return get_session_factory()


@pytest.fixture
def mock_spdk():
    """SPDKClient mock: all RPC calls return None (safe for ensure_* functions)."""
    client = MagicMock()
    client.call = MagicMock(return_value=None)
    return client


@pytest_asyncio.fixture
async def array(session_factory):
    """Pre-existing 'default' Array."""
    async with session_factory() as s:
        arr = Array(
            name="default",
            vendor="generic",
            profile="{}",
        )
        s.add(arr)
        await s.commit()
        await s.refresh(arr)
        return arr


@pytest_asyncio.fixture
async def pool(session_factory, array):
    """Pre-existing Pool named 'pool0' (required by mkvdisk tests)."""
    async with session_factory() as s:
        p = Pool(
            name="pool0",
            backend_type="malloc",
            size_mb=10240,
            array_id=array.id,
        )
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return p


@pytest_asyncio.fixture
async def ctx(session_factory, mock_spdk, pool, array):
    """SvcContext wired to the test DB and mock SPDK client."""
    profile_data = json.loads(array.profile)
    profile = merge_profile(array.vendor, profile_data)
    async with session_factory() as session:
        yield SvcContext(
            session=session,
            spdk=mock_spdk,
            array_id=array.id,
            array_name=array.name,
            effective_profile=profile.model_dump(),
        )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def run(cmd: str, ctx: SvcContext) -> tuple[int, str]:
    """Run *cmd* through the dispatcher and return (exit_code, captured_stdout)."""
    buf = io.StringIO()
    err = io.StringIO()
    code = await dispatch(cmd, ctx, stdout=buf, stderr=err)
    return code, buf.getvalue()


# ---------------------------------------------------------------------------
# Error / unknown command tests (no DB required)
# ---------------------------------------------------------------------------

class TestDispatchErrors:
    async def test_unknown_verb_returns_1(self, ctx):
        code, _ = await run("garbage lsvdisk", ctx)
        assert code == 1

    async def test_unknown_subcommand_returns_1(self, ctx):
        code, _ = await run("svcinfo nonexistent", ctx)
        assert code == 1

    async def test_empty_command_not_reached(self, ctx):
        # dispatch() does NOT handle empty commands — _main() does.
        # An empty string yields SvcUnknownCommandError from parse_ssh_command.
        code, _ = await run("svcinfo", ctx)
        assert code == 1


# ---------------------------------------------------------------------------
# lssystem
# ---------------------------------------------------------------------------

class TestLsSystem:
    async def test_lssystem_returns_name_field(self, ctx):
        code, out = await run("svcinfo lssystem", ctx)
        assert code == 0
        # ctx.array_name == "default"
        assert "name!default" in out

    async def test_lssystem_custom_delim(self, ctx):
        code, out = await run("svcinfo lssystem -delim :", ctx)
        assert code == 0
        assert "name:default" in out


# ---------------------------------------------------------------------------
# mkvdisk / lsvdisk / rmvdisk lifecycle
# ---------------------------------------------------------------------------

class TestVdiskLifecycle:
    async def test_mkvdisk_creates_volume(self, ctx, session_factory):
        code, out = await run(
            "svctask mkvdisk -name vol1 -size 1 -unit gb -mdiskgrp pool0", ctx
        )
        assert code == 0
        assert "successfully created" in out

        # Verify DB state
        async with session_factory() as s:
            result = await s.execute(select(Volume).where(Volume.name == "vol1"))
            vol = result.scalar_one_or_none()
        assert vol is not None
        assert vol.size_mb == 1024
        assert vol.status == VolumeStatus.available

    async def test_mkvdisk_unknown_pool_returns_1(self, ctx):
        code, _ = await run(
            "svctask mkvdisk -name vol1 -size 1 -unit gb -mdiskgrp nosuchpool", ctx
        )
        assert code == 1

    async def test_mkvdisk_duplicate_name_returns_1(self, ctx):
        await run("svctask mkvdisk -name vol1 -size 1 -unit gb -mdiskgrp pool0", ctx)
        code, out = await run(
            "svctask mkvdisk -name vol1 -size 1 -unit gb -mdiskgrp pool0", ctx
        )
        assert code == 1

    async def test_lsvdisk_list(self, ctx):
        await run("svctask mkvdisk -name vol1 -size 2 -unit gb -mdiskgrp pool0", ctx)
        code, out = await run("svcinfo lsvdisk", ctx)
        assert code == 0
        assert "vol1" in out

    async def test_lsvdisk_single_delim(self, ctx):
        await run("svctask mkvdisk -name myvol -size 5 -unit gb -mdiskgrp pool0", ctx)
        code, out = await run("svcinfo lsvdisk myvol -delim !", ctx)
        assert code == 0
        assert "name!myvol" in out
        assert "capacity!5.00GB" in out
        assert "status!online" in out
        assert "mdisk_grp_name!pool0" in out

    async def test_lsvdisk_not_found(self, ctx):
        code, _ = await run("svcinfo lsvdisk nosuchvol", ctx)
        assert code == 1

    async def test_rmvdisk_deletes_volume(self, ctx, session_factory):
        await run("svctask mkvdisk -name tmpvol -size 1 -unit gb -mdiskgrp pool0", ctx)
        code, _ = await run("svctask rmvdisk tmpvol", ctx)
        assert code == 0

        async with session_factory() as s:
            result = await s.execute(select(Volume).where(Volume.name == "tmpvol"))
            assert result.scalar_one_or_none() is None

    async def test_rmvdisk_not_found(self, ctx):
        code, _ = await run("svctask rmvdisk nosuchvol", ctx)
        assert code == 1


# ---------------------------------------------------------------------------
# mkhost / addhostport / rmhost lifecycle
# ---------------------------------------------------------------------------

class TestHostLifecycle:
    async def test_mkhost_creates_host(self, ctx, session_factory):
        code, out = await run("svctask mkhost -name host1", ctx)
        assert code == 0
        assert "successfully created" in out

        async with session_factory() as s:
            result = await s.execute(select(Host).where(Host.name == "host1"))
            h = result.scalar_one_or_none()
        assert h is not None

    async def test_mkhost_duplicate_returns_1(self, ctx):
        await run("svctask mkhost -name host1", ctx)
        code, out = await run("svctask mkhost -name host1", ctx)
        assert code == 1

    async def test_addhostport_sets_iqn(self, ctx, session_factory):
        await run("svctask mkhost -name host1", ctx)
        code, _ = await run(
            "svctask addhostport -host host1 -iscsiname iqn.2001-04.example:h1", ctx
        )
        assert code == 0

        async with session_factory() as s:
            result = await s.execute(select(Host).where(Host.name == "host1"))
            h = result.scalar_one_or_none()
        assert h is not None
        assert "iqn.2001-04.example:h1" in (h.initiators_iscsi_iqns or "")

    async def test_addhostport_multiple_iqns_appended(self, ctx, session_factory):
        await run("svctask mkhost -name host1", ctx)
        await run("svctask addhostport -host host1 -iscsiname iqn.example:p1", ctx)
        code, _ = await run("svctask addhostport -host host1 -iscsiname iqn.example:p2", ctx)
        assert code == 0

        async with session_factory() as s:
            result = await s.execute(select(Host).where(Host.name == "host1"))
            h = result.scalar_one_or_none()
        assert "iqn.example:p1" in (h.initiators_iscsi_iqns or "")
        assert "iqn.example:p2" in (h.initiators_iscsi_iqns or "")

    async def test_addhostport_idempotent(self, ctx, session_factory):
        await run("svctask mkhost -name host1", ctx)
        await run("svctask addhostport -host host1 -iscsiname iqn.example:p1", ctx)
        await run("svctask addhostport -host host1 -iscsiname iqn.example:p1", ctx)

        async with session_factory() as s:
            result = await s.execute(select(Host).where(Host.name == "host1"))
            h = result.scalar_one_or_none()
        # Should not duplicate
        assert h.initiators_iscsi_iqns.count("iqn.example:p1") == 1

    async def test_lshost_list(self, ctx):
        await run("svctask mkhost -name host1", ctx)
        code, out = await run("svcinfo lshost", ctx)
        assert code == 0
        assert "host1" in out

    async def test_lshost_single(self, ctx):
        await run("svctask mkhost -name host1", ctx)
        await run("svctask addhostport -host host1 -iscsiname iqn.ex:h1", ctx)
        code, out = await run("svcinfo lshost host1", ctx)
        assert code == 0
        assert "name!host1" in out
        assert "iqn.ex:h1" in out

    async def test_lshost_not_found(self, ctx):
        code, _ = await run("svcinfo lshost nosuchhost", ctx)
        assert code == 1

    async def test_rmhost_deletes_host(self, ctx, session_factory):
        await run("svctask mkhost -name host1", ctx)
        code, _ = await run("svctask rmhost host1", ctx)
        assert code == 0

        async with session_factory() as s:
            result = await s.execute(select(Host).where(Host.name == "host1"))
            assert result.scalar_one_or_none() is None

    async def test_rmhost_not_found(self, ctx):
        code, _ = await run("svctask rmhost nosuchhost", ctx)
        assert code == 1


# ---------------------------------------------------------------------------
# Full mapping lifecycle
# ---------------------------------------------------------------------------

class TestMappingLifecycle:
    async def _setup_vol_and_host(self, ctx):
        await run("svctask mkvdisk -name vol1 -size 1 -unit gb -mdiskgrp pool0", ctx)
        await run("svctask mkhost -name host1", ctx)
        await run(
            "svctask addhostport -host host1 -iscsiname iqn.2001-04.example:h1", ctx
        )

    async def test_mkvdiskhostmap_creates_mapping(self, ctx, session_factory):
        await self._setup_vol_and_host(ctx)
        code, out = await run("svctask mkvdiskhostmap -host host1 vol1", ctx)
        assert code == 0
        assert "successfully created" in out

        async with session_factory() as s:
            result = await s.execute(select(Mapping))
            m = result.scalar_one_or_none()
        assert m is not None
        assert m.lun_id == 0    # first allocation is LUN 0
        assert m.desired_state == "attached"

    async def test_mkvdiskhostmap_volume_becomes_in_use(self, ctx, session_factory):
        await self._setup_vol_and_host(ctx)
        await run("svctask mkvdiskhostmap -host host1 vol1", ctx)

        async with session_factory() as s:
            result = await s.execute(select(Volume).where(Volume.name == "vol1"))
            vol = result.scalar_one_or_none()
        assert vol.status == VolumeStatus.in_use

    async def test_mkvdiskhostmap_duplicate_returns_1(self, ctx):
        await self._setup_vol_and_host(ctx)
        await run("svctask mkvdiskhostmap -host host1 vol1", ctx)
        code, out = await run("svctask mkvdiskhostmap -host host1 vol1", ctx)
        assert code == 1

    async def test_lshostvdiskmap(self, ctx):
        await self._setup_vol_and_host(ctx)
        await run("svctask mkvdiskhostmap -host host1 vol1", ctx)
        code, out = await run("svcinfo lshostvdiskmap host1", ctx)
        assert code == 0
        assert "vol1" in out
        assert "host1" in out

    async def test_lsvdiskhostmap(self, ctx):
        await self._setup_vol_and_host(ctx)
        await run("svctask mkvdiskhostmap -host host1 vol1", ctx)
        code, out = await run("svcinfo lsvdiskhostmap vol1", ctx)
        assert code == 0
        assert "host1" in out
        assert "vol1" in out

    async def test_rmvdiskhostmap_removes_mapping(self, ctx, session_factory):
        await self._setup_vol_and_host(ctx)
        await run("svctask mkvdiskhostmap -host host1 vol1", ctx)
        code, _ = await run("svctask rmvdiskhostmap -host host1 vol1", ctx)
        assert code == 0

        async with session_factory() as s:
            result = await s.execute(select(Mapping))
            assert result.scalar_one_or_none() is None

    async def test_rmvdiskhostmap_volume_returns_available(self, ctx, session_factory):
        await self._setup_vol_and_host(ctx)
        await run("svctask mkvdiskhostmap -host host1 vol1", ctx)
        await run("svctask rmvdiskhostmap -host host1 vol1", ctx)

        async with session_factory() as s:
            result = await s.execute(select(Volume).where(Volume.name == "vol1"))
            vol = result.scalar_one_or_none()
        assert vol.status == VolumeStatus.available

    async def test_rmvdisk_blocked_while_mapped(self, ctx):
        await self._setup_vol_and_host(ctx)
        await run("svctask mkvdiskhostmap -host host1 vol1", ctx)
        code, _ = await run("svctask rmvdisk vol1", ctx)
        assert code == 1

    async def test_rmhost_blocked_while_mapped(self, ctx):
        await self._setup_vol_and_host(ctx)
        await run("svctask mkvdiskhostmap -host host1 vol1", ctx)
        code, _ = await run("svctask rmhost host1", ctx)
        assert code == 1

    async def test_full_round_trip(self, ctx, session_factory):
        """Complete create → map → query → unmap → delete cycle."""
        # Create
        assert (await run("svctask mkvdisk -name vol1 -size 2 -unit gb -mdiskgrp pool0", ctx))[0] == 0
        assert (await run("svctask mkhost -name host1", ctx))[0] == 0
        assert (await run("svctask addhostport -host host1 -iscsiname iqn.2001-04.example:h1", ctx))[0] == 0

        # Map
        assert (await run("svctask mkvdiskhostmap -host host1 vol1", ctx))[0] == 0

        # Query — lsvdisk should show online, 2GB
        _, out = await run("svcinfo lsvdisk vol1 -delim !", ctx)
        assert "status!online" in out
        assert "capacity!2.00GB" in out

        # Query — lshostvdiskmap
        code, out = await run("svcinfo lshostvdiskmap host1", ctx)
        assert code == 0
        assert "vol1" in out

        # Query — lsvdiskhostmap
        code, out = await run("svcinfo lsvdiskhostmap vol1", ctx)
        assert code == 0
        assert "host1" in out

        # Unmap
        assert (await run("svctask rmvdiskhostmap -host host1 vol1", ctx))[0] == 0

        # Delete
        assert (await run("svctask rmvdisk vol1", ctx))[0] == 0
        assert (await run("svctask rmhost host1", ctx))[0] == 0

        # Confirm cleaned up
        async with session_factory() as s:
            assert (await s.execute(select(Volume))).scalar_one_or_none() is None
            assert (await s.execute(select(Host))).scalar_one_or_none() is None
            assert (await s.execute(select(Mapping))).scalar_one_or_none() is None
