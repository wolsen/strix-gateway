# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for apply plan ordering and smoke-test logic."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from strix_gateway.cli.errors import APIError, ValidationError
from strix_gateway.cli.client import StrixClient
from strix_gateway.cli.topo.apply import apply_topology, smoke_test
from strix_gateway.cli.topo.models import TopologyFile


def _make_topo() -> TopologyFile:
    """Build a small topology for testing apply order."""
    return TopologyFile.model_validate(
        {
            "arrays": [
                {
                    "name": "a1",
                    "vendor": "generic",
                    "endpoints": [{"protocol": "iscsi"}],
                }
            ],
            "pools": [
                {
                    "name": "p1",
                    "array": "a1",
                    "backend": "malloc",
                    "size_gb": 10,
                }
            ],
            "hosts": [{"name": "h1", "iqns": ["iqn.example"]}],
            "volumes": [{"name": "v1", "size_gb": 5, "pool": "p1"}],
            "mappings": [
                {"host": "h1", "volume": "v1", "protocol": "iscsi"}
            ],
        }
    )


class _FakeClient:
    """Mock client that records call order."""

    def __init__(self):
        self.calls: list[str] = []
        self._arrays: dict[str, dict] = {}
        self._endpoints: dict[str, list[dict]] = {}
        self._pools: dict[str, dict] = {}
        self._hosts: dict[str, dict] = {}
        self._volumes: dict[str, dict] = {}
        self._mappings: list[dict] = []
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"

    # Arrays
    def list_arrays(self):
        self.calls.append("list_arrays")
        return list(self._arrays.values())

    def create_array(self, name, vendor="generic", profile=None):
        self.calls.append(f"create_array:{name}")
        aid = self._next_id()
        self._arrays[name] = {"id": aid, "name": name, "vendor": vendor, "profile": profile}
        return self._arrays[name]

    def update_array(self, name, **fields):
        self.calls.append(f"update_array:{name}")
        self._arrays[name].update(fields)
        return self._arrays[name]

    def get_array(self, name):
        self.calls.append(f"get_array:{name}")
        if name in self._arrays:
            return self._arrays[name]
        raise APIError(404, f"not found: {name}")

    # Endpoints
    def list_endpoints(self, array):
        self.calls.append(f"list_endpoints:{array}")
        return self._endpoints.get(array, [])

    def create_endpoint(self, array, protocol, targets=None, addresses=None, auth=None):
        self.calls.append(f"create_endpoint:{array}:{protocol}")
        eid = self._next_id()
        ep = {"id": eid, "protocol": protocol, "targets": targets or {}, "addresses": addresses or {}, "auth": auth or {}}
        self._endpoints.setdefault(array, []).append(ep)
        return ep

    def update_endpoint(self, array, endpoint_id, targets=None, addresses=None, auth=None):
        self.calls.append(f"update_endpoint:{array}:{endpoint_id}")
        for ep in self._endpoints.get(array, []):
            if ep["id"] == endpoint_id:
                if targets is not None:
                    ep["targets"] = targets
                if addresses is not None:
                    ep["addresses"] = addresses
                if auth is not None:
                    ep["auth"] = auth
                return ep
        raise APIError(404, f"not found: {endpoint_id}")

    # Pools
    def list_pools(self, array=None):
        self.calls.append(f"list_pools:{array}")
        return list(self._pools.values())

    def create_pool(self, name, array, backend, size_gb, aio_path=None):
        self.calls.append(f"create_pool:{name}")
        pid = self._next_id()
        self._pools[name] = {"id": pid, "name": name}
        return self._pools[name]

    def resolve_pool(self, name, array):
        self.calls.append(f"resolve_pool:{name}")
        if name in self._pools:
            return self._pools[name]
        raise ValidationError(f"Pool '{name}' not found in '{array}'")

    # Hosts
    def list_hosts(self):
        self.calls.append("list_hosts")
        return list(self._hosts.values())

    def create_host(self, name, iqns=None, nqns=None, wwpns=None):
        self.calls.append(f"create_host:{name}")
        hid = self._next_id()
        self._hosts[name] = {"id": hid, "name": name}
        return self._hosts[name]

    def resolve_host(self, name):
        self.calls.append(f"resolve_host:{name}")
        if name in self._hosts:
            return self._hosts[name]
        raise ValidationError(f"Host '{name}' not found")

    # Volumes
    def list_volumes(self, array=None):
        self.calls.append(f"list_volumes:{array}")
        return list(self._volumes.values())

    def create_volume(self, name, pool_id, size_gb):
        self.calls.append(f"create_volume:{name}")
        vid = self._next_id()
        self._volumes[name] = {"id": vid, "name": name}
        return self._volumes[name]

    def resolve_volume(self, name, array):
        self.calls.append(f"resolve_volume:{name}")
        if name in self._volumes:
            return self._volumes[name]
        raise ValidationError(f"Volume '{name}' not found in '{array}'")

    # Mappings
    def list_mappings(self, array=None):
        self.calls.append(f"list_mappings:{array}")
        return self._mappings

    def create_mapping(self, volume_id, host_id, protocol=None, **kwargs):
        self.calls.append(f"create_mapping:{volume_id}")
        mid = self._next_id()
        m = {
            "id": mid,
            "volume_id": volume_id,
            "host_id": host_id,
        }
        self._mappings.append(m)
        return m

    def resolve_mapping(self, host_name, volume_name, array):
        self.calls.append(f"resolve_mapping:{host_name}-{volume_name}")
        host = self.resolve_host(host_name)
        vol = self.resolve_volume(volume_name, array)
        for m in self._mappings:
            if m["host_id"] == host["id"] and m["volume_id"] == vol["id"]:
                return m
        raise ValidationError("mapping not found")

    def post(self, path, **kwargs):
        self.calls.append(f"post:{path}")
        return {}


