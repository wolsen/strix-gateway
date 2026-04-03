# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Integration tests — HPE 3PAR CLI façade (POST /v1/3par/run).

Exercises the full Cinder 3PAR driver CLI workflow through the HTTP layer:
showsys → createvv → showvv → createhost → createvlun → showvlun →
removevlun → removevv → removehost.
Uses in-memory SQLite + mocked SPDK.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import MagicMock

from strix_gateway.core.db import (
    Array,
    Pool,
    TransportEndpoint,
    get_session_factory,
    init_db,
)
from strix_gateway.main import app, _ensure_default_array

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def threepar_client():
    """AsyncClient with a seeded HPE 3PAR array, CPG pool, and endpoints."""
    await init_db(TEST_DATABASE_URL)
    await _ensure_default_array(get_session_factory())

    factory = get_session_factory()
    async with factory() as session:
        arr = Array(name="3par-a", vendor="hpe_3par")
        session.add(arr)
        await session.flush()

        pool = Pool(
            name="cpg0",
            array_id=arr.id,
            backend_type="malloc",
            size_mb=8192,
        )
        session.add(pool)

        for proto in ("iscsi", "fc"):
            ep = TransportEndpoint(
                array_id=arr.id,
                protocol=proto,
                targets=json.dumps(
                    {"target_iqn": "iqn.strix.3par.test"}
                    if proto == "iscsi"
                    else {"target_wwpns": ["50:00:00:00:00:00:00:02"]}
                ),
                addresses=json.dumps(
                    {"portals": ["10.0.0.2:3260"]}
                    if proto == "iscsi"
                    else {}
                ),
            )
            session.add(ep)

        await session.commit()

    mock_spdk = MagicMock()
    mock_spdk.call = MagicMock(return_value=None)
    app.state.spdk_client = mock_spdk

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


pytestmark = pytest.mark.asyncio


async def _run(client: AsyncClient, command: str) -> dict:
    """POST a command to the 3PAR CLI façade and return the JSON response."""
    resp = await client.post(
        "/v1/3par/run",
        json={"array": "3par-a", "command": command},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------


async def test_showsys(threepar_client):
    data = await _run(threepar_client, "showsys")
    assert data["exit_code"] == 0
    assert "System Name" in data["stdout"]


async def test_showcpg_list(threepar_client):
    data = await _run(threepar_client, "showcpg")
    assert data["exit_code"] == 0
    assert "cpg0" in data["stdout"]


async def test_showcpg_detail(threepar_client):
    data = await _run(threepar_client, "showcpg cpg0")
    assert data["exit_code"] == 0
    assert "cpg0" in data["stdout"]


async def test_showport(threepar_client):
    data = await _run(threepar_client, "showport")
    assert data["exit_code"] == 0


async def test_showport_iscsi(threepar_client):
    data = await _run(threepar_client, "showport -type iscsi")
    assert data["exit_code"] == 0


async def test_showport_fc(threepar_client):
    data = await _run(threepar_client, "showport -type fc")
    assert data["exit_code"] == 0


# ---------------------------------------------------------------------------
# Full volume + host + VLUN lifecycle
# ---------------------------------------------------------------------------


async def test_volume_host_vlun_lifecycle(threepar_client):
    """Exercise the complete Cinder 3PAR iSCSI driver CLI workflow."""

    # Create volume — 1024 MiB in cpg0
    data = await _run(threepar_client, "createvv -tpvv vol01 cpg0 1024")
    assert data["exit_code"] == 0

    # List volumes
    data = await _run(threepar_client, "showvv")
    assert data["exit_code"] == 0
    assert "vol01" in data["stdout"]

    # Get volume detail
    data = await _run(threepar_client, "showvv vol01")
    assert data["exit_code"] == 0
    assert "tpvv" in data["stdout"].lower()

    # Create host with iSCSI initiator
    data = await _run(
        threepar_client,
        "createhost host01 iqn.2024-01.com.example:host01",
    )
    assert data["exit_code"] == 0

    # List hosts
    data = await _run(threepar_client, "showhost")
    assert data["exit_code"] == 0
    assert "host01" in data["stdout"]

    # Get host detail
    data = await _run(threepar_client, "showhost host01")
    assert data["exit_code"] == 0

    # Create VLUN mapping
    data = await _run(threepar_client, "createvlun vol01 0 host01")
    assert data["exit_code"] == 0

    # Verify mapping via showvlun
    data = await _run(threepar_client, "showvlun")
    assert data["exit_code"] == 0
    assert "vol01" in data["stdout"]
    assert "host01" in data["stdout"]

    # showvlun filtered by host
    data = await _run(threepar_client, "showvlun -host host01")
    assert data["exit_code"] == 0
    assert "vol01" in data["stdout"]

    # Remove VLUN
    data = await _run(threepar_client, "removevlun -f vol01 0 host01")
    assert data["exit_code"] == 0

    # Mapping should be gone
    data = await _run(threepar_client, "showvlun")
    assert data["exit_code"] == 0
    assert "host01" not in data["stdout"]

    # Remove volume
    data = await _run(threepar_client, "removevv -f vol01")
    assert data["exit_code"] == 0

    # Volume should be gone
    data = await _run(threepar_client, "showvv")
    assert data["exit_code"] == 0
    assert "vol01" not in data["stdout"]

    # Remove host
    data = await _run(threepar_client, "removehost host01")
    assert data["exit_code"] == 0


async def test_fc_host_workflow(threepar_client):
    """Exercise FC host lifecycle via CLI."""

    # Create FC host with WWPNs
    data = await _run(
        threepar_client,
        "createhost fchost01 10:00:00:00:00:00:00:CC 10:00:00:00:00:00:00:DD",
    )
    assert data["exit_code"] == 0

    # Verify host has FC paths
    data = await _run(threepar_client, "showhost fchost01")
    assert data["exit_code"] == 0

    # Add another FC port
    data = await _run(
        threepar_client,
        "sethost -add 10:00:00:00:00:00:00:EE fchost01",
    )
    assert data["exit_code"] == 0

    # Cleanup
    data = await _run(threepar_client, "removehost fchost01")
    assert data["exit_code"] == 0


async def test_grow_volume(threepar_client):
    """Test growvv (expand-by semantics)."""
    # Create volume
    data = await _run(threepar_client, "createvv -tpvv grow_vol cpg0 1024")
    assert data["exit_code"] == 0

    # Grow by 512 MiB
    data = await _run(threepar_client, "growvv grow_vol 512")
    assert data["exit_code"] == 0

    # Verify new size (1024 + 512 = 1536 MiB)
    data = await _run(threepar_client, "showvv grow_vol")
    assert data["exit_code"] == 0
    assert "1536" in data["stdout"]

    # Cleanup
    data = await _run(threepar_client, "removevv -f grow_vol")
    assert data["exit_code"] == 0


async def test_unknown_command_returns_error(threepar_client):
    """An unrecognised command returns a non-zero exit code."""
    resp = await threepar_client.post(
        "/v1/3par/run",
        json={"array": "3par-a", "command": "nosuchcmd"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exit_code"] != 0


async def test_nonexistent_array_returns_404(threepar_client):
    """Referencing a nonexistent array returns a 404."""
    resp = await threepar_client.post(
        "/v1/3par/run",
        json={"array": "no-such-array", "command": "showsys"},
    )
    assert resp.status_code == 404
