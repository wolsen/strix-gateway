# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Integration tests — FC persona mapping and attachments response.

Covers:
- FC endpoint creation with target_wwpns
- Mapping with explicit persona (FC) and underlay (iSCSI) endpoint IDs
- Attachments response shape and field validation
- LUN allocation across multiple FC mappings
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_fc_endpoint(
    client: AsyncClient,
    array_id: str,
    target_wwpns: list[str] | None = None,
) -> dict:
    resp = await client.post(f"/v1/arrays/{array_id}/endpoints", json={
        "protocol": "fc",
        "targets": {"target_wwpns": target_wwpns or ["0x500a09c0ffe1aa01"]},
        "addresses": {},
        "auth": {"method": "none"},
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_iscsi_endpoint(
    client: AsyncClient,
    array_id: str,
    target_iqn: str = "iqn.2026-03.com.lunacy:strix.fc.underlay",
) -> dict:
    resp = await client.post(f"/v1/arrays/{array_id}/endpoints", json={
        "protocol": "iscsi",
        "targets": {"target_iqn": target_iqn},
        "addresses": {"portals": ["127.0.0.1:3260"]},
        "auth": {"method": "none"},
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_pool(client: AsyncClient, name: str = "fc-pool") -> dict:
    resp = await client.post("/v1/pools", json={
        "name": name,
        "backend_type": "malloc",
        "size_mb": 4096,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_volume(
    client: AsyncClient, pool_id: str, name: str = "fc-vol",
) -> dict:
    resp = await client.post("/v1/volumes", json={
        "name": name,
        "pool_id": pool_id,
        "size_gb": 1,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_host(
    client: AsyncClient,
    name: str = "fc-host",
    wwpns: list[str] | None = None,
) -> dict:
    resp = await client.post("/v1/hosts", json={
        "name": name,
        "initiators_fc_wwpns": wwpns or ["0x500a09c0ffe1bb01"],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_mapping(
    client: AsyncClient,
    volume_id: str,
    host_id: str,
    persona_endpoint_id: str,
    underlay_endpoint_id: str,
) -> dict:
    resp = await client.post("/v1/mappings", json={
        "volume_id": volume_id,
        "host_id": host_id,
        "persona_endpoint_id": persona_endpoint_id,
        "underlay_endpoint_id": underlay_endpoint_id,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# FC persona full flow
# ---------------------------------------------------------------------------

async def test_fc_mapping_and_attachments(client: AsyncClient):
    """FC persona + iSCSI underlay: mapping → attachments round-trip."""
    pool = await _create_pool(client)
    volume = await _create_volume(client, pool["id"])
    host = await _create_host(client)

    array_id = pool["array_id"]
    fc_ep = await _create_fc_endpoint(client, array_id)
    iscsi_ep = await _create_iscsi_endpoint(client, array_id)

    # Create mapping with explicit endpoint IDs
    mapping = await _create_mapping(
        client, volume["id"], host["id"],
        persona_endpoint_id=fc_ep["id"],
        underlay_endpoint_id=iscsi_ep["id"],
    )
    assert mapping["lun_id"] == 0
    assert mapping["desired_state"] == "attached"
    assert mapping["persona_endpoint_id"] == fc_ep["id"]
    assert mapping["underlay_endpoint_id"] == iscsi_ep["id"]

    # Validate host attachments response
    resp = await client.get(f"/v1/hosts/{host['id']}/attachments")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["host_id"] == host["id"]
    assert "generated_at" in data

    atts = data["attachments"]
    assert len(atts) == 1
    att = atts[0]

    # Persona: FC with correct WWPNs
    assert att["persona"]["protocol"] == "fc"
    assert att["persona"]["target_wwpns"] == ["0x500a09c0ffe1aa01"]
    assert att["persona"]["lun_id"] == 0

    # Underlay: iSCSI with target info and target_lun
    assert att["underlay"]["protocol"] == "iscsi"
    assert att["underlay"]["targets"]["target_iqn"] == "iqn.2026-03.com.lunacy:strix.fc.underlay"
    assert att["underlay"]["addresses"]["portals"] == ["127.0.0.1:3260"]
    assert att["underlay"]["target_lun"] == 0

    # Metadata
    assert att["volume_id"] == volume["id"]
    assert att["desired_state"] == "attached"
    assert att["revision"] == 1


# ---------------------------------------------------------------------------
# LUN allocation across multiple FC mappings
# ---------------------------------------------------------------------------

async def test_fc_lun_allocation(client: AsyncClient):
    """Two volumes mapped to the same host under the same FC endpoint get
    sequential LUN IDs (0, 1)."""
    pool = await _create_pool(client, "fc-lun-pool")
    vol1 = await _create_volume(client, pool["id"], "fc-vol-1")
    vol2 = await _create_volume(client, pool["id"], "fc-vol-2")
    host = await _create_host(client, name="fc-lun-host")

    array_id = pool["array_id"]
    fc_ep = await _create_fc_endpoint(
        client, array_id, target_wwpns=["0x500a09c0ffe1aa02"],
    )
    iscsi_ep = await _create_iscsi_endpoint(
        client, array_id,
        target_iqn="iqn.2026-03.com.lunacy:strix.fc.lun-test",
    )

    m1 = await _create_mapping(
        client, vol1["id"], host["id"],
        persona_endpoint_id=fc_ep["id"],
        underlay_endpoint_id=iscsi_ep["id"],
    )
    m2 = await _create_mapping(
        client, vol2["id"], host["id"],
        persona_endpoint_id=fc_ep["id"],
        underlay_endpoint_id=iscsi_ep["id"],
    )

    assert m1["lun_id"] == 0
    assert m2["lun_id"] == 1

    # Attachments should list both
    resp = await client.get(f"/v1/hosts/{host['id']}/attachments")
    assert resp.status_code == 200
    assert len(resp.json()["attachments"]) == 2
