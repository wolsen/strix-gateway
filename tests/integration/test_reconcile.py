# FILE: tests/integration/test_reconcile.py
"""Tests for the startup reconciler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo_gateway.config import Settings
from apollo_gateway.core.db import init_db, get_session_factory
from apollo_gateway.core.reconcile import reconcile
from apollo_gateway.spdk.rpc import SPDKClient, SPDKError

pytestmark = pytest.mark.asyncio

TEST_DB = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def session_factory():
    await init_db(TEST_DB)
    return get_session_factory()


@pytest.fixture
async def default_sub(session_factory):
    """Pre-existing 'default' Subsystem required for non-nullable subsystem_id FK."""
    from apollo_gateway.core.db import Subsystem
    async with session_factory() as session:
        sub = Subsystem(
            name="default",
            persona="generic",
            protocols_enabled='["iscsi","nvmeof_tcp"]',
            capability_profile="{}",
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        return sub


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


async def test_reconcile_with_pool_and_volume(session_factory, default_sub, spdk, cfg):
    """Reconcile correctly ensures pool and volume SPDK resources."""
    from apollo_gateway.core.db import Pool, Volume

    async with session_factory() as session:
        pool = Pool(name="rpool", backend_type="malloc", size_mb=1024,
                    subsystem_id=default_sub.id)
        session.add(pool)
        await session.flush()
        vol = Volume(name="rvol", pool_id=pool.id, size_mb=512, status="available",
                     subsystem_id=default_sub.id)
        session.add(vol)
        await session.commit()

    with patch("apollo_gateway.core.reconcile.ensure_pool") as mock_pool, \
         patch("apollo_gateway.core.reconcile.ensure_lvol", return_value="rpool/apollo-vol-x") as mock_lvol, \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("apollo_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)

    mock_pool.assert_called_once()
    mock_lvol.assert_called_once()


async def test_reconcile_volume_with_unknown_pool_is_skipped(session_factory, default_sub, spdk, cfg):
    """A volume referencing a non-existent pool should be skipped gracefully."""
    from apollo_gateway.core.db import Volume

    async with session_factory() as session:
        vol = Volume(name="orphan", pool_id="nonexistent-pool", size_mb=512,
                     status="available", subsystem_id=default_sub.id)
        session.add(vol)
        await session.commit()

    with patch("apollo_gateway.core.reconcile.ensure_pool"), \
         patch("apollo_gateway.core.reconcile.ensure_lvol") as mock_lvol, \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("apollo_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)

    mock_lvol.assert_not_called()


async def test_reconcile_with_export_containers_and_mappings(session_factory, default_sub, spdk, cfg):
    """Reconcile calls ensure_iscsi/nvmef_export and ensure_*_mapping for each record."""
    from apollo_gateway.core.db import ExportContainer, Mapping, Pool, Volume

    async with session_factory() as session:
        pool = Pool(name="rp2", backend_type="malloc", size_mb=512,
                    subsystem_id=default_sub.id)
        session.add(pool)
        await session.flush()
        vol = Volume(name="rv2", pool_id=pool.id, size_mb=256,
                     status="in_use", bdev_name="rp2/apollo-vol-rv2",
                     subsystem_id=default_sub.id)
        session.add(vol)
        await session.flush()
        ec_iscsi = ExportContainer(
            protocol="iscsi", host_id="h1",
            target_iqn="iqn.test:ec1", target_nqn=None,
            portal_ip="0.0.0.0", portal_port=3260,
            subsystem_id=default_sub.id,
        )
        ec_nvme = ExportContainer(
            protocol="nvmeof_tcp", host_id="h2",
            target_iqn=None, target_nqn="nqn.test:ec2",
            portal_ip="0.0.0.0", portal_port=4420,
            subsystem_id=default_sub.id,
        )
        session.add_all([ec_iscsi, ec_nvme])
        await session.flush()
        m1 = Mapping(volume_id=vol.id, host_id="h1", export_container_id=ec_iscsi.id,
                     protocol="iscsi", lun_id=0, subsystem_id=default_sub.id)
        m2 = Mapping(volume_id=vol.id, host_id="h2", export_container_id=ec_nvme.id,
                     protocol="nvmeof_tcp", ns_id=1, subsystem_id=default_sub.id)
        session.add_all([m1, m2])
        await session.commit()

    with patch("apollo_gateway.core.reconcile.ensure_pool"), \
         patch("apollo_gateway.core.reconcile.ensure_lvol", return_value="rp2/apollo-vol-rv2"), \
         patch("apollo_gateway.core.reconcile.ensure_iscsi_export") as mock_iscsi_exp, \
         patch("apollo_gateway.core.reconcile.ensure_nvmef_export") as mock_nvme_exp, \
         patch("apollo_gateway.core.reconcile.ensure_iscsi_mapping") as mock_iscsi_map, \
         patch("apollo_gateway.core.reconcile.ensure_nvmef_mapping") as mock_nvme_map, \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("apollo_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)

    mock_iscsi_exp.assert_called_once()
    mock_nvme_exp.assert_called_once()
    mock_iscsi_map.assert_called_once()
    mock_nvme_map.assert_called_once()


async def test_reconcile_ensure_pool_failure_is_non_fatal(session_factory, default_sub, spdk, cfg):
    """An exception from ensure_pool should be logged, not raised."""
    from apollo_gateway.core.db import Pool

    async with session_factory() as session:
        session.add(Pool(name="bad-pool", backend_type="malloc", size_mb=512,
                         subsystem_id=default_sub.id))
        await session.commit()

    with patch("apollo_gateway.core.reconcile.ensure_pool", side_effect=Exception("pool fail")), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("apollo_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)  # must not raise


async def test_reconcile_ensure_lvol_failure_is_non_fatal(session_factory, default_sub, spdk, cfg):
    """An exception from ensure_lvol should be logged, not raised."""
    from apollo_gateway.core.db import Pool, Volume

    async with session_factory() as session:
        pool = Pool(name="lf-pool", backend_type="malloc", size_mb=512,
                    subsystem_id=default_sub.id)
        session.add(pool)
        await session.flush()
        session.add(Volume(name="lf-vol", pool_id=pool.id, size_mb=256,
                           status="available", subsystem_id=default_sub.id))
        await session.commit()

    with patch("apollo_gateway.core.reconcile.ensure_pool"), \
         patch("apollo_gateway.core.reconcile.ensure_lvol", side_effect=Exception("lvol fail")), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("apollo_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)


async def test_reconcile_ensure_export_failure_is_non_fatal(session_factory, default_sub, spdk, cfg):
    """An exception from ensure_*_export should be logged, not raised."""
    from apollo_gateway.core.db import ExportContainer

    async with session_factory() as session:
        session.add(ExportContainer(
            protocol="iscsi", host_id="h99",
            target_iqn="iqn.test:bad-ec", target_nqn=None,
            portal_ip="0.0.0.0", portal_port=3260,
            subsystem_id=default_sub.id,
        ))
        await session.commit()

    with patch("apollo_gateway.core.reconcile.ensure_pool"), \
         patch("apollo_gateway.core.reconcile.ensure_iscsi_export", side_effect=Exception("ec fail")), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("apollo_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)


async def test_reconcile_mapping_with_missing_volume_or_ec_is_skipped(session_factory, default_sub, spdk, cfg):
    """A mapping whose volume/ec is not in the loaded maps should be skipped."""
    from apollo_gateway.core.db import Mapping

    async with session_factory() as session:
        session.add(Mapping(
            volume_id="ghost-vol", host_id="ghost-host",
            export_container_id="ghost-ec",
            protocol="iscsi", lun_id=0,
            subsystem_id=default_sub.id,
        ))
        await session.commit()

    with patch("apollo_gateway.core.reconcile.ensure_pool"), \
         patch("apollo_gateway.core.reconcile.ensure_iscsi_mapping") as mock_map, \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("apollo_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)

    mock_map.assert_not_called()


async def test_reconcile_ensure_mapping_failure_is_non_fatal(session_factory, default_sub, spdk, cfg):
    """An exception from ensure_*_mapping should be logged, not raised."""
    from apollo_gateway.core.db import ExportContainer, Mapping, Pool, Volume

    async with session_factory() as session:
        pool = Pool(name="mf-pool", backend_type="malloc", size_mb=512,
                    subsystem_id=default_sub.id)
        session.add(pool)
        await session.flush()
        vol = Volume(name="mf-vol", pool_id=pool.id, size_mb=256,
                     status="in_use", bdev_name="mf-pool/apollo-vol-x",
                     subsystem_id=default_sub.id)
        session.add(vol)
        await session.flush()
        ec = ExportContainer(
            protocol="iscsi", host_id="h1",
            target_iqn="iqn.test:mf-ec", target_nqn=None,
            portal_ip="0.0.0.0", portal_port=3260,
            subsystem_id=default_sub.id,
        )
        session.add(ec)
        await session.flush()
        session.add(Mapping(
            volume_id=vol.id, host_id="h1", export_container_id=ec.id,
            protocol="iscsi", lun_id=0, subsystem_id=default_sub.id,
        ))
        await session.commit()

    with patch("apollo_gateway.core.reconcile.ensure_pool"), \
         patch("apollo_gateway.core.reconcile.ensure_lvol", return_value="mf-pool/apollo-vol-x"), \
         patch("apollo_gateway.core.reconcile.ensure_iscsi_export"), \
         patch("apollo_gateway.core.reconcile.ensure_iscsi_mapping", side_effect=Exception("map fail")), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_portal_group"), \
         patch("apollo_gateway.core.reconcile.iscsi_rpc.ensure_initiator_group"), \
         patch("apollo_gateway.core.reconcile.nvmf_rpc.ensure_transport"):
        await reconcile(spdk, session_factory, cfg)
