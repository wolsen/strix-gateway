# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the startup reconciler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strix_gateway.config import Settings
from strix_gateway.core.db import init_db, get_session_factory
from strix_gateway.core.reconcile import reconcile
from strix_gateway.spdk.rpc import SPDKClient, SPDKError

pytestmark = pytest.mark.asyncio

TEST_DB = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def session_factory():
    await init_db(TEST_DB)
    return get_session_factory()


@pytest.fixture
async def default_arr(session_factory):
    """Pre-existing 'default' Array required for non-nullable array_id FK."""
    from strix_gateway.core.db import Array
    async with session_factory() as session:
        arr = Array(
            name="default",
            vendor="generic",
            profile="{}",
        )
        session.add(arr)
        await session.commit()
        await session.refresh(arr)
        return arr


@pytest.fixture
def spdk():
    client = MagicMock(spec=SPDKClient)
    client.call.return_value = None
    return client


@pytest.fixture
def cfg():
    s = MagicMock(spec=Settings)
    s.iscsi_portal_ip = "0.0.0.0"
    s.iscsi_portal_port = 3260
    s.nvmef_portal_ip = "0.0.0.0"
    s.nvmef_portal_port = 4420
    return s


async def test_reconcile_empty_db_succeeds(session_factory, spdk, cfg):
    """With an empty DB, reconcile should run without errors."""
    await reconcile(spdk, session_factory, cfg)


async def test_reconcile_iscsi_infra_failure_is_non_fatal(session_factory, spdk, cfg):
    """An exception from ensure_portal_group should be caught and logged, not raised."""
    spdk.call.side_effect = Exception("socket unavailable")
    # Should complete without raising
    await reconcile(spdk, session_factory, cfg)


async def test_reconcile_nvmef_transport_failure_is_non_fatal(session_factory, spdk, cfg):
    def side_effect(method, *args, **kw):
        if method in ("iscsi_get_portal_groups", "iscsi_get_initiator_groups"):
            return []
        raise Exception("nvmef transport error")

    spdk.call.side_effect = side_effect
    await reconcile(spdk, session_factory, cfg)


