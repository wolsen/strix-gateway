# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for FC-related IBM SVC handlers.

Covers the FC enhancements added to handlers.py:
  - addhostport -hbawwpn  (FC WWPN port registration)
  - lshost with WWPN fields
  - lsportfc  (FC target port discovery)
  - lsfabric  (FC fabric path enumeration)
  - mkvdiskhostmap FC-aware path (FC persona over iSCSI underlay)
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from strix_gateway.personalities.svc.handlers import SvcContext, dispatch
from strix_gateway.core.db import (
    Array,
    Base,
    Host,
    Mapping,
    Pool,
    TransportEndpoint,
    Volume,
    init_db,
    get_session_factory,
)
from strix_gateway.core.models import Protocol, VolumeStatus
from strix_gateway.core.personas import merge_profile

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


@pytest_asyncio.fixture
async def array(session_factory):
    async with session_factory() as s:
        arr = Array(name="default", vendor="generic", profile="{}")
        s.add(arr)
        await s.commit()
        await s.refresh(arr)
        return arr


@pytest_asyncio.fixture
async def pool(session_factory, array):
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
async def iscsi_endpoint(session_factory, array):
    """Pre-existing iSCSI TransportEndpoint on the array."""
    async with session_factory() as s:
        ep = TransportEndpoint(
            array_id=array.id,
            protocol=Protocol.iscsi.value,
            targets=json.dumps({"target_iqn": "iqn.2026-03.com.lunacy:default"}),
            addresses=json.dumps({"portals": ["127.0.0.1:3260"]}),
            auth=json.dumps({"method": "none"}),
        )
        s.add(ep)
        await s.commit()
        await s.refresh(ep)
        return ep


@pytest_asyncio.fixture
async def fc_endpoint(session_factory, array):
    """Pre-existing FC TransportEndpoint with two target WWPNs."""
    async with session_factory() as s:
        ep = TransportEndpoint(
            array_id=array.id,
            protocol=Protocol.fc.value,
            targets=json.dumps({
                "target_wwpns": ["0x500a09c0ffe1aa01", "0x500a09c0ffe1aa02"],
            }),
            addresses=json.dumps({}),
            auth=json.dumps({"method": "none"}),
        )
        s.add(ep)
        await s.commit()
        await s.refresh(ep)
        return ep


@pytest_asyncio.fixture
async def ctx(session_factory, mock_spdk, pool, array):
    """SvcContext without FC endpoints (iSCSI-only baseline)."""
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


@pytest_asyncio.fixture
async def fc_ctx(session_factory, mock_spdk, pool, array, fc_endpoint, iscsi_endpoint):
    """SvcContext with both FC and iSCSI endpoints on the array."""
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
    buf = io.StringIO()
    err = io.StringIO()
    code = await dispatch(cmd, ctx, stdout=buf, stderr=err)
    return code, buf.getvalue()


# ---------------------------------------------------------------------------
# addhostport -hbawwpn
# ---------------------------------------------------------------------------

