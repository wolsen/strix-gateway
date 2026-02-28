# FILE: tests/conftest.py
"""Shared pytest fixtures for Apollo Gateway tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apollo_gateway.core.db import Base, init_db, get_session_factory
from apollo_gateway.main import app, _ensure_default_subsystem

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def mock_spdk():
    """A MagicMock SPDKClient whose .call() returns {} by default."""
    client = MagicMock()
    client.call = MagicMock(return_value=None)
    return client


@pytest_asyncio.fixture
async def client(mock_spdk):
    """An httpx AsyncClient wired to the FastAPI app with in-memory DB."""
    # Re-initialise with an in-memory DB for each test
    await init_db(TEST_DATABASE_URL)
    await _ensure_default_subsystem(get_session_factory())

    app.state.spdk_client = mock_spdk

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
