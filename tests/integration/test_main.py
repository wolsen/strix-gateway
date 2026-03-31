# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for main.py: healthz endpoint and lifespan."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from strix_gateway.main import app, lifespan
from strix_gateway.spdk.rpc import SPDKClient

pytestmark = pytest.mark.asyncio


def _mock_session_factory():
    """Return a callable that produces an async-context-manager session.

    The mock session's ``.execute()`` returns an object whose
    ``.scalars().all()`` yields an empty list so the Hitachi mapper
    bootstrap loop simply becomes a no-op.
    """
    scalars_result = MagicMock()
    scalars_result.all.return_value = []

    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result

    mock_session = AsyncMock()
    mock_session.execute.return_value = execute_result

    @asynccontextmanager
    async def _factory():
        yield mock_session

    return _factory


async def test_healthz(client: AsyncClient):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_lifespan_sets_spdk_client_on_app_state():
    """Lifespan should initialise the DB, set spdk_client, and run reconcile."""
    with patch("strix_gateway.main.init_db", new_callable=AsyncMock) as mock_init, \
         patch("strix_gateway.main.reconcile", new_callable=AsyncMock) as mock_reconcile, \
         patch("strix_gateway.main.SPDKClient") as mock_cls, \
         patch("strix_gateway.main._ensure_default_array", new_callable=AsyncMock), \
         patch("strix_gateway.main.get_session_factory", return_value=_mock_session_factory()):

        mock_client = MagicMock(spec=SPDKClient)
        mock_cls.return_value = mock_client

        async with lifespan(app):
            assert app.state.spdk_client is mock_client

    mock_init.assert_awaited_once()
    mock_reconcile.assert_awaited_once()


async def test_lifespan_reconcile_failure_is_non_fatal():
    """A reconcile exception should be swallowed and startup should proceed."""
    with patch("strix_gateway.main.init_db", new_callable=AsyncMock), \
         patch("strix_gateway.main.reconcile", new_callable=AsyncMock,
               side_effect=Exception("SPDK unavailable")), \
         patch("strix_gateway.main.SPDKClient"), \
         patch("strix_gateway.main._ensure_default_array", new_callable=AsyncMock), \
         patch("strix_gateway.main.get_session_factory", return_value=_mock_session_factory()):
        # Should not raise
        async with lifespan(app):
            pass
