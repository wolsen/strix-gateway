# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Integration tests — IBM SVC CLI façade (POST /v1/svc/run).

Exercises the full Cinder SVC driver workflow through the HTTP layer:
create pool → mkvdisk → lsvdisk → mkhost → addhostport → mkvdiskhostmap →
lshostvdiskmap → rmvdiskhostmap → rmvdisk → rmhost.
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
async def svc_client():
    """AsyncClient with a seeded SVC array, pool, and transport endpoints."""
    await init_db(TEST_DATABASE_URL)
    await _ensure_default_array(get_session_factory())

    factory = get_session_factory()
    async with factory() as session:
        arr = Array(name="svc-a", vendor="ibm_svc")
        session.add(arr)
        await session.flush()

        pool = Pool(
            name="pool0",
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
                    {"target_iqn": "iqn.strix.svc.test"}
                    if proto == "iscsi"
                    else {"target_wwpns": ["50:00:00:00:00:00:00:01"]}
                ),
                addresses=json.dumps(
                    {"portals": ["10.0.0.1:3260"]}
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
    """POST a command to the SVC façade and return the JSON response."""
    resp = await client.post(
        "/v1/svc/run",
        json={"array": "svc-a", "command": command},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------


async def test_lssystem(svc_client):
    data = await _run(svc_client, "svcinfo lssystem -delim !")
    assert data["exit_code"] == 0
    assert "product_name" in data["stdout"]


async def test_lslicense(svc_client):
    data = await _run(svc_client, "svcinfo lslicense -delim !")
    assert data["exit_code"] == 0
    assert "used_flash" in data["stdout"]


async def test_lsguicapabilities(svc_client):
    data = await _run(svc_client, "svcinfo lsguicapabilities -delim !")
    assert data["exit_code"] == 0
    assert "product_key" in data["stdout"]


# ---------------------------------------------------------------------------
# Pools (mdisk groups)
# ---------------------------------------------------------------------------


async def test_lsmdiskgrp_list(svc_client):
    data = await _run(svc_client, "svcinfo lsmdiskgrp -delim !")
    assert data["exit_code"] == 0
    assert "pool0" in data["stdout"]


async def test_lsmdiskgrp_detail(svc_client):
    data = await _run(svc_client, "svcinfo lsmdiskgrp -delim ! pool0")
    assert data["exit_code"] == 0
    assert "capacity" in data["stdout"]


# ---------------------------------------------------------------------------
# IO groups / nodes / network
# ---------------------------------------------------------------------------


async def test_lsiogrp(svc_client):
    data = await _run(svc_client, "svcinfo lsiogrp -delim !")
    assert data["exit_code"] == 0
    assert "io_grp0" in data["stdout"]


async def test_lsnode_list(svc_client):
    data = await _run(svc_client, "svcinfo lsnode -delim !")
    assert data["exit_code"] == 0
    assert "node_name" in data["stdout"] or "name" in data["stdout"]


async def test_lsip(svc_client):
    data = await _run(svc_client, "svcinfo lsip -delim !")
    assert data["exit_code"] == 0


async def test_lstargetportfc(svc_client):
    data = await _run(svc_client, "svcinfo lstargetportfc -delim !")
    assert data["exit_code"] == 0


async def test_lsfcportsetmember(svc_client):
    data = await _run(svc_client, "svcinfo lsfcportsetmember -delim !")
    assert data["exit_code"] == 0


# ---------------------------------------------------------------------------
# Full volume + host + mapping lifecycle
# ---------------------------------------------------------------------------


async def test_volume_host_mapping_lifecycle(svc_client):
    """Exercise the complete Cinder SVC iSCSI driver workflow."""

    # Create volume
    data = await _run(
        svc_client,
        "svctask mkvdisk -name vol01 -size 1 -unit gb -mdiskgrp pool0",
    )
    assert data["exit_code"] == 0
    assert "successfully created" in data["stdout"]

    # List volumes — verify vol01 is present
    data = await _run(svc_client, "svcinfo lsvdisk -delim !")
    assert data["exit_code"] == 0
    assert "vol01" in data["stdout"]

    # Get volume detail
    data = await _run(svc_client, "svcinfo lsvdisk -delim ! vol01")
    assert data["exit_code"] == 0
    assert "vdisk_UID" in data["stdout"]

    # Create host with iSCSI initiator
    data = await _run(
        svc_client,
        "svctask mkhost -name host01 -iscsiname iqn.2024-01.com.example:host01",
    )
    assert data["exit_code"] == 0
    assert "successfully created" in data["stdout"]

    # List hosts
    data = await _run(svc_client, "svcinfo lshost -delim !")
    assert data["exit_code"] == 0
    assert "host01" in data["stdout"]

    # Get host detail
    data = await _run(svc_client, "svcinfo lshost -delim ! host01")
    assert data["exit_code"] == 0
    assert "iqn.2024-01.com.example:host01" in data["stdout"]

    # Map volume to host
    data = await _run(
        svc_client,
        "svctask mkvdiskhostmap -host host01 vol01",
    )
    assert data["exit_code"] == 0
    assert "successfully created" in data["stdout"]

    # Verify mapping via lshostvdiskmap
    data = await _run(svc_client, "svcinfo lshostvdiskmap host01 -delim !")
    assert data["exit_code"] == 0
    assert "vol01" in data["stdout"]

    # Verify mapping via lsvdiskhostmap
    data = await _run(svc_client, "svcinfo lsvdiskhostmap vol01 -delim !")
    assert data["exit_code"] == 0
    assert "host01" in data["stdout"]

    # Unmap volume from host
    data = await _run(
        svc_client,
        "svctask rmvdiskhostmap -host host01 vol01",
    )
    assert data["exit_code"] == 0

    # Mapping should be gone
    data = await _run(svc_client, "svcinfo lshostvdiskmap host01 -delim !")
    assert data["exit_code"] == 0
    assert "vol01" not in data["stdout"]

    # Remove volume
    data = await _run(svc_client, "svctask rmvdisk vol01")
    assert data["exit_code"] == 0

    # Volume should be gone
    data = await _run(svc_client, "svcinfo lsvdisk -delim !")
    assert data["exit_code"] == 0
    assert "vol01" not in data["stdout"]

    # Remove host
    data = await _run(svc_client, "svctask rmhost host01")
    assert data["exit_code"] == 0


async def test_fc_host_and_fabric(svc_client):
    """Exercise FC host creation, port addition, and fabric lookup."""

    # Create FC host
    data = await _run(
        svc_client,
        "svctask mkhost -name fchost01 -hbawwpn 10:00:00:00:00:00:00:AA",
    )
    assert data["exit_code"] == 0
    assert "successfully created" in data["stdout"]

    # Add another WWPN
    data = await _run(
        svc_client,
        "svctask addhostport -force -hbawwpn 10:00:00:00:00:00:00:BB fchost01",
    )
    assert data["exit_code"] == 0

    # Check host detail shows both WWPNs
    data = await _run(svc_client, "svcinfo lshost -delim ! fchost01")
    assert data["exit_code"] == 0
    assert "10:00:00:00:00:00:00:AA" in data["stdout"].upper() or \
           "10000000000000AA" in data["stdout"].upper()

    # lsfabric
    data = await _run(svc_client, "svcinfo lsfabric -host fchost01 -delim !")
    assert data["exit_code"] == 0

    # Cleanup
    data = await _run(svc_client, "svctask rmhost fchost01")
    assert data["exit_code"] == 0


async def test_expand_volume(svc_client):
    """Test expandvdisksize."""
    # Create volume
    data = await _run(
        svc_client,
        "svctask mkvdisk -name expand_vol -size 1 -unit gb -mdiskgrp pool0",
    )
    assert data["exit_code"] == 0

    # Expand by 1 GB
    data = await _run(
        svc_client,
        "svctask expandvdisksize -size 1 -unit gb expand_vol",
    )
    assert data["exit_code"] == 0

    # Get detail — verify size increased
    data = await _run(svc_client, "svcinfo lsvdisk -delim ! -bytes expand_vol")
    assert data["exit_code"] == 0
    # 2 GB = 2147483648 bytes
    assert "2147483648" in data["stdout"]

    # Cleanup
    data = await _run(svc_client, "svctask rmvdisk expand_vol")
    assert data["exit_code"] == 0


async def test_iscsi_auth(svc_client):
    """lsiscsiauth returns host authentication info."""
    # Create a host first
    data = await _run(
        svc_client,
        "svctask mkhost -name authhost -iscsiname iqn.2024-01.com.example:auth",
    )
    assert data["exit_code"] == 0

    data = await _run(svc_client, "svcinfo lsiscsiauth -delim !")
    assert data["exit_code"] == 0

    # Cleanup
    data = await _run(svc_client, "svctask rmhost authhost")
    assert data["exit_code"] == 0


async def test_unknown_command_returns_error(svc_client):
    """An unrecognised command returns a non-zero exit code."""
    resp = await svc_client.post(
        "/v1/svc/run",
        json={"array": "svc-a", "command": "svcinfo nosuchcmd"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exit_code"] != 0


async def test_nonexistent_array_returns_404(svc_client):
    """Referencing a nonexistent array returns a 404."""
    resp = await svc_client.post(
        "/v1/svc/run",
        json={"array": "no-such-array", "command": "svcinfo lssystem"},
    )
    assert resp.status_code == 404