# ------------------------------------------------------------------
# Apply ordering
# ------------------------------------------------------------------


class TestApplyOrdering:
    def test_creation_order(self):
        """Resources must be created in the canonical order."""
        client = _FakeClient()
        topo = _make_topo()
        actions = apply_topology(client, topo)

        # Extract creation calls only
        creates = [c for c in client.calls if c.startswith("create_")]
        # array=id-1, endpoint=id-2, pool=id-3, host=id-4, vol=id-5, mapping uses vol id
        assert creates == [
            "create_array:a1",
            "create_endpoint:a1:iscsi",
            "create_pool:p1",
            "create_host:h1",
            "create_volume:v1",
            "create_mapping:id-5",  # volume_id
        ]

    def test_idempotent_existing_resources(self):
        """When resources already exist, apply should not recreate them."""
        client = _FakeClient()
        # Pre-populate
        client._arrays["a1"] = {"id": "pre-1", "name": "a1", "vendor": "generic", "profile": None}
        client._endpoints["a1"] = [{"id": "pre-ep", "protocol": "iscsi", "targets": {}, "addresses": {}, "auth": {"method": "none"}}]
        client._pools["p1"] = {"id": "pre-2", "name": "p1"}
        client._hosts["h1"] = {"id": "pre-3", "name": "h1"}
        client._volumes["v1"] = {"id": "pre-4", "name": "v1"}
        client._mappings = [
            {
                "id": "pre-5",
                "volume_id": "pre-4",
                "host_id": "pre-3",
            }
        ]

        topo = _make_topo()
        actions = apply_topology(client, topo)

        creates = [c for c in client.calls if c.startswith("create_")]
        assert creates == [], "Nothing should be created when all resources exist"
        updates = [c for c in client.calls if c.startswith("update_")]
        assert updates == [], "Nothing should be updated if vendor/profile match"

    def test_strict_mode_reports_extras(self):
        """Strict mode reports live resources not in topology."""
        client = _FakeClient()
        client._arrays["a1"] = {"id": "pre-1", "name": "a1", "vendor": "generic", "profile": None}
        client._endpoints["a1"] = [{"id": "pre-ep", "protocol": "iscsi", "targets": {}, "addresses": {}, "auth": {"method": "none"}}]
        client._pools["p1"] = {"id": "pre-2", "name": "p1"}
        client._pools["extra-pool"] = {"id": "pre-99", "name": "extra-pool"}
        client._hosts["h1"] = {"id": "pre-3", "name": "h1"}
        client._volumes["v1"] = {"id": "pre-4", "name": "v1"}
        client._mappings = [
            {
                "id": "pre-5",
                "volume_id": "pre-4",
                "host_id": "pre-3",
            }
        ]

        topo = _make_topo()
        actions = apply_topology(client, topo, strict=True)
        strict_msgs = [a for a in actions if "STRICT" in a]
        assert any("extra-pool" in m for m in strict_msgs)


def test_client_create_pool_attaches_non_default_arrays():
    client = object.__new__(StrixClient)
    client.resolve_pool = MagicMock(side_effect=[
        ValidationError("missing from target"),
        ValidationError("missing from default"),
    ])
    client.post = MagicMock(side_effect=[
        {"id": "pool-1", "name": "gold"},
        {"id": "pool-1", "name": "gold", "array_id": "svc-a"},
    ])

    result = StrixClient.create_pool(client, "gold", "svc-a", "malloc", 10)

    assert result["array_id"] == "svc-a"
    assert client.post.mock_calls == [
        call("/v1/pools", json={
            "name": "gold",
            "backend_type": "malloc",
            "size_mb": 10240,
        }),
        call("/v1/arrays/svc-a/pools/pool-1"),
    ]


def test_client_create_pool_reuses_misplaced_default_pool():
    client = object.__new__(StrixClient)
    client.resolve_pool = MagicMock(return_value={"id": "pool-1", "name": "gold"})
    client.post = MagicMock(return_value={
        "id": "pool-1",
        "name": "gold",
        "array_id": "svc-a",
    })

    result = StrixClient.create_pool(client, "gold", "svc-a", "malloc", 10)

    assert result["array_id"] == "svc-a"
    client.post.assert_called_once_with("/v1/arrays/svc-a/pools/pool-1")


# ------------------------------------------------------------------
# Smoke test
# ------------------------------------------------------------------


class TestSmokeTest:
    def test_all_pass(self):
        client = _FakeClient()
        client._arrays["a1"] = {"id": "1", "name": "a1", "vendor": "generic", "profile": None}
        client._pools["p1"] = {"id": "2", "name": "p1"}
        client._hosts["h1"] = {"id": "3", "name": "h1"}
        client._volumes["v1"] = {"id": "4", "name": "v1"}
        client._mappings = [
            {
                "id": "5",
                "volume_id": "4",
                "host_id": "3",
            }
        ]

        topo = _make_topo()
        results = smoke_test(client, topo)
        assert all("\u2713" in r for r in results), f"Expected all checks to pass: {results}"

    def test_missing_array_reported(self):
        client = _FakeClient()
        topo = _make_topo()
        results = smoke_test(client, topo)
        assert any("\u2717" in r and "a1" in r for r in results)
