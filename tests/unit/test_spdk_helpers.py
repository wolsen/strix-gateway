# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for spdk/iscsi.py, spdk/nvmf.py, and spdk/ensure.py helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from strix_gateway.spdk.rpc import SPDKClient, SPDKError
from strix_gateway.spdk import iscsi as iscsi_rpc
from strix_gateway.spdk import nvmf as nvmf_rpc
from strix_gateway.spdk.ensure import (
    _bdev_exists,
    _lvstore_exists,
    delete_lvol,
    ensure_iscsi_export,
    ensure_iscsi_mapping,
    ensure_lvol,
    ensure_nvmef_export,
    ensure_nvmef_mapping,
    ensure_pool,
    resize_lvol,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client(responses: dict[str, object] | None = None) -> MagicMock:
    """Return a mock SPDKClient whose call() returns pre-canned responses."""
    client = MagicMock(spec=SPDKClient)
    if responses:
        client.call.side_effect = lambda method, *args, **kw: responses.get(method)
    else:
        client.call.return_value = None
    return client


def _pool(name="lv0", backend_type="malloc", size_mb=1024, aio_path=None, pool_id="pid-1"):
    p = MagicMock()
    p.id = pool_id
    p.name = name
    p.backend_type = backend_type
    p.size_mb = size_mb
    p.aio_path = aio_path
    return p


def _volume(vol_id="vid-1", size_mb=512, bdev_name="lv0/apollo-vol-vid-1"):
    v = MagicMock()
    v.id = vol_id
    v.size_mb = size_mb
    v.bdev_name = bdev_name
    return v


def _ec(ec_id="ec-1", protocol="iscsi", target_iqn=None, target_nqn=None,
        portal_ip="0.0.0.0", portal_port=3260):
    """Create a mock TransportEndpoint with JSON targets/addresses/auth."""
    import json
    ec = MagicMock()
    ec.id = ec_id
    ec.protocol = protocol
    iqn = target_iqn or f"iqn.2026-02.lunacysystems.apollo:{ec_id}"
    nqn = target_nqn or f"nqn.2026-02.io.lunacysystems:apollo:{ec_id}"
    if protocol == "iscsi":
        ec.targets = json.dumps({"target_iqn": iqn})
        ec.addresses = json.dumps({"portals": [f"{portal_ip}:{portal_port}"]})
    else:
        ec.targets = json.dumps({"subsystem_nqn": nqn})
        ec.addresses = json.dumps({"portals": [f"{portal_ip}:{portal_port}"]})
    ec.auth = json.dumps({})
    # Keep convenience attributes for tests that reference them directly
    ec.target_iqn = iqn
    ec.target_nqn = nqn
    ec.portal_ip = portal_ip
    ec.portal_port = portal_port
    return ec


def _mapping(map_id="m-1", lun_id=0, underlay_id=0):
    m = MagicMock()
    m.id = map_id
    m.lun_id = lun_id
    m.underlay_id = underlay_id or lun_id
    return m


def _settings(iscsi_ip="0.0.0.0", iscsi_port=3260, nvmef_ip="0.0.0.0", nvmef_port=4420):
    s = MagicMock()
    s.iscsi_portal_ip = iscsi_ip
    s.iscsi_portal_port = iscsi_port
    s.nvmef_portal_ip = nvmef_ip
    s.nvmef_portal_port = nvmef_port
    return s


# ===========================================================================
# spdk/iscsi.py
# ===========================================================================

class TestEnsurePortalGroup:
    def test_creates_when_absent(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = []  # iscsi_get_portal_groups returns empty list
        iscsi_rpc.ensure_portal_group(client, "0.0.0.0", 3260)
        client.call.assert_any_call("iscsi_create_portal_group", {
            "tag": 1,
            "portals": [{"host": "0.0.0.0", "port": "3260"}],
        })

    def test_skips_when_tag1_already_exists(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = [{"tag": 1}]  # already exists
        iscsi_rpc.ensure_portal_group(client, "0.0.0.0", 3260)
        # Should only call get, not create
        assert client.call.call_count == 1
        client.call.assert_called_once_with("iscsi_get_portal_groups")

    def test_creates_when_other_tags_present_but_not_1(self):
        """Loop iterates past non-matching tags then falls through to create."""
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = [{"tag": 2}, {"tag": 3}]
        iscsi_rpc.ensure_portal_group(client, "0.0.0.0", 3260)
        client.call.assert_any_call("iscsi_create_portal_group", {
            "tag": 1,
            "portals": [{"host": "0.0.0.0", "port": "3260"}],
        })


class TestEnsureInitiatorGroup:
    def test_creates_when_absent(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = []
        iscsi_rpc.ensure_initiator_group(client)
        client.call.assert_any_call("iscsi_create_initiator_group", {
            "tag": 1,
            "initiators": ["ANY"],
            "netmasks": ["ANY"],
        })

    def test_creates_when_other_tags_present_but_not_1(self):
        """Loop iterates past non-matching tags then falls through to create."""
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = [{"tag": 5}]
        iscsi_rpc.ensure_initiator_group(client)
        client.call.assert_any_call("iscsi_create_initiator_group", {
            "tag": 1,
            "initiators": ["ANY"],
            "netmasks": ["ANY"],
        })

    def test_skips_when_tag1_already_exists(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = [{"tag": 1}]
        iscsi_rpc.ensure_initiator_group(client)
        assert client.call.call_count == 1


class TestDeleteTargetNode:
    def test_tolerates_not_found(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-32602, "target not found")
        # Should not raise
        iscsi_rpc.delete_target_node(client, "iqn.test:target")

    def test_reraises_other_spdk_errors(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-1, "internal error")
        with pytest.raises(SPDKError):
            iscsi_rpc.delete_target_node(client, "iqn.test:target")


class TestGetLunIdsOnTarget:
    def test_returns_lun_ids_when_found(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = [
            {"name": "iqn.test:t1", "luns": [{"lun_id": 0}, {"lun_id": 1}]},
        ]
        result = iscsi_rpc.get_lun_ids_on_target(client, "iqn.test:t1")
        assert result == [0, 1]

    def test_returns_empty_when_iqn_not_found(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = [{"name": "iqn.test:other", "luns": []}]
        result = iscsi_rpc.get_lun_ids_on_target(client, "iqn.test:missing")
        assert result == []


# ===========================================================================
# spdk/nvmf.py
# ===========================================================================

class TestEnsureTransport:
    def test_creates_transport(self):
        client = MagicMock(spec=SPDKClient)
        nvmf_rpc.ensure_transport(client)
        client.call.assert_called_once_with("nvmf_create_transport", {"trtype": "TCP"})

    def test_swallows_already_exists(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-32602, "transport already exists")
        # Should not raise
        nvmf_rpc.ensure_transport(client)

    def test_reraises_other_spdk_errors(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-1, "internal error")
        with pytest.raises(SPDKError):
            nvmf_rpc.ensure_transport(client)


class TestRemoveNamespace:
    def test_tolerates_not_found(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-1, "namespace not found")
        nvmf_rpc.remove_namespace(client, "nqn.test", 1)

    def test_reraises_other_errors(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-1, "something else")
        # "not found" is NOT in "something else", so it re-raises
        with pytest.raises(SPDKError):
            nvmf_rpc.remove_namespace(client, "nqn.test", 1)


class TestDeleteSubsystem:
    def test_tolerates_not_found(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-1, "subsystem not found")
        nvmf_rpc.delete_subsystem(client, "nqn.test")

    def test_reraises_other_errors(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-1, "other failure")
        with pytest.raises(SPDKError):
            nvmf_rpc.delete_subsystem(client, "nqn.test")


class TestGetNamespaces:
    def test_returns_namespaces_when_found(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = [
            {"nqn": "nqn.test", "namespaces": [{"nsid": 1, "name": "bdev0"}]},
        ]
        result = nvmf_rpc.get_namespaces(client, "nqn.test")
        assert result == [{"nsid": 1, "name": "bdev0"}]

    def test_returns_empty_when_nqn_not_found(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = [{"nqn": "nqn.other", "namespaces": []}]
        result = nvmf_rpc.get_namespaces(client, "nqn.missing")
        assert result == []


# ===========================================================================
# spdk/ensure.py
# ===========================================================================

class TestBdevExists:
    def test_returns_true_when_result_non_empty(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = [{"name": "bdev0"}]
        assert _bdev_exists(client, "bdev0") is True

    def test_returns_false_on_spdk_error(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-32602, "bdev not found")
        assert _bdev_exists(client, "missing") is False

    def test_returns_false_when_result_empty(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = []
        assert _bdev_exists(client, "bdev0") is False


class TestLvstoreExists:
    def test_returns_false_on_spdk_error(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-32602, "lvstore not found")
        assert _lvstore_exists(client, "missing") is False


class TestEnsurePool:
    def test_malloc_creates_bdev_and_lvstore(self):
        client = MagicMock(spec=SPDKClient)
        client.call.return_value = None  # bdev_get_bdevs SPDKError = not found = False

        # Make _bdev_exists return False and _lvstore_exists return False
        with patch("strix_gateway.spdk.ensure._bdev_exists", return_value=False), \
             patch("strix_gateway.spdk.ensure._lvstore_exists", return_value=False):
            ensure_pool(client, _pool(), "test-sub")

        calls = [c[0][0] for c in client.call.call_args_list]
        assert "bdev_malloc_create" in calls
        assert "bdev_lvol_create_lvstore" in calls

    def test_malloc_skips_when_bdev_and_lvstore_exist(self):
        client = MagicMock(spec=SPDKClient)
        with patch("strix_gateway.spdk.ensure._bdev_exists", return_value=True), \
             patch("strix_gateway.spdk.ensure._lvstore_exists", return_value=True):
            ensure_pool(client, _pool(), "test-sub")
        client.call.assert_not_called()

    def test_aio_file_creates_bdev_and_lvstore(self):
        client = MagicMock(spec=SPDKClient)
        with patch("strix_gateway.spdk.ensure._bdev_exists", return_value=False), \
             patch("strix_gateway.spdk.ensure._lvstore_exists", return_value=False):
            ensure_pool(client, _pool(backend_type="aio_file", aio_path="/dev/sdb"), "test-sub")
        calls = [c[0][0] for c in client.call.call_args_list]
        assert "bdev_aio_create" in calls
        assert "bdev_lvol_create_lvstore" in calls

    def test_aio_file_missing_aio_path_raises(self):
        client = MagicMock(spec=SPDKClient)
        with patch("strix_gateway.spdk.ensure._bdev_exists", return_value=False):
            with pytest.raises(ValueError, match="aio_path"):
                ensure_pool(client, _pool(backend_type="aio_file", aio_path=None), "test-sub")

    def test_malloc_missing_size_mb_raises(self):
        client = MagicMock(spec=SPDKClient)
        with patch("strix_gateway.spdk.ensure._bdev_exists", return_value=False):
            with pytest.raises(ValueError, match="size_mb"):
                ensure_pool(client, _pool(backend_type="malloc", size_mb=None), "test-sub")

    def test_unknown_backend_raises(self):
        client = MagicMock(spec=SPDKClient)
        with patch("strix_gateway.spdk.ensure._bdev_exists", return_value=False):
            with pytest.raises(ValueError, match="Unknown backend"):
                ensure_pool(client, _pool(backend_type="unknown"), "test-sub")

    def test_lvstore_skipped_when_exists(self):
        client = MagicMock(spec=SPDKClient)
        with patch("strix_gateway.spdk.ensure._bdev_exists", return_value=False), \
             patch("strix_gateway.spdk.ensure._lvstore_exists", return_value=True):
            ensure_pool(client, _pool(), "test-sub")
        calls = [c[0][0] for c in client.call.call_args_list]
        assert "bdev_malloc_create" in calls
        assert "bdev_lvol_create_lvstore" not in calls


class TestEnsureLvol:
    def test_creates_when_absent(self):
        client = MagicMock(spec=SPDKClient)
        with patch("strix_gateway.spdk.ensure._bdev_exists", return_value=False):
            name = ensure_lvol(client, _volume(vol_id="abc"), "mypool", "test-sub")
        assert name == "test-sub.mypool/apollo-vol-abc"
        client.call.assert_called_once_with("bdev_lvol_create", {
            "lvol_name": "apollo-vol-abc",
            "size_in_mib": 512,
            "lvs_name": "test-sub.mypool",
        })

    def test_skips_when_exists(self):
        client = MagicMock(spec=SPDKClient)
        with patch("strix_gateway.spdk.ensure._bdev_exists", return_value=True):
            name = ensure_lvol(client, _volume(vol_id="abc"), "mypool", "test-sub")
        assert name == "test-sub.mypool/apollo-vol-abc"
        client.call.assert_not_called()


class TestDeleteLvol:
    def test_deletes_successfully(self):
        client = MagicMock(spec=SPDKClient)
        delete_lvol(client, "mypool/lvol-1")
        client.call.assert_called_once_with("bdev_lvol_delete", {"name": "mypool/lvol-1"})

    def test_tolerates_not_found(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-1, "not found")
        delete_lvol(client, "mypool/lvol-1")  # should not raise

    def test_reraises_other_errors(self):
        client = MagicMock(spec=SPDKClient)
        client.call.side_effect = SPDKError(-1, "io error")
        with pytest.raises(SPDKError):
            delete_lvol(client, "mypool/lvol-1")


class TestEnsureIscsiExport:
    def test_ensures_portal_and_initiator_groups(self):
        client = MagicMock(spec=SPDKClient)
        ec = _ec()
        with patch("strix_gateway.spdk.ensure.iscsi_rpc.ensure_portal_group") as mock_pg, \
             patch("strix_gateway.spdk.ensure.iscsi_rpc.ensure_initiator_group") as mock_ig:
            ensure_iscsi_export(client, ec, _settings())
        mock_pg.assert_called_once()
        mock_ig.assert_called_once()


class TestEnsureNvmefExport:
    def test_creates_subsystem_when_absent(self):
        client = MagicMock(spec=SPDKClient)
        ec = _ec(protocol="nvmeof_tcp")
        with patch("strix_gateway.spdk.ensure.nvmf_rpc.ensure_transport"), \
             patch("strix_gateway.spdk.ensure.nvmf_rpc.subsystem_exists", return_value=False), \
             patch("strix_gateway.spdk.ensure.nvmf_rpc.create_subsystem") as mock_create, \
             patch("strix_gateway.spdk.ensure.nvmf_rpc.add_listener") as mock_listener:
            ensure_nvmef_export(client, ec, _settings())
        mock_create.assert_called_once_with(
            client, ec.target_nqn,
            model_number="Apollo Gateway",
            serial_number="APOLLO0001",
        )
        mock_listener.assert_called_once()

    def test_skips_when_subsystem_exists(self):
        client = MagicMock(spec=SPDKClient)
        ec = _ec(protocol="nvmeof_tcp")
        with patch("strix_gateway.spdk.ensure.nvmf_rpc.ensure_transport"), \
             patch("strix_gateway.spdk.ensure.nvmf_rpc.subsystem_exists", return_value=True), \
             patch("strix_gateway.spdk.ensure.nvmf_rpc.create_subsystem") as mock_create:
            ensure_nvmef_export(client, ec, _settings())
        mock_create.assert_not_called()


class TestEnsureIscsiMapping:
    def test_creates_target_with_first_lun_when_absent(self):
        client = MagicMock(spec=SPDKClient)
        ec = _ec()
        vol = _volume()
        mapping = _mapping(lun_id=0)
        with patch("strix_gateway.spdk.ensure.iscsi_rpc.target_node_exists", return_value=False), \
             patch("strix_gateway.spdk.ensure.iscsi_rpc.create_target_node") as mock_create:
            ensure_iscsi_mapping(client, mapping, vol, ec)
        mock_create.assert_called_once_with(
            client, ec.target_iqn,
            luns=[{"bdev_name": vol.bdev_name, "lun_id": 0}],
        )

    def test_adds_lun_when_target_exists(self):
        client = MagicMock(spec=SPDKClient)
        ec = _ec()
        vol = _volume()
        mapping = _mapping(lun_id=2)
        with patch("strix_gateway.spdk.ensure.iscsi_rpc.target_node_exists", return_value=True), \
             patch("strix_gateway.spdk.ensure.iscsi_rpc.get_lun_ids_on_target", return_value=[0, 1]), \
             patch("strix_gateway.spdk.ensure.iscsi_rpc.add_lun") as mock_add:
            ensure_iscsi_mapping(client, mapping, vol, ec)
        mock_add.assert_called_once_with(client, ec.target_iqn, vol.bdev_name, 2)

    def test_skips_when_lun_already_present(self):
        client = MagicMock(spec=SPDKClient)
        ec = _ec()
        vol = _volume()
        mapping = _mapping(lun_id=0)
        with patch("strix_gateway.spdk.ensure.iscsi_rpc.target_node_exists", return_value=True), \
             patch("strix_gateway.spdk.ensure.iscsi_rpc.get_lun_ids_on_target", return_value=[0]), \
             patch("strix_gateway.spdk.ensure.iscsi_rpc.add_lun") as mock_add:
            ensure_iscsi_mapping(client, mapping, vol, ec)
        mock_add.assert_not_called()


class TestEnsureNvmefMapping:
    def test_adds_namespace_when_absent(self):
        client = MagicMock(spec=SPDKClient)
        ec = _ec(protocol="nvmeof_tcp")
        vol = _volume()
        mapping = _mapping(underlay_id=3)
        with patch("strix_gateway.spdk.ensure.nvmf_rpc.get_nsids", return_value=[1, 2]), \
             patch("strix_gateway.spdk.ensure.nvmf_rpc.add_namespace") as mock_add:
            ensure_nvmef_mapping(client, mapping, vol, ec)
        mock_add.assert_called_once_with(client, ec.target_nqn, vol.bdev_name, 3)

    def test_skips_when_nsid_already_present(self):
        client = MagicMock(spec=SPDKClient)
        ec = _ec(protocol="nvmeof_tcp")
        vol = _volume()
        mapping = _mapping(underlay_id=1)
        with patch("strix_gateway.spdk.ensure.nvmf_rpc.get_nsids", return_value=[1]), \
             patch("strix_gateway.spdk.ensure.nvmf_rpc.add_namespace") as mock_add:
            ensure_nvmef_mapping(client, mapping, vol, ec)
        mock_add.assert_not_called()
