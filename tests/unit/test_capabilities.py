# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for capability profile enforcement.

Validates:
  - assert_feature_enabled raises HTTPException 422 when a feature is disabled
  - merge_profile deep-merges persona defaults with overrides
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from strix_gateway.core.capabilities import assert_feature_enabled
from strix_gateway.core.personas import (
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
