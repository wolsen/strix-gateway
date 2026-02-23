# FILE: tests/integration/test_main.py
"""Tests for main.py: healthz endpoint and lifespan."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from apollo_gateway.main import app, lifespan
from apollo_gateway.spdk.rpc import SPDKClient

pytestmark = pytest.mark.asyncio


async def test_healthz(client: AsyncClient):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_lifespan_sets_spdk_client_on_app_state():
    """Lifespan should initialise the DB, set spdk_client, and run reconcile."""
    with patch("apollo_gateway.main.init_db", new_callable=AsyncMock) as mock_init, \
         patch("apollo_gateway.main.reconcile", new_callable=AsyncMock) as mock_reconcile, \
         patch("apollo_gateway.main.SPDKClient") as mock_cls, \
         patch("apollo_gateway.main.get_session_factory", return_value=MagicMock()):

        mock_client = MagicMock(spec=SPDKClient)
        mock_cls.return_value = mock_client

        async with lifespan(app):
            assert app.state.spdk_client is mock_client

    mock_init.assert_awaited_once()
    mock_reconcile.assert_awaited_once()


async def test_lifespan_reconcile_failure_is_non_fatal():
    """A reconcile exception should be swallowed and startup should proceed."""
    with patch("apollo_gateway.main.init_db", new_callable=AsyncMock), \
         patch("apollo_gateway.main.reconcile", new_callable=AsyncMock,
               side_effect=Exception("SPDK unavailable")), \
         patch("apollo_gateway.main.SPDKClient"), \
         patch("apollo_gateway.main.get_session_factory", return_value=MagicMock()):
        # Should not raise
        async with lifespan(app):
            pass
