# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for Hitachi ID translation layer.

Tests run in-memory — no database required.  We exercise the mapper's
in-memory lookups and response builders.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from strix_gateway.personalities.hitachi.translate import HitachiIdMapper


def _mock_volume(vid: str, name: str, pool_id: str, size_mb: int, ldev_id: int | None = None):
    vol = MagicMock()
    vol.id = vid
    vol.name = name
    vol.pool_id = pool_id
    vol.size_mb = size_mb
    vol.status = "available"
    vol.mappings = []
    vol.created_at = datetime(2025, 1, 1)
    meta = {"ldev_id": ldev_id} if ldev_id is not None else {}
    vol.vendor_metadata = json.dumps(meta)
    vol.vendor_meta_dict = meta
    return vol


def _mock_pool(pid: str, name: str, size_mb: int, pool_id: int | None = None):
    pool = MagicMock()
    pool.id = pid
    pool.name = name
    pool.size_mb = size_mb
    pool.created_at = datetime(2025, 1, 1)
    meta = {"pool_id": pool_id} if pool_id is not None else {}
    pool.vendor_metadata = json.dumps(meta)
    pool.vendor_meta_dict = meta
    return pool


def _mock_host(hid: str, name: str, fc_wwpns=None, iscsi_iqns=None, hg_map=None, it_map=None):
    host = MagicMock()
    host.id = hid
    host.name = name
    host.fc_wwpns = fc_wwpns or []
    host.iscsi_iqns = iscsi_iqns or []
    meta = {}
    if hg_map:
        meta["hitachi_host_groups"] = hg_map
    if it_map:
        meta["hitachi_iscsi_targets"] = it_map
    host.vendor_meta_dict = meta
    host.vendor_metadata = json.dumps(meta)
    return host


def _mock_array(aid: str, name: str):
    arr = MagicMock()
    arr.id = aid
    arr.name = name
    arr.vendor = "hitachi"
    arr.profile = "{}"
    arr.profile_dict = {}
    return arr


def _mock_mapping(mid: str, vol_id: str, host_id: str, lun_id: int, persona_ep_id: str):
    m = MagicMock()
    m.id = mid
    m.volume_id = vol_id
    m.host_id = host_id
    m.lun_id = lun_id
    m.persona_endpoint_id = persona_ep_id
    return m


class TestHitachiIdMapper:
    """Tests for in-memory ID lookups and response builders."""

    def setup_method(self):
        self.mapper = HitachiIdMapper("array-1")

    def test_register_and_lookup_ldev(self):
        self.mapper.register_ldev(42, "vol-uuid-1")
        assert self.mapper.ldev_for_volume("vol-uuid-1") == 42
        assert self.mapper.volume_for_ldev(42) == "vol-uuid-1"

    def test_unregister_ldev(self):
        self.mapper.register_ldev(10, "vol-uuid-2")
        self.mapper.unregister_ldev("vol-uuid-2")
        assert self.mapper.ldev_for_volume("vol-uuid-2") is None
        assert self.mapper.volume_for_ldev(10) is None

    def test_next_ldev_id_empty(self):
        assert self.mapper.next_ldev_id() == 0

    def test_next_ldev_id_after_register(self):
        self.mapper.register_ldev(5, "vol-a")
        self.mapper.register_ldev(10, "vol-b")
        assert self.mapper.next_ldev_id() == 11

    def test_volume_to_ldev_response(self):
        self.mapper.register_ldev(7, "vol-1")
        self.mapper._uuid_to_pool["pool-1"] = 0

        vol = _mock_volume("vol-1", "test-vol", "pool-1", 1024, ldev_id=7)
        pool = _mock_pool("pool-1", "pool0", 8192, pool_id=0)

        resp = self.mapper.volume_to_ldev(vol, pool)
        assert resp["ldevId"] == 7
        assert resp["label"] == "test-vol"
        assert resp["poolId"] == 0
        assert resp["byteFormatCapacity"] == str(1024 * 1024 * 1024)
        assert resp["status"] == "NML"
        assert resp["emulationType"] == "OPEN-V"

    def test_pool_to_hitachi_response(self):
        self.mapper._uuid_to_pool["pool-1"] = 3

        pool = _mock_pool("pool-1", "my-pool", 8192, pool_id=3)
        stats = {"volume_count": 5, "used_capacity_mb": 2048}

        resp = self.mapper.pool_to_hitachi(pool, stats)
        assert resp["poolId"] == 3
        assert resp["poolName"] == "my-pool"
        assert resp["numOfLdevs"] == 5
        assert resp["totalPoolCapacity"] == 8192 * 1024 * 1024

    def test_host_to_host_group_response(self):
        host = _mock_host("host-1", "compute-01", fc_wwpns=["10:00:00:00:00:00:00:01"])
        resp = self.mapper.host_to_host_group(host, "CL1-A", 1)
        assert resp["hostGroupId"] == "CL1-A,1"
        assert resp["portId"] == "CL1-A"
        assert resp["hostGroupNumber"] == 1
        assert resp["hostGroupName"] == "compute-01"

    def test_host_to_iscsi_target_response(self):
        host = _mock_host("host-2", "compute-02", iscsi_iqns=["iqn.test"])
        resp = self.mapper.host_to_iscsi_target(host, "CL3-A", 0)
        assert resp["iscsiTargetId"] == "CL3-A,0"
        assert resp["iscsiTargetName"] == "compute-02"

    def test_mapping_to_lun_response(self):
        self.mapper.register_ldev(42, "vol-1")
        mapping = _mock_mapping("map-1", "vol-1", "host-1", 0, "ep-1")
        resp = self.mapper.mapping_to_lun(mapping, "CL1-A", 1)
        assert resp["lunId"] == "CL1-A,1,0"
        assert resp["ldevId"] == 42
        assert resp["lun"] == 0

    def test_array_to_storage_response(self):
        arr = _mock_array("abcdef12-0000-0000-0000-000000000000", "hitachi-array")
        self.mapper.storage_device_id = "ABCDEF"
        resp = self.mapper.array_to_storage(arr)
        assert resp["storageDeviceId"] == "ABCDEF"
        assert resp["model"] == "VSP G900"