async def test_reconcile_with_pool_and_volume(session_factory, default_arr, spdk, cfg):
    """Reconcile correctly ensures pool and volume SPDK resources."""
    from strix_gateway.core.db import Pool, Volume

    async with session_factory() as session:
        pool = Pool(name="rpool", backend_type="malloc", size_mb=1024,
                    array_id=default_arr.id)
        session.add(pool)
        await session.flush()
        vol = Volume(name="rvol", pool_id=pool.id, size_mb=512, status="available",
                     array_id=default_arr.id)
        session.add(vol)
        await session.commit()

    with patch("strix_gateway.core.reconcile.ensure_pool") as mock_pool, \
         patch("strix_gateway.core.reconcile.ensure_lvol", return_value="rpool/apollo-vol-x") as mock_lvol, \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("strix_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)

    mock_pool.assert_called_once()
    mock_lvol.assert_called_once()


async def test_reconcile_volume_with_unknown_pool_is_skipped(session_factory, default_arr, spdk, cfg):
    """A volume referencing a non-existent pool should be skipped gracefully."""
    from strix_gateway.core.db import Volume

    async with session_factory() as session:
        vol = Volume(name="orphan", pool_id="nonexistent-pool", size_mb=512,
                     status="available", array_id=default_arr.id)
        session.add(vol)
        await session.commit()

    with patch("strix_gateway.core.reconcile.ensure_pool"), \
         patch("strix_gateway.core.reconcile.ensure_lvol") as mock_lvol, \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("strix_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)

    mock_lvol.assert_not_called()


async def test_reconcile_with_endpoints_and_mappings(session_factory, default_arr, spdk, cfg):
    """Reconcile calls ensure_iscsi/nvmef_export and ensure_*_mapping for each record."""
    from strix_gateway.core.db import TransportEndpoint, Mapping, Pool, Volume

    async with session_factory() as session:
        pool = Pool(name="rp2", backend_type="malloc", size_mb=512,
                    array_id=default_arr.id)
        session.add(pool)
        await session.flush()
        vol = Volume(name="rv2", pool_id=pool.id, size_mb=256,
                     status="in_use", bdev_name="rp2/apollo-vol-rv2",
                     array_id=default_arr.id)
        session.add(vol)
        await session.flush()
        ep_iscsi = TransportEndpoint(
            protocol="iscsi",
            array_id=default_arr.id,
            targets='{"target_iqn": "iqn.test:ec1"}',
            addresses='{"portals": ["0.0.0.0:3260"]}',
        )
        ep_nvme = TransportEndpoint(
            protocol="nvmeof_tcp",
            array_id=default_arr.id,
            targets='{"subsystem_nqn": "nqn.test:ec2"}',
            addresses='{"listeners": [{"traddr": "0.0.0.0", "trsvcid": "4420"}]}',
        )
        session.add_all([ep_iscsi, ep_nvme])
        await session.flush()
        m1 = Mapping(volume_id=vol.id, host_id="h1",
                     persona_endpoint_id=ep_iscsi.id,
                     underlay_endpoint_id=ep_iscsi.id,
                     lun_id=0, underlay_id=0,
                     desired_state="attached")
        m2 = Mapping(volume_id=vol.id, host_id="h2",
                     persona_endpoint_id=ep_nvme.id,
                     underlay_endpoint_id=ep_nvme.id,
                     lun_id=0, underlay_id=1,
                     desired_state="attached")
        session.add_all([m1, m2])
        await session.commit()

    with patch("strix_gateway.core.reconcile.ensure_pool"), \
         patch("strix_gateway.core.reconcile.ensure_lvol", return_value="rp2/apollo-vol-rv2"), \
         patch("strix_gateway.core.reconcile.ensure_iscsi_export") as mock_iscsi_exp, \
         patch("strix_gateway.core.reconcile.ensure_nvmef_export") as mock_nvme_exp, \
         patch("strix_gateway.core.reconcile.ensure_iscsi_mapping") as mock_iscsi_map, \
         patch("strix_gateway.core.reconcile.ensure_nvmef_mapping") as mock_nvme_map, \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("strix_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)

    mock_iscsi_exp.assert_called_once()
    mock_nvme_exp.assert_called_once()
    mock_iscsi_map.assert_called_once()
    mock_nvme_map.assert_called_once()


async def test_reconcile_ensure_pool_failure_is_non_fatal(session_factory, default_arr, spdk, cfg):
    """An exception from ensure_pool should be logged, not raised."""
    from strix_gateway.core.db import Pool

    async with session_factory() as session:
        session.add(Pool(name="bad-pool", backend_type="malloc", size_mb=512,
                         array_id=default_arr.id))
        await session.commit()

    with patch("strix_gateway.core.reconcile.ensure_pool", side_effect=Exception("pool fail")), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("strix_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)  # must not raise


async def test_reconcile_ensure_lvol_failure_is_non_fatal(session_factory, default_arr, spdk, cfg):
    """An exception from ensure_lvol should be logged, not raised."""
    from strix_gateway.core.db import Pool, Volume

    async with session_factory() as session:
        pool = Pool(name="lf-pool", backend_type="malloc", size_mb=512,
                    array_id=default_arr.id)
        session.add(pool)
        await session.flush()
        session.add(Volume(name="lf-vol", pool_id=pool.id, size_mb=256,
                           status="available", array_id=default_arr.id))
        await session.commit()

    with patch("strix_gateway.core.reconcile.ensure_pool"), \
         patch("strix_gateway.core.reconcile.ensure_lvol", side_effect=Exception("lvol fail")), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("strix_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)


async def test_reconcile_ensure_export_failure_is_non_fatal(session_factory, default_arr, spdk, cfg):
    """An exception from ensure_*_export should be logged, not raised."""
    from strix_gateway.core.db import TransportEndpoint

    async with session_factory() as session:
        session.add(TransportEndpoint(
            protocol="iscsi",
            array_id=default_arr.id,
            targets='{"target_iqn": "iqn.test:bad-ep"}',
            addresses='{"portals": ["0.0.0.0:3260"]}',
        ))
        await session.commit()

    with patch("strix_gateway.core.reconcile.ensure_pool"), \
         patch("strix_gateway.core.reconcile.ensure_iscsi_export", side_effect=Exception("ep fail")), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("strix_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)


async def test_reconcile_mapping_with_missing_volume_or_ep_is_skipped(session_factory, default_arr, spdk, cfg):
    """A mapping whose volume/ep is not in the loaded maps should be skipped."""
    from strix_gateway.core.db import Mapping

    async with session_factory() as session:
        session.add(Mapping(
            volume_id="ghost-vol", host_id="ghost-host",
            persona_endpoint_id="ghost-ep",
            underlay_endpoint_id="ghost-ep",
            lun_id=0, underlay_id=0,
            desired_state="attached",
        ))
        await session.commit()

    with patch("strix_gateway.core.reconcile.ensure_pool"), \
         patch("strix_gateway.core.reconcile.ensure_iscsi_mapping") as mock_map, \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("strix_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)

    mock_map.assert_not_called()


async def test_reconcile_ensure_mapping_failure_is_non_fatal(session_factory, default_arr, spdk, cfg):
    """An exception from ensure_*_mapping should be logged, not raised."""
    from strix_gateway.core.db import TransportEndpoint, Mapping, Pool, Volume

    async with session_factory() as session:
        pool = Pool(name="mf-pool", backend_type="malloc", size_mb=512,
                    array_id=default_arr.id)
        session.add(pool)
        await session.flush()
        vol = Volume(name="mf-vol", pool_id=pool.id, size_mb=256,
                     status="in_use", bdev_name="mf-pool/apollo-vol-x",
                     array_id=default_arr.id)
        session.add(vol)
        await session.flush()
        ep = TransportEndpoint(
            protocol="iscsi",
            array_id=default_arr.id,
            targets='{"target_iqn": "iqn.test:mf-ep"}',
            addresses='{"portals": ["0.0.0.0:3260"]}',
        )
        session.add(ep)
        await session.flush()
        session.add(Mapping(
            volume_id=vol.id, host_id="h1",
            persona_endpoint_id=ep.id,
            underlay_endpoint_id=ep.id,
            lun_id=0, underlay_id=0,
            desired_state="attached",
        ))
        await session.commit()

    with patch("strix_gateway.core.reconcile.ensure_pool"), \
         patch("strix_gateway.core.reconcile.ensure_lvol", return_value="mf-pool/apollo-vol-x"), \
         patch("strix_gateway.core.reconcile.ensure_iscsi_export"), \
         patch("strix_gateway.core.reconcile.ensure_iscsi_mapping", side_effect=Exception("map fail")), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("strix_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("strix_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)
