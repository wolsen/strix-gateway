# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for CLI topology parsing and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from apollo_gateway.cli.errors import ValidationError
from apollo_gateway.cli.topo.load import load_topology
from apollo_gateway.cli.topo.models import TopologyFile
from apollo_gateway.cli.topo.validate import validate_topology


# ------------------------------------------------------------------
# YAML parsing
# ------------------------------------------------------------------


class TestYAMLParsing:
    def test_minimal_yaml(self, tmp_path: Path):
        f = tmp_path / "topo.yaml"
        f.write_text(
            yaml.dump(
                {
                    "arrays": [
                        {
                            "name": "a1",
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
                    "hosts": [{"name": "h1", "iqns": ["iqn.example:01"]}],
                    "volumes": [{"name": "v1", "size_gb": 5, "pool": "p1"}],
                    "mappings": [
                        {"host": "h1", "volume": "v1", "protocol": "iscsi"}
                    ],
                }
            )
        )
        topo = load_topology(str(f))
        assert len(topo.arrays) == 1
        assert topo.arrays[0].name == "a1"
        assert len(topo.pools) == 1
        assert len(topo.volumes) == 1
        assert len(topo.mappings) == 1

    def test_empty_sections(self, tmp_path: Path):
        f = tmp_path / "empty.yaml"
        f.write_text(yaml.dump({"arrays": []}))
        topo = load_topology(str(f))
        assert topo.arrays == []

    def test_host_iqns(self, tmp_path: Path):
        f = tmp_path / "hosts.yaml"
        f.write_text(
            yaml.dump(
                {
                    "hosts": [
                        {"name": "h1", "iqns": ["iqn.a", "iqn.b"]},
                    ]
                }
            )
        )
        topo = load_topology(str(f))
        assert topo.hosts[0].iqns == ["iqn.a", "iqn.b"]

    def test_host_nqns(self, tmp_path: Path):
        f = tmp_path / "hosts.yaml"
        f.write_text(
            yaml.dump(
                {
                    "hosts": [
                        {"name": "h1", "nqns": ["nqn.example:01"]},
                    ]
                }
            )
        )
        topo = load_topology(str(f))
        assert topo.hosts[0].nqns == ["nqn.example:01"]

    def test_host_wwpns(self, tmp_path: Path):
        f = tmp_path / "hosts.yaml"
        f.write_text(
            yaml.dump(
                {
                    "hosts": [
                        {
                            "name": "h1",
                            "wwpns": ["50:00:00:00:00:00:00:01"],
                        },
                    ]
                }
            )
        )
        topo = load_topology(str(f))
        assert topo.hosts[0].wwpns == ["50:00:00:00:00:00:00:01"]

    def test_faults_and_delays(self, tmp_path: Path):
        f = tmp_path / "faults.yaml"
        f.write_text(
            yaml.dump(
                {
                    "faults": [
                        {"operation": "create_pool", "error_message": "boom"}
                    ],
                    "delays": [
                        {"operation": "create_volume", "delay_seconds": 2.5}
                    ],
                }
            )
        )
        topo = load_topology(str(f))
        assert len(topo.faults) == 1
        assert topo.faults[0].operation == "create_pool"
        assert len(topo.delays) == 1
        assert topo.delays[0].delay_seconds == 2.5

    def test_array_with_multiple_endpoints(self, tmp_path: Path):
        f = tmp_path / "multi.yaml"
        f.write_text(
            yaml.dump(
                {
                    "arrays": [
                        {
                            "name": "a1",
                            "vendor": "acme",
                            "endpoints": [
                                {"protocol": "iscsi"},
                                {"protocol": "nvmeof_tcp"},
                                {"protocol": "fc"},
                            ],
                        }
                    ],
                }
            )
        )
        topo = load_topology(str(f))
        assert len(topo.arrays[0].endpoints) == 3
        assert topo.arrays[0].vendor == "acme"
        protos = [ep.protocol for ep in topo.arrays[0].endpoints]
        assert protos == ["iscsi", "nvmeof_tcp", "fc"]

    def test_endpoint_with_targets_and_addresses(self, tmp_path: Path):
        f = tmp_path / "ep.yaml"
        f.write_text(
            yaml.dump(
                {
                    "arrays": [
                        {
                            "name": "a1",
                            "endpoints": [
                                {
                                    "protocol": "iscsi",
                                    "targets": {"iqn": "iqn.target:01"},
                                    "addresses": {"ip": "10.0.0.1", "port": 3260},
                                    "auth": {"method": "chap", "user": "u"},
                                }
                            ],
                        }
                    ],
                }
            )
        )
        topo = load_topology(str(f))
        ep = topo.arrays[0].endpoints[0]
        assert ep.targets == {"iqn": "iqn.target:01"}
        assert ep.addresses == {"ip": "10.0.0.1", "port": 3260}
        assert ep.auth == {"method": "chap", "user": "u"}


# ------------------------------------------------------------------
# TOML parsing
# ------------------------------------------------------------------


class TestTOMLParsing:
    def test_minimal_toml(self, tmp_path: Path):
        content = textwrap.dedent("""\
            [[arrays]]
            name = "a1"

            [[arrays.endpoints]]
            protocol = "iscsi"

            [[pools]]
            name = "p1"
            array = "a1"
            backend = "malloc"
            size_gb = 10.0
        """)
        f = tmp_path / "topo.toml"
        f.write_text(content)
        topo = load_topology(str(f))
        assert len(topo.arrays) == 1
        assert topo.arrays[0].name == "a1"
        assert len(topo.pools) == 1

    def test_array_profile_toml(self, tmp_path: Path):
        content = textwrap.dedent("""\
            [[arrays]]
            name = "a1"
            vendor = "ibm"

            [arrays.profile]
            model = "FlashSystem"
            thin_provisioning = true

            [[arrays.endpoints]]
            protocol = "iscsi"
        """)
        f = tmp_path / "prof.toml"
        f.write_text(content)
        topo = load_topology(str(f))
        assert topo.arrays[0].profile["model"] == "FlashSystem"
        assert topo.arrays[0].profile["thin_provisioning"] is True
        assert topo.arrays[0].vendor == "ibm"

    def test_array_fc_endpoint_toml(self, tmp_path: Path):
        content = textwrap.dedent("""\
            [[arrays]]
            name = "fc-array"

            [[arrays.endpoints]]
            protocol = "fc"

            [arrays.endpoints.targets]
            wwpn = "50:00:00:00:00:00:00:AA"
        """)
        f = tmp_path / "fc.toml"
        f.write_text(content)
        topo = load_topology(str(f))
        assert topo.arrays[0].endpoints[0].protocol == "fc"
        assert topo.arrays[0].endpoints[0].targets["wwpn"] == "50:00:00:00:00:00:00:AA"


# ------------------------------------------------------------------
# File-not-found / bad format
# ------------------------------------------------------------------


class TestLoadErrors:
    def test_file_not_found(self):
        with pytest.raises(ValidationError, match="File not found"):
            load_topology("/nonexistent/path.yaml")

    def test_unsupported_extension(self, tmp_path: Path):
        f = tmp_path / "topo.xml"
        f.write_text("<nope/>")
        with pytest.raises(ValidationError, match="Unsupported file format"):
            load_topology(str(f))

    def test_bad_yaml_content(self, tmp_path: Path):
        f = tmp_path / "bad.yaml"
        f.write_text("just a string")
        with pytest.raises(ValidationError, match="must be a YAML/TOML mapping"):
            load_topology(str(f))


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


class TestValidation:
    def _base_topo(self, **overrides) -> TopologyFile:
        data = {
            "arrays": [
                {
                    "name": "a1",
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
            "hosts": [{"name": "h1"}],
            "volumes": [{"name": "v1", "size_gb": 5, "pool": "p1"}],
            "mappings": [{"host": "h1", "volume": "v1", "protocol": "iscsi"}],
        }
        data.update(overrides)
        return TopologyFile.model_validate(data)

    def test_valid_topology(self):
        topo = self._base_topo()
        errors = validate_topology(topo)
        assert errors == []

    def test_duplicate_array(self):
        topo = self._base_topo(
            arrays=[
                {"name": "dup", "endpoints": [{"protocol": "iscsi"}]},
                {"name": "dup", "endpoints": [{"protocol": "iscsi"}]},
            ],
            pools=[
                {
                    "name": "p1",
                    "array": "dup",
                    "backend": "malloc",
                    "size_gb": 10,
                }
            ],
        )
        errors = validate_topology(topo)
        assert any("Duplicate array" in e for e in errors)

    def test_pool_references_unknown_array(self):
        topo = self._base_topo(
            pools=[
                {
                    "name": "orphan",
                    "array": "no-such",
                    "backend": "malloc",
                    "size_gb": 10,
                }
            ]
        )
        errors = validate_topology(topo)
        assert any("unknown array" in e for e in errors)

    def test_volume_references_unknown_pool(self):
        topo = self._base_topo(
            volumes=[{"name": "v-bad", "size_gb": 5, "pool": "no-such"}]
        )
        errors = validate_topology(topo)
        assert any("unknown pool" in e for e in errors)

    def test_mapping_unknown_host(self):
        topo = self._base_topo(
            mappings=[
                {"host": "ghost", "volume": "v1", "protocol": "iscsi"}
            ]
        )
        errors = validate_topology(topo)
        assert any("unknown host" in e for e in errors)

    def test_mapping_unknown_volume(self):
        topo = self._base_topo(
            mappings=[
                {"host": "h1", "volume": "no-vol", "protocol": "iscsi"}
            ]
        )
        errors = validate_topology(topo)
        assert any("unknown volume" in e for e in errors)

    def test_invalid_endpoint_protocol(self):
        topo = self._base_topo(
            arrays=[
                {
                    "name": "a1",
                    "endpoints": [{"protocol": "foobar"}],
                }
            ]
        )
        errors = validate_topology(topo)
        assert any("unknown endpoint protocol" in e for e in errors)

    def test_fc_endpoint_protocol_valid(self):
        topo = self._base_topo(
            arrays=[
                {
                    "name": "a1",
                    "endpoints": [{"protocol": "fc"}],
                }
            ]
        )
        errors = validate_topology(topo)
        assert not any("unknown" in e for e in errors)

    def test_nvmeof_tcp_endpoint_protocol_valid(self):
        topo = self._base_topo(
            arrays=[
                {
                    "name": "a1",
                    "endpoints": [{"protocol": "nvmeof_tcp"}],
                }
            ]
        )
        errors = validate_topology(topo)
        assert not any("unknown" in e for e in errors)

    def test_duplicate_pool_in_array(self):
        topo = self._base_topo(
            pools=[
                {
                    "name": "dup",
                    "array": "a1",
                    "backend": "malloc",
                    "size_gb": 10,
                },
                {
                    "name": "dup",
                    "array": "a1",
                    "backend": "malloc",
                    "size_gb": 20,
                },
            ]
        )
        errors = validate_topology(topo)
        assert any("Duplicate pool" in e for e in errors)

    def test_array_with_no_endpoints_is_valid(self):
        topo = self._base_topo(
            arrays=[{"name": "a1"}]
        )
        errors = validate_topology(topo)
        assert errors == []

    def test_multiple_arrays_unique(self):
        topo = self._base_topo(
            arrays=[
                {"name": "a1", "endpoints": [{"protocol": "iscsi"}]},
                {"name": "a2", "endpoints": [{"protocol": "fc"}]},
            ],
            pools=[
                {
                    "name": "p1",
                    "array": "a1",
                    "backend": "malloc",
                    "size_gb": 10,
                }
            ],
        )
        errors = validate_topology(topo)
        assert errors == []
