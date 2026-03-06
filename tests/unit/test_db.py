# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for db module edge cases."""

from __future__ import annotations

import pytest

import apollo_gateway.core.db as db_module


def test_get_session_factory_before_init_raises():
    # Temporarily clear the factory to simulate uninitialized state
    original = db_module._session_factory
    db_module._session_factory = None
    try:
        with pytest.raises(RuntimeError, match="not initialised"):
            db_module.get_session_factory()
    finally:
        db_module._session_factory = original
