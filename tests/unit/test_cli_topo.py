# FILE: tests/unit/test_cli_topo.py
"""Tests for CLI topology parsing and validation."""

from __future__ import annotations

import tempfile
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
                    "subsystems": [{"name": "s1", "protocols": ["iscsi"]}],
                    "pools": [
                        {
                            "name": "p1",
                            "subsystem": "s1",
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
        assert len(topo.subsystems) == 1
        assert topo.subsystems[0].name == "s1"
        assert len(topo.pools) == 1
        assert len(topo.volumes) == 1
        assert len(topo.mappings) == 1

    def test_empty_sections(self, tmp_path: Path):
        f = tmp_path / "empty.yaml"
        f.write_text(yaml.dump({"subsystems": []}))
        topo = load_topology(str(f))
        assert topo.subsystems == []

    def test_host_normalises_iqns(self, tmp_path: Path):
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
        assert "iscsi" in topo.hosts[0].initiators
        assert topo.hosts[0].initiators["iscsi"] == ["iqn.a", "iqn.b"]

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


# ------------------------------------------------------------------
# TOML parsing
# ------------------------------------------------------------------


class TestTOMLParsing:
    def test_minimal_toml(self, tmp_path: Path):
        content = textwrap.dedent("""\
            [[subsystems]]
            name = "s1"
            protocols = ["iscsi"]

            [[pools]]
            name = "p1"
            subsystem = "s1"
            backend = "malloc"
            size_gb = 10.0
        """)
        f = tmp_path / "topo.toml"
        f.write_text(content)
        topo = load_topology(str(f))
        assert len(topo.subsystems) == 1
        assert topo.subsystems[0].name == "s1"
        assert len(topo.pools) == 1

    def test_capability_profile_toml(self, tmp_path: Path):
        content = textwrap.dedent("""\
            [[subsystems]]
            name = "svc"
            persona = "ibm_svc"
            protocols = ["iscsi"]

            [subsystems.capability_profile]
            model = "FlashSystem"

            [subsystems.capability_profile.features]
            thin_provisioning = true
        """)
        f = tmp_path / "cap.toml"
        f.write_text(content)
        topo = load_topology(str(f))
        assert topo.subsystems[0].capability_profile is not None
        assert topo.subsystems[0].capability_profile.model == "FlashSystem"


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
            "subsystems": [{"name": "s1", "protocols": ["iscsi"]}],
            "pools": [
                {
                    "name": "p1",
                    "subsystem": "s1",
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

    def test_duplicate_subsystem(self):
        topo = self._base_topo(
            subsystems=[
                {"name": "dup", "protocols": ["iscsi"]},
                {"name": "dup", "protocols": ["iscsi"]},
            ],
            pools=[
                {
                    "name": "p1",
                    "subsystem": "dup",
                    "backend": "malloc",
                    "size_gb": 10,
                }
            ],
        )
        errors = validate_topology(topo)
        assert any("Duplicate subsystem" in e for e in errors)

    def test_pool_references_unknown_subsystem(self):
        topo = self._base_topo(
            pools=[
                {
                    "name": "orphan",
                    "subsystem": "no-such",
                    "backend": "malloc",
                    "size_gb": 10,
                }
            ]
        )
        errors = validate_topology(topo)
        assert any("unknown subsystem" in e for e in errors)

    def test_volume_references_unknown_pool(self):
        topo = self._base_topo(
            volumes=[{"name": "v-bad", "size_gb": 5, "pool": "no-such"}]
        )
        errors = validate_topology(topo)
        assert any("unknown pool" in e for e in errors)

    def test_mapping_bad_protocol(self):
        topo = self._base_topo(
            mappings=[
                {"host": "h1", "volume": "v1", "protocol": "nvmeof_tcp"}
            ]
        )
        errors = validate_topology(topo)
        assert any("not enabled" in e for e in errors)

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

    def test_thin_provisioning_constraint(self):
        topo = self._base_topo(
            subsystems=[
                {
                    "name": "s1",
                    "protocols": ["iscsi"],
                    "capability_profile": {
                        "features": {"thin_provisioning": False}
                    },
                }
            ],
            volumes=[{"name": "v1", "size_gb": 5, "pool": "p1", "thin": True}],
        )
        errors = validate_topology(topo)
        assert any("thin provisioning" in e.lower() for e in errors)

    def test_invalid_protocol_in_subsystem(self):
        topo = self._base_topo(
            subsystems=[{"name": "s1", "protocols": ["foobar"]}]
        )
        errors = validate_topology(topo)
        assert any("unknown protocol" in e for e in errors)

    def test_duplicate_pool_in_subsystem(self):
        topo = self._base_topo(
            pools=[
                {
                    "name": "dup",
                    "subsystem": "s1",
                    "backend": "malloc",
                    "size_gb": 10,
                },
                {
                    "name": "dup",
                    "subsystem": "s1",
                    "backend": "malloc",
                    "size_gb": 20,
                },
            ]
        )
        errors = validate_topology(topo)
        assert any("Duplicate pool" in e for e in errors)
