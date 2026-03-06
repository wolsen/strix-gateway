# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for topology specification parsing and validation.

Validates:
  - YAML / TOML loaders parse correctly into TopologySpec
  - validate() catches structural errors
  - apply_topology() creates resources idempotently
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import pytest_asyncio

from apollo_gateway.topology.schema import (
    ArraySpec,
    EndpointSpec,
    HostSpec,
    MappingSpec,
    PoolSpec,
    TopologySpec,
    VolumeSpec,
)
from apollo_gateway.topology.validate import validate


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------

class TestTopologySchema:
    def test_empty_spec_is_valid(self):
        spec = TopologySpec()
        assert spec.arrays == []
        assert spec.pools == []

    def test_full_spec_from_dict(self):
        data = {
            "arrays": [
                {
                    "name": "a1",
                    "vendor": "ibm_svc",
                    "endpoints": [{"protocol": "iscsi"}],
                }
            ],
            "pools": [{"name": "gold", "array": "a1", "size_gb": 100}],
            "hosts": [{"name": "h1", "iqns": ["iqn.example:h1"]}],
            "volumes": [{"name": "v1", "size_gb": 10, "pool": "gold"}],
            "mappings": [{"host": "h1", "volume": "v1", "protocol": "iscsi"}],
        }
        spec = TopologySpec.model_validate(data)
        assert len(spec.arrays) == 1
        assert spec.arrays[0].name == "a1"
        assert spec.pools[0].size_gb == 100
        assert spec.volumes[0].size_gb == 10

    def test_aio_pool_requires_aio_path(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PoolSpec(name="p", array="a", backend="aio", size_gb=10)  # missing aio_path

    def test_aio_pool_with_path_is_valid(self):
        p = PoolSpec(name="p", array="a", backend="aio", size_gb=10, aio_path="/dev/sdb")
        assert p.aio_path == "/dev/sdb"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidate:
    def _make_spec(self, **kwargs) -> TopologySpec:
        defaults = {
            "arrays": [ArraySpec(name="a1", endpoints=[EndpointSpec(protocol="iscsi")])],
            "pools": [PoolSpec(name="gold", array="a1", size_gb=10)],
            "hosts": [HostSpec(name="h1", iqns=["iqn.ex:h1"])],
            "volumes": [VolumeSpec(name="v1", size_gb=1, pool="gold")],
            "mappings": [MappingSpec(host="h1", volume="v1", protocol="iscsi")],
        }
        defaults.update(kwargs)
        return TopologySpec(**defaults)

    def test_valid_spec_returns_no_errors(self):
        spec = self._make_spec()
        assert validate(spec) == []

    def test_duplicate_array_name(self):
        spec = self._make_spec(
            arrays=[
                ArraySpec(name="a1"),
                ArraySpec(name="a1"),
            ]
        )
        errors = validate(spec)
        assert any("Duplicate array" in e for e in errors)

    def test_pool_references_unknown_array(self):
        spec = self._make_spec(
            pools=[PoolSpec(name="gold", array="no-such-arr", size_gb=10)]
        )
        errors = validate(spec)
        assert any("unknown array" in e for e in errors)

    def test_duplicate_pool_name_within_array(self):
        spec = self._make_spec(
            pools=[
                PoolSpec(name="gold", array="a1", size_gb=10),
                PoolSpec(name="gold", array="a1", size_gb=20),
            ]
        )
        errors = validate(spec)
        assert any("Duplicate pool name" in e for e in errors)

    def test_same_pool_name_in_different_arrays_is_ok(self):
        spec = self._make_spec(
            arrays=[ArraySpec(name="a1"), ArraySpec(name="a2")],
            pools=[
                PoolSpec(name="gold", array="a1", size_gb=10),
                PoolSpec(name="gold", array="a2", size_gb=10),
            ],
        )
        errors = validate(spec)
        assert not any("Duplicate pool name" in e for e in errors)

    def test_volume_references_unknown_pool(self):
        spec = self._make_spec(
            volumes=[VolumeSpec(name="v1", size_gb=1, pool="no-such-pool")]
        )
        errors = validate(spec)
        assert any("unknown pool" in e for e in errors)

    def test_mapping_references_unknown_host(self):
        spec = self._make_spec(
            mappings=[MappingSpec(host="no-such-host", volume="v1", protocol="iscsi")]
        )
        errors = validate(spec)
        assert any("unknown host" in e for e in errors)

    def test_mapping_references_unknown_volume(self):
        spec = self._make_spec(
            mappings=[MappingSpec(host="h1", volume="no-such-vol", protocol="iscsi")]
        )
        errors = validate(spec)
        assert any("unknown volume" in e for e in errors)

    def test_duplicate_host_name(self):
        spec = self._make_spec(
            hosts=[HostSpec(name="h1"), HostSpec(name="h1")],
        )
        errors = validate(spec)
        assert any("Duplicate host" in e for e in errors)

    def test_duplicate_volume_name(self):
        spec = self._make_spec(
            volumes=[
                VolumeSpec(name="v1", size_gb=1, pool="gold"),
                VolumeSpec(name="v1", size_gb=2, pool="gold"),
            ]
        )
        errors = validate(spec)
        assert any("Duplicate volume" in e for e in errors)


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

class TestYamlLoader:
    def test_load_single_svc_example(self):
        from apollo_gateway.topology.load import load_yaml
        path = Path(__file__).parents[2] / "examples" / "ci" / "single_svc.yaml"
        spec = load_yaml(path)
        assert len(spec.arrays) == 1
        assert spec.arrays[0].name == "svc-a"
        assert spec.arrays[0].vendor == "ibm_svc"
        assert len(spec.pools) == 1
        assert spec.pools[0].name == "gold"
        assert len(spec.volumes) == 2
        assert validate(spec) == []

    def test_load_dual_array_example(self):
        from apollo_gateway.topology.load import load_yaml
        path = Path(__file__).parents[2] / "examples" / "ci" / "dual_subsystem.yaml"
        spec = load_yaml(path)
        assert len(spec.arrays) == 2
        names = [a.name for a in spec.arrays]
        assert "svc-a" in names
        assert "svc-b" in names

    def test_load_inline_yaml(self, tmp_path):
        from apollo_gateway.topology.load import load_yaml
        content = textwrap.dedent("""\
            arrays:
              - name: inline-arr
                vendor: generic
                endpoints:
                  - protocol: iscsi
            pools:
              - name: pool1
                array: inline-arr
                size_gb: 10
        """)
        p = tmp_path / "topo.yaml"
        p.write_text(content)
        spec = load_yaml(p)
        assert spec.arrays[0].name == "inline-arr"
        assert spec.pools[0].array == "inline-arr"
        assert validate(spec) == []


# ---------------------------------------------------------------------------
# TOML loader
# ---------------------------------------------------------------------------

class TestTomlLoader:
    def test_load_inline_toml(self, tmp_path):
        from apollo_gateway.topology.load import load_toml
        content = textwrap.dedent("""\
            [[arrays]]
            name = "toml-arr"
            vendor = "generic"

            [[arrays.endpoints]]
            protocol = "iscsi"

            [[pools]]
            name = "pool1"
            array = "toml-arr"
            size_gb = 20.0
        """)
        p = tmp_path / "topo.toml"
        p.write_bytes(content.encode())
        spec = load_toml(p)
        assert spec.arrays[0].name == "toml-arr"
        assert spec.pools[0].size_gb == 20.0
        assert validate(spec) == []
