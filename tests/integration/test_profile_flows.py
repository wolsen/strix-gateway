# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Integration tests covering the remaining storage profiles and protocols.

These tests extend the main ``/v1`` flow coverage beyond the default generic
array so we exercise:

- generic arrays over NVMe/TCP
- hitachi arrays through FC persona + iSCSI underlay auto-resolution
- hpe_3par arrays through iSCSI mapping/attachments
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.asyncio


async def _create_array(
    client: AsyncClient,
    *,
    name: str,
    vendor: str,
    profile: dict | None = None,
) -> dict:
    resp = await client.post(
        "/v1/arrays",
        json={"name": name, "vendor": vendor, "profile": profile or {}},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _get_capabilities(client: AsyncClient, array_id: str) -> dict:
    resp = await client.get(f"/v1/arrays/{array_id}/capabilities")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_pool(client: AsyncClient, *, name: str, array_id: str) -> dict:
    resp = await client.post(
        "/v1/pools",
        json={"name": name, "backend_type": "malloc", "size_mb": 4096},
    )
    assert resp.status_code == 201, resp.text
    pool = resp.json()

    resp = await client.post(f"/v1/arrays/{array_id}/pools/{pool['id']}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_endpoint(
    client: AsyncClient,
    *,
    array_id: str,
    protocol: str,
    targets: dict,
    addresses: dict | None = None,
) -> dict:
    resp = await client.post(
        f"/v1/arrays/{array_id}/endpoints",
        json={
            "protocol": protocol,
            "targets": targets,
            "addresses": addresses or {},
            "auth": {"method": "none"},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_volume(
    client: AsyncClient,
    *,
    pool_id: str,
    name: str,
) -> dict:
    resp = await client.post(
        "/v1/volumes",
        json={"name": name, "pool_id": pool_id, "size_gb": 1},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_host(
    client: AsyncClient,
    *,
    name: str,
    iqns: list[str] | None = None,
    nqns: list[str] | None = None,
    wwpns: list[str] | None = None,
) -> dict:
    resp = await client.post(
        "/v1/hosts",
        json={
            "name": name,
            "initiators_iscsi_iqns": iqns or [],
            "initiators_nvme_host_nqns": nqns or [],
            "initiators_fc_wwpns": wwpns or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_mapping(client: AsyncClient, **payload) -> dict:
    resp = await client.post("/v1/mappings", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _attachments(client: AsyncClient, host_id: str) -> dict:
    resp = await client.get(f"/v1/hosts/{host_id}/attachments")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_generic_nvmeof_tcp_full_flow_uses_nsid(client: AsyncClient, mock_spdk):
    array = await _create_array(client, name="nvme-a", vendor="generic")
    caps = await _get_capabilities(client, array["id"])
    assert caps["vendor"] == "generic"
    assert caps["effective_profile"]["model"] == "Strix-Generic"

    pool = await _create_pool(client, name="nvme-pool", array_id=array["id"])
    nvme_ep = await _create_endpoint(
        client,
        array_id=array["id"],
        protocol="nvmeof_tcp",
        targets={"subsystem_nqn": "nqn.2026-03.com.lunacy:nvme-a"},
        addresses={"listeners": ["127.0.0.1:4420"]},
    )

    volume = await _create_volume(client, pool_id=pool["id"], name="nvme-vol")
    host = await _create_host(
        client,
        name="nvme-host",
        nqns=["nqn.2014-08.org.nvmexpress:uuid:nvme-host"],
    )

    mapping = await _create_mapping(
        client,
        volume_id=volume["id"],
        host_id=host["id"],
        persona_endpoint_id=nvme_ep["id"],
        underlay_endpoint_id=nvme_ep["id"],
    )
    assert mapping["lun_id"] == 0
    assert mapping["underlay_id"] == 1

    data = await _attachments(client, host["id"])
    assert data["host_id"] == host["id"]
    assert len(data["attachments"]) == 1
    att = data["attachments"][0]
    assert att["array_id"] == array["id"]
    assert att["persona"]["protocol"] == "nvmeof_tcp"
    assert att["persona"]["lun_id"] == 0
    assert att["underlay"]["protocol"] == "nvmeof_tcp"
    assert att["underlay"]["targets"]["subsystem_nqn"] == "nqn.2026-03.com.lunacy:nvme-a"
    assert att["underlay"]["addresses"]["listeners"] == ["127.0.0.1:4420"]
    assert att["underlay"]["nsid"] == 1
    assert att["underlay"]["target_lun"] is None

    methods = [call.args[0] for call in mock_spdk.call.call_args_list]
    assert "nvmf_create_transport" in methods
    assert "nvmf_get_subsystems" in methods
    assert "nvmf_create_subsystem" in methods
    assert "nvmf_subsystem_add_listener" in methods
    assert "nvmf_subsystem_add_ns" in methods


async def test_hitachi_profile_flow_uses_fc_persona_auto_resolution(client: AsyncClient):
    array = await _create_array(client, name="hitachi-b", vendor="hitachi")
    caps = await _get_capabilities(client, array["id"])
    assert caps["vendor"] == "hitachi"
    assert caps["effective_profile"]["model"] == "VSP-stub"
    assert caps["effective_profile"]["features"]["multiattach"] is False

    pool = await _create_pool(client, name="hitachi-pool", array_id=array["id"])
    fc_ep = await _create_endpoint(
        client,
        array_id=array["id"],
        protocol="fc",
        targets={"target_wwpns": ["0x500a09c0ffe1cc01"]},
    )
    iscsi_ep = await _create_endpoint(
        client,
        array_id=array["id"],
        protocol="iscsi",
        targets={"target_iqn": "iqn.2026-03.com.lunacy:hitachi-b"},
        addresses={"portals": ["10.0.0.11:3260"]},
    )

    volume = await _create_volume(client, pool_id=pool["id"], name="hitachi-vol")
    host = await _create_host(
        client,
        name="hitachi-host",
        wwpns=["0x500a09c0ffe1dd01"],
    )

    mapping = await _create_mapping(
        client,
        volume_id=volume["id"],
        host_id=host["id"],
    )
    assert mapping["persona_endpoint_id"] == fc_ep["id"]
    assert mapping["underlay_endpoint_id"] == iscsi_ep["id"]
    assert mapping["lun_id"] == 0
    assert mapping["underlay_id"] == 0

    data = await _attachments(client, host["id"])
    assert len(data["attachments"]) == 1
    att = data["attachments"][0]
    assert att["array_id"] == array["id"]
    assert att["persona"]["protocol"] == "fc"
    assert att["persona"]["target_wwpns"] == ["0x500a09c0ffe1cc01"]
    assert att["underlay"]["protocol"] == "iscsi"
    assert att["underlay"]["targets"]["target_iqn"] == "iqn.2026-03.com.lunacy:hitachi-b"
    assert att["underlay"]["addresses"]["portals"] == ["10.0.0.11:3260"]
    assert att["underlay"]["target_lun"] == 0


async def test_hpe3par_profile_iscsi_flow_on_non_default_array(client: AsyncClient):
    array = await _create_array(client, name="threepar-b", vendor="hpe_3par")
    caps = await _get_capabilities(client, array["id"])
    assert caps["vendor"] == "hpe_3par"
    assert caps["effective_profile"]["model"] == "3PAR-stub"
    assert caps["effective_profile"]["features"]["multiattach"] is True
    assert caps["effective_profile"]["quirks"]["strict_name_length"] == 31

    pool = await _create_pool(client, name="threepar-pool", array_id=array["id"])
    iscsi_ep = await _create_endpoint(
        client,
        array_id=array["id"],
        protocol="iscsi",
        targets={"target_iqn": "iqn.2026-03.com.lunacy:threepar-b"},
        addresses={"portals": ["10.0.0.12:3260"]},
    )

    volume = await _create_volume(client, pool_id=pool["id"], name="threepar-vol")
    host = await _create_host(
        client,
        name="threepar-host",
        iqns=["iqn.2026-03.com.lunacy:threepar-host"],
    )

    mapping = await _create_mapping(
        client,
        volume_id=volume["id"],
        host_id=host["id"],
        persona_protocol="iscsi",
        underlay_protocol="iscsi",
    )
    assert mapping["persona_endpoint_id"] == iscsi_ep["id"]
    assert mapping["underlay_endpoint_id"] == iscsi_ep["id"]
    assert mapping["lun_id"] == 0
    assert mapping["underlay_id"] == 0

    data = await _attachments(client, host["id"])
    assert len(data["attachments"]) == 1
    att = data["attachments"][0]
    assert att["array_id"] == array["id"]
    assert att["persona"]["protocol"] == "iscsi"
    assert att["persona"]["target_wwpns"] == []
    assert att["underlay"]["protocol"] == "iscsi"
    assert att["underlay"]["targets"]["target_iqn"] == "iqn.2026-03.com.lunacy:threepar-b"
    assert att["underlay"]["addresses"]["portals"] == ["10.0.0.12:3260"]
    assert att["underlay"]["target_lun"] == 0
