# FILE: tests/unit/test_capabilities.py
"""Tests for capability profile enforcement.

Validates:
  - assert_feature_enabled raises HTTPException 422 when a feature is disabled
  - assert_protocol_allowed raises HTTPException 422 when a protocol is disabled
  - merge_profile deep-merges persona defaults with overrides
  - REST API respects protocol restrictions
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from apollo_gateway.core.capabilities import assert_feature_enabled, assert_protocol_allowed
from apollo_gateway.core.personas import (
    CapabilityProfile,
    CapabilityFeatures,
    get_persona_defaults,
    merge_profile,
)


# ---------------------------------------------------------------------------
# Unit tests: assert_feature_enabled
# ---------------------------------------------------------------------------

class TestAssertFeatureEnabled:
    def test_enabled_feature_does_not_raise(self):
        profile = CapabilityProfile(
            features=CapabilityFeatures(snapshots=True)
        )
        assert_feature_enabled(profile, "snapshots", "Snapshot")  # no exception

    def test_disabled_feature_raises_422(self):
        profile = CapabilityProfile(
            features=CapabilityFeatures(snapshots=False)
        )
        with pytest.raises(HTTPException) as exc_info:
            assert_feature_enabled(profile, "snapshots", "Snapshot")
        assert exc_info.value.status_code == 422
        assert "snapshots" in exc_info.value.detail

    def test_unknown_feature_defaults_to_enabled(self):
        profile = CapabilityProfile()
        # Unknown feature → getattr returns True (default) → no exception
        assert_feature_enabled(profile, "nonexistent_feature", "Widget")


# ---------------------------------------------------------------------------
# Unit tests: assert_protocol_allowed
# ---------------------------------------------------------------------------

class TestAssertProtocolAllowed:
    def _make_subsystem(self, protocols: list[str]):
        """Create a minimal subsystem-like object for testing."""
        class FakeSub:
            name = "test"
            protocols_enabled = json.dumps(protocols)
        return FakeSub()

    def test_allowed_protocol_does_not_raise(self):
        sub = self._make_subsystem(["iscsi"])
        assert_protocol_allowed(sub, "iscsi")  # no exception

    def test_disallowed_protocol_raises_422(self):
        sub = self._make_subsystem(["iscsi"])
        with pytest.raises(HTTPException) as exc_info:
            assert_protocol_allowed(sub, "nvmeof_tcp")
        assert exc_info.value.status_code == 422
        assert "nvmeof_tcp" in exc_info.value.detail

    def test_empty_protocols_raises_422(self):
        sub = self._make_subsystem([])
        with pytest.raises(HTTPException):
            assert_protocol_allowed(sub, "iscsi")


# ---------------------------------------------------------------------------
# Unit tests: merge_profile
# ---------------------------------------------------------------------------

class TestMergeProfile:
    def test_generic_persona_defaults(self):
        profile = get_persona_defaults("generic")
        assert profile.model == "Apollo-Generic"

    def test_ibm_svc_persona_defaults(self):
        profile = get_persona_defaults("ibm_svc")
        assert profile.model == "SVC-SAFER-FAKE-9000"
        assert profile.features.snapshots is True

    def test_unknown_persona_falls_back_to_generic(self):
        profile = get_persona_defaults("does_not_exist")
        assert profile.model == "Apollo-Generic"

    def test_merge_overrides_scalar_model(self):
        profile = merge_profile("ibm_svc", {"model": "FlashSystem-5200"})
        assert profile.model == "FlashSystem-5200"
        # Other defaults preserved
        assert profile.version == "8.6.0.0"

    def test_merge_overrides_feature_flag(self):
        profile = merge_profile("ibm_svc", {"features": {"snapshots": False}})
        assert profile.features.snapshots is False
        # Other features untouched
        assert profile.features.thin_provisioning is True

    def test_merge_with_no_overrides(self):
        profile = merge_profile("generic", None)
        assert profile.model == "Apollo-Generic"

    def test_merge_with_empty_overrides(self):
        profile = merge_profile("ibm_svc", {})
        assert profile.model == "SVC-SAFER-FAKE-9000"


# ---------------------------------------------------------------------------
# Integration tests: protocol restriction via REST API
# ---------------------------------------------------------------------------

class TestProtocolRestrictionAPI:
    async def _create_iscsi_only_subsystem(self, client):
        await client.post("/v1/subsystems", json={
            "name": "iscsi-only",
            "protocols_enabled": ["iscsi"],
        })
        pool = (await client.post("/v1/pools", json={
            "name": "pool1",
            "backend_type": "malloc",
            "size_mb": 256,
            "subsystem": "iscsi-only",
        })).json()
        host = (await client.post("/v1/hosts", json={
            "name": "host1",
            "iqn": "iqn.1993-08.org.debian:01:test",
        })).json()
        vol = (await client.post("/v1/volumes", json={
            "name": "vol1",
            "pool_id": pool["id"],
            "size_mb": 64,
        })).json()
        return pool, host, vol

    async def test_iscsi_mapping_allowed_on_iscsi_only_subsystem(self, client):
        _, host, vol = await self._create_iscsi_only_subsystem(client)
        resp = await client.post("/v1/mappings", json={
            "volume_id": vol["id"],
            "host_id": host["id"],
            "protocol": "iscsi",
        })
        assert resp.status_code == 201

    async def test_nvmeof_mapping_rejected_on_iscsi_only_subsystem(self, client):
        _, host, vol = await self._create_iscsi_only_subsystem(client)
        resp = await client.post("/v1/mappings", json={
            "volume_id": vol["id"],
            "host_id": host["id"],
            "protocol": "nvmeof_tcp",
        })
        assert resp.status_code == 422
        assert "nvmeof_tcp" in resp.json()["detail"]
