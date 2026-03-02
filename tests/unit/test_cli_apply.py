# FILE: tests/unit/test_cli_apply.py
"""Tests for apply plan ordering and smoke-test logic."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from apollo_gateway.cli.errors import APIError, ValidationError
from apollo_gateway.cli.topo.apply import apply_topology, smoke_test
from apollo_gateway.cli.topo.models import TopologyFile


def _make_topo() -> TopologyFile:
    """Build a small topology for testing apply order."""
    return TopologyFile.model_validate(
        {
            "subsystems": [{"name": "s1", "protocols": ["iscsi"]}],
            "pools": [
                {
                    "name": "p1",
                    "subsystem": "s1",
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
        self._subsystems: dict[str, dict] = {}
        self._pools: dict[str, dict] = {}
        self._hosts: dict[str, dict] = {}
        self._volumes: dict[str, dict] = {}
        self._mappings: list[dict] = []
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"

    # Subsystems
    def list_subsystems(self):
        self.calls.append("list_subsystems")
        return list(self._subsystems.values())

    def create_subsystem(self, name, persona="generic", protocols=None, cap=None):
        self.calls.append(f"create_subsystem:{name}")
        sid = self._next_id()
        self._subsystems[name] = {"id": sid, "name": name}
        return self._subsystems[name]

    def get_subsystem(self, name):
        self.calls.append(f"get_subsystem:{name}")
        if name in self._subsystems:
            return self._subsystems[name]
        raise APIError(404, f"not found: {name}")

    # Pools
    def list_pools(self, subsystem=None):
        self.calls.append(f"list_pools:{subsystem}")
        return list(self._pools.values())

    def create_pool(self, name, subsystem, backend, size_gb, aio_path=None):
        self.calls.append(f"create_pool:{name}")
        pid = self._next_id()
        self._pools[name] = {"id": pid, "name": name}
        return self._pools[name]

    def resolve_pool(self, name, subsystem):
        self.calls.append(f"resolve_pool:{name}")
        if name in self._pools:
            return self._pools[name]
        raise ValidationError(f"Pool '{name}' not found in '{subsystem}'")

    # Hosts
    def list_hosts(self):
        self.calls.append("list_hosts")
        return list(self._hosts.values())

    def create_host(self, name, iqn=None, nqn=None):
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
    def list_volumes(self, subsystem=None):
        self.calls.append(f"list_volumes:{subsystem}")
        return list(self._volumes.values())

    def create_volume(self, name, pool_id, size_gb):
        self.calls.append(f"create_volume:{name}")
        vid = self._next_id()
        self._volumes[name] = {"id": vid, "name": name}
        return self._volumes[name]

    def resolve_volume(self, name, subsystem):
        self.calls.append(f"resolve_volume:{name}")
        if name in self._volumes:
            return self._volumes[name]
        raise ValidationError(f"Volume '{name}' not found in '{subsystem}'")

    # Mappings
    def list_mappings(self, subsystem=None):
        self.calls.append(f"list_mappings:{subsystem}")
        return self._mappings

    def create_mapping(self, volume_id, host_id, protocol):
        self.calls.append(f"create_mapping:{volume_id}")
        mid = self._next_id()
        m = {
            "id": mid,
            "volume_id": volume_id,
            "host_id": host_id,
            "protocol": protocol,
        }
        self._mappings.append(m)
        return m

    def resolve_mapping(self, host_name, volume_name, subsystem):
        self.calls.append(f"resolve_mapping:{host_name}-{volume_name}")
        host = self.resolve_host(host_name)
        vol = self.resolve_volume(volume_name, subsystem)
        for m in self._mappings:
            if m["host_id"] == host["id"] and m["volume_id"] == vol["id"]:
                return m
        raise ValidationError("mapping not found")

    def get_connection_info(self, mapping_id):
        self.calls.append(f"get_connection_info:{mapping_id}")
        return {"driver_volume_type": "iscsi", "data": {}}

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
        # Volume gets id-4 (sub=1, pool=2, host=3, vol=4); mapping uses volume_id
        assert creates == [
            "create_subsystem:s1",
            "create_pool:p1",
            "create_host:h1",
            "create_volume:v1",
            "create_mapping:id-4",  # volume_id
        ]

    def test_idempotent_existing_resources(self):
        """When resources already exist, apply should not recreate them."""
        client = _FakeClient()
        # Pre-populate subsystem
        client._subsystems["s1"] = {"id": "pre-1", "name": "s1"}
        client._pools["p1"] = {"id": "pre-2", "name": "p1"}
        client._hosts["h1"] = {"id": "pre-3", "name": "h1"}
        client._volumes["v1"] = {"id": "pre-4", "name": "v1"}
        client._mappings = [
            {
                "id": "pre-5",
                "volume_id": "pre-4",
                "host_id": "pre-3",
                "protocol": "iscsi",
            }
        ]

        topo = _make_topo()
        actions = apply_topology(client, topo)

        creates = [c for c in client.calls if c.startswith("create_")]
        assert creates == [], "Nothing should be created when all resources exist"

    def test_strict_mode_reports_extras(self):
        """Strict mode reports live resources not in topology."""
        client = _FakeClient()
        client._subsystems["s1"] = {"id": "pre-1", "name": "s1"}
        client._pools["p1"] = {"id": "pre-2", "name": "p1"}
        client._pools["extra-pool"] = {"id": "pre-99", "name": "extra-pool"}
        client._hosts["h1"] = {"id": "pre-3", "name": "h1"}
        client._volumes["v1"] = {"id": "pre-4", "name": "v1"}
        client._mappings = [
            {
                "id": "pre-5",
                "volume_id": "pre-4",
                "host_id": "pre-3",
                "protocol": "iscsi",
            }
        ]

        topo = _make_topo()
        actions = apply_topology(client, topo, strict=True)
        strict_msgs = [a for a in actions if "STRICT" in a]
        assert any("extra-pool" in m for m in strict_msgs)


# ------------------------------------------------------------------
# Smoke test
# ------------------------------------------------------------------


class TestSmokeTest:
    def test_all_pass(self):
        client = _FakeClient()
        client._subsystems["s1"] = {"id": "1", "name": "s1"}
        client._pools["p1"] = {"id": "2", "name": "p1"}
        client._hosts["h1"] = {"id": "3", "name": "h1"}
        client._volumes["v1"] = {"id": "4", "name": "v1"}
        client._mappings = [
            {
                "id": "5",
                "volume_id": "4",
                "host_id": "3",
                "protocol": "iscsi",
            }
        ]

        topo = _make_topo()
        results = smoke_test(client, topo)
        assert all("\u2713" in r for r in results), f"Expected all checks to pass: {results}"

    def test_missing_subsystem_reported(self):
        client = _FakeClient()
        topo = _make_topo()
        results = smoke_test(client, topo)
        assert any("\u2717" in r and "s1" in r for r in results)
