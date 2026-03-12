# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for PersonalityDispatcher middleware."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from strix_gateway.middleware.personality_dispatch import PersonalityDispatcher


@dataclass
class FakeArrayInfo:
    id: str = "arr-1"
    name: str = "test-array"
    fqdn: str = "test.example.com"
    vendor: str = "generic"


def _make_scope(path: str = "/", array_info=None, personality_apps=None):
    """Build a minimal ASGI scope dict."""
    app = MagicMock()
    app.state.personality_apps = personality_apps or {}
    return {
        "type": "http",
        "path": path,
        "state": {"array": array_info} if array_info else {},
        "app": app,
    }


@pytest.mark.asyncio
class TestPersonalityDispatcher:
    async def test_non_http_delegates_to_inner(self):
        inner = AsyncMock()
        dispatcher = PersonalityDispatcher(inner)
        scope = {"type": "lifespan"}
        await dispatcher(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_healthz_always_inner(self):
        inner = AsyncMock()
        vendor_app = AsyncMock()
        dispatcher = PersonalityDispatcher(inner)
        scope = _make_scope(
            path="/healthz",
            array_info=FakeArrayInfo(vendor="hitachi"),
            personality_apps={"hitachi": vendor_app},
        )
        await dispatcher(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()
        vendor_app.assert_not_awaited()

    async def test_base_hostname_no_array(self):
        inner = AsyncMock()
        dispatcher = PersonalityDispatcher(inner)
        scope = _make_scope(path="/v1/pools")
        await dispatcher(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_vendor_vhost_dispatches_to_subapp(self):
        inner = AsyncMock()
        vendor_app = AsyncMock()
        dispatcher = PersonalityDispatcher(inner)
        scope = _make_scope(
            path="/ConfigurationManager/v1/objects/sessions",
            array_info=FakeArrayInfo(vendor="hitachi"),
            personality_apps={"hitachi": vendor_app},
        )
        await dispatcher(scope, AsyncMock(), AsyncMock())
        vendor_app.assert_awaited_once()
        inner.assert_not_awaited()

    async def test_unknown_vendor_falls_through(self):
        inner = AsyncMock()
        dispatcher = PersonalityDispatcher(inner)
        scope = _make_scope(
            path="/v1/pools",
            array_info=FakeArrayInfo(vendor="generic"),
            personality_apps={},
        )
        await dispatcher(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()