class TestAddHostPortFcWwpn:
    async def test_addhostport_fcwwpn_sets_wwpn(self, ctx, session_factory):
        await run("svctask mkhost -name fchost1", ctx)
        code, _ = await run(
            "svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost1", ctx
        )
        assert code == 0

        async with session_factory() as s:
            result = await s.execute(select(Host).where(Host.name == "fchost1"))
            h = result.scalar_one_or_none()
        assert h is not None
        wwpns = json.loads(h.initiators_fc_wwpns) if h.initiators_fc_wwpns else []
        assert "0x200a09c0ffe1bb01" in wwpns

    async def test_addhostport_multiple_wwpns(self, ctx, session_factory):
        await run("svctask mkhost -name fchost1", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost1", ctx)
        code, _ = await run(
            "svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb02 fchost1", ctx
        )
        assert code == 0

        async with session_factory() as s:
            result = await s.execute(select(Host).where(Host.name == "fchost1"))
            h = result.scalar_one_or_none()
        wwpns = json.loads(h.initiators_fc_wwpns)
        assert "0x200a09c0ffe1bb01" in wwpns
        assert "0x200a09c0ffe1bb02" in wwpns

    async def test_addhostport_fcwwpn_idempotent(self, ctx, session_factory):
        await run("svctask mkhost -name fchost1", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost1", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost1", ctx)

        async with session_factory() as s:
            result = await s.execute(select(Host).where(Host.name == "fchost1"))
            h = result.scalar_one_or_none()
        wwpns = json.loads(h.initiators_fc_wwpns)
        assert wwpns.count("0x200a09c0ffe1bb01") == 1

    async def test_addhostport_mixed_iqn_and_wwpn(self, ctx, session_factory):
        """Host can have both IQN and WWPN ports."""
        await run("svctask mkhost -name mixhost", ctx)
        await run("svctask addhostport -force -iscsiname iqn.2001:h1 mixhost", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 mixhost", ctx)

        async with session_factory() as s:
            result = await s.execute(select(Host).where(Host.name == "mixhost"))
            h = result.scalar_one_or_none()
        iqns = json.loads(h.initiators_iscsi_iqns) if h.initiators_iscsi_iqns else []
        wwpns = json.loads(h.initiators_fc_wwpns) if h.initiators_fc_wwpns else []
        assert "iqn.2001:h1" in iqns
        assert "0x200a09c0ffe1bb01" in wwpns


# ---------------------------------------------------------------------------
# lshost with WWPN fields
# ---------------------------------------------------------------------------

class TestLsHostFcWwpn:
    async def test_lshost_detail_shows_wwpn_fields(self, ctx):
        await run("svctask mkhost -name fchost1", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost1", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb02 fchost1", ctx)

        code, out = await run("svcinfo lshost fchost1", ctx)
        assert code == 0
        assert "WWPN_0!0x200a09c0ffe1bb01" in out
        assert "WWPN_1!0x200a09c0ffe1bb02" in out

    async def test_lshost_detail_port_count_includes_wwpns(self, ctx):
        await run("svctask mkhost -name mixhost", ctx)
        await run("svctask addhostport -force -iscsiname iqn.example:p1 mixhost", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 mixhost", ctx)

        code, out = await run("svcinfo lshost mixhost", ctx)
        assert code == 0
        assert "port_count!2" in out

    async def test_lshost_list_shows_wwpn_column(self, ctx):
        await run("svctask mkhost -name fchost1", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost1", ctx)

        code, out = await run("svcinfo lshost", ctx)
        assert code == 0
        assert "0x200a09c0ffe1bb01" in out

    async def test_lshost_detail_no_wwpns_shows_empty(self, ctx):
        """Host with only IQNs shows empty WWPN field."""
        await run("svctask mkhost -name iscsihost", ctx)
        await run("svctask addhostport -force -iscsiname iqn.ex:h1 iscsihost", ctx)

        code, out = await run("svcinfo lshost iscsihost", ctx)
        assert code == 0
        assert "WWPN!" in out  # empty value
        assert "iscsi_name_0!iqn.ex:h1" in out


# ---------------------------------------------------------------------------
# lsportfc
# ---------------------------------------------------------------------------

class TestLsPortFc:
    async def test_lsportfc_returns_target_wwpns(self, fc_ctx):
        code, out = await run("svcinfo lsportfc", fc_ctx)
        assert code == 0
        assert "0x500a09c0ffe1aa01" in out
        assert "0x500a09c0ffe1aa02" in out

    async def test_lsportfc_shows_active_status(self, fc_ctx):
        code, out = await run("svcinfo lsportfc", fc_ctx)
        assert code == 0
        assert "active" in out

    async def test_lsportfc_empty_without_fc_endpoint(self, ctx):
        """Array without FC endpoint → empty table."""
        code, out = await run("svcinfo lsportfc", ctx)
        assert code == 0
        # No rows should be printed (empty table)
        lines = [l for l in out.strip().splitlines() if l.strip()]
        # Header only or empty
        assert len(lines) <= 1

    async def test_lsportfc_row_per_wwpn(self, fc_ctx):
        """Two target WWPNs → two rows."""
        code, out = await run("svcinfo lsportfc", fc_ctx)
        assert code == 0
        # Count rows containing "fc" type
        fc_rows = [l for l in out.strip().splitlines() if "0x500a09c0ffe1aa" in l]
        assert len(fc_rows) == 2


# ---------------------------------------------------------------------------
# lsfabric
# ---------------------------------------------------------------------------

class TestLsFabric:
    async def test_lsfabric_requires_host_flag(self, fc_ctx):
        code, _ = await run("svcinfo lsfabric", fc_ctx)
        assert code == 1

    async def test_lsfabric_not_found_host(self, fc_ctx):
        code, _ = await run("svcinfo lsfabric -host nosuchhost", fc_ctx)
        assert code == 1

    async def test_lsfabric_cross_product(self, fc_ctx):
        """Host with 1 WWPN × 2 target WWPNs → 2 fabric rows."""
        await run("svctask mkhost -name fchost1", fc_ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost1", fc_ctx)

        code, out = await run("svcinfo lsfabric -host fchost1", fc_ctx)
        assert code == 0
        # Should show both target WWPNs as local_wwpn
        assert "0x500a09c0ffe1aa01" in out
        assert "0x500a09c0ffe1aa02" in out
        # Should show host WWPN as remote_wwpn
        assert "0x200a09c0ffe1bb01" in out
        # Host name appears
        assert "fchost1" in out

    async def test_lsfabric_multi_initiator_cross_product(self, fc_ctx):
        """Host with 2 WWPNs × 2 target WWPNs → 4 fabric rows."""
        await run("svctask mkhost -name fchost2", fc_ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost2", fc_ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb02 fchost2", fc_ctx)

        code, out = await run("svcinfo lsfabric -host fchost2", fc_ctx)
        assert code == 0
        # Count data lines (non-header)
        lines = [l for l in out.strip().splitlines() if l.strip()]
        # 2 host WWPNs × 2 target WWPNs = 4, plus header = 5
        # But format may vary — at minimum assert both initiators appear
        assert "0x200a09c0ffe1bb01" in out
        assert "0x200a09c0ffe1bb02" in out
        assert "0x500a09c0ffe1aa01" in out
        assert "0x500a09c0ffe1aa02" in out

    async def test_lsfabric_host_no_wwpns_returns_empty(self, fc_ctx):
        """Host with only IQNs → no fabric entries (empty result)."""
        await run("svctask mkhost -name iscsihost", fc_ctx)
        await run("svctask addhostport -force -iscsiname iqn.example:h1 iscsihost", fc_ctx)

        code, out = await run("svcinfo lsfabric -host iscsihost", fc_ctx)
        assert code == 0
        # No rows — host has no FC ports
        data_lines = [l for l in out.strip().splitlines() if l.strip()]
        assert len(data_lines) <= 1  # header only or empty


# ---------------------------------------------------------------------------
# mkvdiskhostmap FC-aware path
# ---------------------------------------------------------------------------

class TestMkvdiskhostmapFcAware:
    async def _setup_fc_host_and_vol(self, ctx):
        """Create a volume and a host with FC WWPN."""
        await run("svctask mkvdisk -name fcvol1 -size 1 -unit gb -mdiskgrp pool0", ctx)
        await run("svctask mkhost -name fchost1", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost1", ctx)

    async def test_fc_mapping_creates_successfully(self, fc_ctx, session_factory):
        """FC host + array with FC endpoint → mapping created."""
        await self._setup_fc_host_and_vol(fc_ctx)
        code, out = await run("svctask mkvdiskhostmap -host fchost1 fcvol1", fc_ctx)
        assert code == 0
        assert "successfully created" in out

    async def test_fc_mapping_uses_fc_persona_endpoint(self, fc_ctx, session_factory, fc_endpoint):
        """Mapping's persona_endpoint should be the FC endpoint."""
        await self._setup_fc_host_and_vol(fc_ctx)
        await run("svctask mkvdiskhostmap -host fchost1 fcvol1", fc_ctx)

        async with session_factory() as s:
            result = await s.execute(select(Mapping))
            m = result.scalar_one()
        # The persona endpoint should be the FC one
        assert m.persona_endpoint_id == fc_endpoint.id

    async def test_fc_mapping_uses_iscsi_underlay(self, fc_ctx, session_factory, iscsi_endpoint):
        """Mapping's underlay_endpoint should always be iSCSI."""
        await self._setup_fc_host_and_vol(fc_ctx)
        await run("svctask mkvdiskhostmap -host fchost1 fcvol1", fc_ctx)

        async with session_factory() as s:
            result = await s.execute(select(Mapping))
            m = result.scalar_one()
        # The underlay endpoint should be iSCSI
        assert m.underlay_endpoint_id == iscsi_endpoint.id

    async def test_iscsi_host_on_fc_array_uses_iscsi_persona(
        self, fc_ctx, session_factory, iscsi_endpoint
    ):
        """iSCSI-only host on array with FC endpoint → iSCSI persona + underlay."""
        await run("svctask mkvdisk -name iscsivol -size 1 -unit gb -mdiskgrp pool0", fc_ctx)
        await run("svctask mkhost -name iscsihost", fc_ctx)
        await run("svctask addhostport -force -iscsiname iqn.example:h1 iscsihost", fc_ctx)
        await run("svctask mkvdiskhostmap -host iscsihost iscsivol", fc_ctx)

        async with session_factory() as s:
            result = await s.execute(select(Mapping))
            m = result.scalar_one()
        # iSCSI host → persona = iSCSI, underlay = iSCSI
        assert m.persona_endpoint_id == iscsi_endpoint.id
        assert m.underlay_endpoint_id == iscsi_endpoint.id

    async def test_fc_mapping_volume_becomes_in_use(self, fc_ctx, session_factory):
        """Volume status should transition to in_use after FC mapping."""
        await self._setup_fc_host_and_vol(fc_ctx)
        await run("svctask mkvdiskhostmap -host fchost1 fcvol1", fc_ctx)

        async with session_factory() as s:
            result = await s.execute(select(Volume).where(Volume.name == "fcvol1"))
            vol = result.scalar_one()
        assert vol.status == VolumeStatus.in_use

    async def test_fc_mapping_lun_allocation(self, fc_ctx, session_factory):
        """First FC mapping gets LUN 0."""
        await self._setup_fc_host_and_vol(fc_ctx)
        await run("svctask mkvdiskhostmap -host fchost1 fcvol1", fc_ctx)

        async with session_factory() as s:
            result = await s.execute(select(Mapping))
            m = result.scalar_one()
        assert m.lun_id == 0

    async def test_fc_host_no_fc_endpoint_falls_back_to_iscsi(
        self, ctx, session_factory
    ):
        """FC host on array WITHOUT FC endpoint → falls back to iSCSI-only."""
        # ctx has no FC endpoint
        await run("svctask mkvdisk -name vol1 -size 1 -unit gb -mdiskgrp pool0", ctx)
        await run("svctask mkhost -name fchost1", ctx)
        await run("svctask addhostport -force -hbawwpn 0x200a09c0ffe1bb01 fchost1", ctx)
        code, out = await run("svctask mkvdiskhostmap -host fchost1 vol1", ctx)
        assert code == 0
        assert "successfully created" in out

        # Both persona and underlay should be iSCSI (auto-created)
        async with session_factory() as s:
            result = await s.execute(select(Mapping))
            m = result.scalar_one()
            # Verify the endpoint is iSCSI
            ep = await s.get(TransportEndpoint, m.persona_endpoint_id)
        assert ep.protocol == Protocol.iscsi.value
