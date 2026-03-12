# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for vendor_metadata column behaviour.

Tests run against in-memory SQLite via async session.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from strix_gateway.core.db import Array, Base, Host, Pool, TransportEndpoint, Volume


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess

    await engine.dispose()


@pytest.mark.asyncio
class TestVendorMetadataDefaults:
    async def test_volume_default_empty(self, session):
        arr = Array(name="a1", vendor="generic")
        session.add(arr)
        await session.flush()

        pool = Pool(name="p1", array_id=arr.id, backend_type="malloc", size_mb=1024)
        session.add(pool)
        await session.flush()

        vol = Volume(name="v1", array_id=arr.id, pool_id=pool.id, size_mb=100)
        session.add(vol)
        await session.flush()

        assert vol.vendor_metadata == "{}"
        assert vol.vendor_meta_dict == {}

    async def test_pool_default_empty(self, session):
        arr = Array(name="a2", vendor="generic")
        session.add(arr)
        await session.flush()

        pool = Pool(name="p2", array_id=arr.id, backend_type="malloc", size_mb=1024)
        session.add(pool)
        await session.flush()

        assert pool.vendor_metadata == "{}"
        assert pool.vendor_meta_dict == {}

    async def test_host_default_empty(self, session):
        host = Host(name="h1")
        session.add(host)
        await session.flush()

        assert host.vendor_metadata == "{}"
        assert host.vendor_meta_dict == {}

    async def test_endpoint_default_empty(self, session):
        arr = Array(name="a3", vendor="generic")
        session.add(arr)
        await session.flush()

        ep = TransportEndpoint(array_id=arr.id, protocol="iscsi")
        session.add(ep)
        await session.flush()

        assert ep.vendor_metadata == "{}"
        assert ep.vendor_meta_dict == {}


@pytest.mark.asyncio
class TestVendorMetadataRoundTrip:
    async def test_volume_metadata_roundtrip(self, session):
        arr = Array(name="a4", vendor="hitachi")
        session.add(arr)
        await session.flush()

        pool = Pool(name="p4", array_id=arr.id, backend_type="malloc", size_mb=1024)
        session.add(pool)
        await session.flush()

        meta = {"ldev_id": 42, "extra": "data"}
        vol = Volume(
            name="v4", array_id=arr.id, pool_id=pool.id,
            size_mb=100, vendor_metadata=json.dumps(meta),
        )
        session.add(vol)
        await session.flush()

        assert vol.vendor_meta_dict == meta
        assert vol.vendor_meta_dict["ldev_id"] == 42

    async def test_host_metadata_roundtrip(self, session):
        meta = {"hitachi_host_groups": {"CL1-A": 1}}
        host = Host(name="h2", vendor_metadata=json.dumps(meta))
        session.add(host)
        await session.flush()

        assert host.vendor_meta_dict == meta

    async def test_update_vendor_metadata_merges(self, session):
        arr = Array(name="a5", vendor="hitachi")
        session.add(arr)
        await session.flush()

        pool = Pool(name="p5", array_id=arr.id, backend_type="malloc", size_mb=1024)
        session.add(pool)
        await session.flush()

        vol = Volume(
            name="v5", array_id=arr.id, pool_id=pool.id,
            size_mb=100, vendor_metadata=json.dumps({"ldev_id": 10}),
        )
        session.add(vol)
        await session.flush()

        # Simulate update_vendor_metadata merge
        current = vol.vendor_meta_dict
        current["new_key"] = "new_value"
        vol.vendor_metadata = json.dumps(current)
        await session.flush()

        assert vol.vendor_meta_dict["ldev_id"] == 10
        assert vol.vendor_meta_dict["new_key"] == "new_value"
