# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Apply a validated topology specification to a live Apollo Gateway instance.

Resources are created in dependency order:
  1. Arrays (+ declared TransportEndpoints)
  2. Pools (SPDK: backing bdev + lvol store)
  3. Hosts
  4. Volumes (SPDK: lvol)
  5. Mappings (find/create endpoints, SPDK: LUN / namespace attachment)

Existing resources are skipped (idempotent).

Example::

    spec = load_yaml("examples/ci/single_svc.yaml")
    errors = validate(spec)
    if not errors:
        summary = await apply_topology(spec, session, spdk_client, settings)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.core.db import (
    Array,
    Host,
    Mapping,
    Pool,
    TransportEndpoint,
    Volume,
)
from apollo_gateway.core.models import PoolBackendType
from apollo_gateway.core.personas import merge_profile
from apollo_gateway.spdk import ensure as spdk_ensure
from apollo_gateway.topology.schema import TopologySpec

if TYPE_CHECKING:
    from apollo_gateway.config import Settings
    from apollo_gateway.spdk.rpc import SPDKClient

logger = logging.getLogger("apollo_gateway.topology.apply")


async def apply_topology(
    spec: TopologySpec,
    session: AsyncSession,
    spdk_client: "SPDKClient",
    settings: "Settings",
) -> dict[str, Any]:
    """Apply *spec* to the running gateway instance.

    Parameters
    ----------
    spec:
        Validated :class:`~apollo_gateway.topology.schema.TopologySpec`.
    session:
        Active :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
    spdk_client:
        Connected :class:`~apollo_gateway.spdk.rpc.SPDKClient`.
    settings:
        Gateway :class:`~apollo_gateway.config.Settings`.

    Returns
    -------
    dict
        Summary with keys ``arrays``, ``pools``, ``hosts``, ``volumes``,
        ``mappings`` — each mapping to a dict with ``created`` and ``skipped``
        counts.
    """
    summary: dict[str, dict[str, int]] = {
        "arrays": {"created": 0, "skipped": 0},
        "pools": {"created": 0, "skipped": 0},
        "hosts": {"created": 0, "skipped": 0},
        "volumes": {"created": 0, "skipped": 0},
        "mappings": {"created": 0, "skipped": 0},
    }

    # ---- 1. Arrays + declared endpoints --------------------------------------
    array_objs: dict[str, Array] = {}
    for arr_spec in spec.arrays:
        existing = (await session.execute(
            select(Array).where(Array.name == arr_spec.name)
        )).scalar_one_or_none()

        if existing:
            logger.debug("Array '%s' already exists — skipping", arr_spec.name)
            array_objs[arr_spec.name] = existing
            summary["arrays"]["skipped"] += 1
        else:
            new_arr = Array(
                name=arr_spec.name,
                vendor=arr_spec.vendor,
                profile=json.dumps(arr_spec.profile),
            )
            session.add(new_arr)
            await session.flush()
            array_objs[arr_spec.name] = new_arr
            summary["arrays"]["created"] += 1
            logger.info("Created array '%s' (vendor=%s)", arr_spec.name, arr_spec.vendor)

        # Ensure declared endpoints exist on the array
        arr_obj = array_objs[arr_spec.name]
        for ep_spec in arr_spec.endpoints:
            existing_ep = (await session.execute(
                select(TransportEndpoint).where(
                    TransportEndpoint.array_id == arr_obj.id,
                    TransportEndpoint.protocol == ep_spec.protocol,
                )
            )).scalar_one_or_none()
            if existing_ep is None:
                ep = TransportEndpoint(
                    array_id=arr_obj.id,
                    protocol=ep_spec.protocol,
                    targets=json.dumps(ep_spec.targets),
                    addresses=json.dumps(ep_spec.addresses),
                    auth=json.dumps(ep_spec.auth),
                )
                session.add(ep)
                await session.flush()
                logger.info("Created %s endpoint on array '%s'",
                            ep_spec.protocol, arr_spec.name)

    # ---- 2. Pools ------------------------------------------------------------
    pool_objs: dict[str, Pool] = {}
    for pool_spec in spec.pools:
        arr = array_objs.get(pool_spec.array)
        if arr is None:
            logger.warning("Pool '%s': array '%s' not found — skipping",
                           pool_spec.name, pool_spec.array)
            continue

        existing = (await session.execute(
            select(Pool).where(
                Pool.array_id == arr.id,
                Pool.name == pool_spec.name,
            )
        )).scalar_one_or_none()

        if existing:
            logger.debug("Pool '%s' already exists in array '%s' — skipping",
                         pool_spec.name, pool_spec.array)
            pool_objs[pool_spec.name] = existing
            summary["pools"]["skipped"] += 1
        else:
            size_mb = int(pool_spec.size_gb * 1024)
            backend_type = (
                PoolBackendType.malloc if pool_spec.backend == "malloc"
                else PoolBackendType.aio_file
            )
            new_pool = Pool(
                name=pool_spec.name,
                array_id=arr.id,
                backend_type=backend_type.value,
                size_mb=size_mb if pool_spec.backend == "malloc" else None,
                aio_path=pool_spec.aio_path,
            )
            session.add(new_pool)
            await session.flush()
            spdk_ensure.ensure_pool(spdk_client, new_pool, arr.name)
            pool_objs[pool_spec.name] = new_pool
            summary["pools"]["created"] += 1
            logger.info("Created pool '%s' in array '%s'", pool_spec.name, arr.name)

    # ---- 3. Hosts ------------------------------------------------------------
    host_objs: dict[str, Host] = {}
    for host_spec in spec.hosts:
        existing = (await session.execute(
            select(Host).where(Host.name == host_spec.name)
        )).scalar_one_or_none()

        if existing:
            logger.debug("Host '%s' already exists — skipping", host_spec.name)
            host_objs[host_spec.name] = existing
            summary["hosts"]["skipped"] += 1
        else:
            new_host = Host(
                name=host_spec.name,
                initiators_iscsi_iqns=json.dumps(host_spec.iqns),
                initiators_nvme_host_nqns=json.dumps(host_spec.nqns),
                initiators_fc_wwpns=json.dumps(host_spec.wwpns),
            )
            session.add(new_host)
            await session.flush()
            host_objs[host_spec.name] = new_host
            summary["hosts"]["created"] += 1
            logger.info("Created host '%s'", host_spec.name)

    # ---- 4. Volumes ----------------------------------------------------------
    volume_objs: dict[str, Volume] = {}
    for vol_spec in spec.volumes:
        pool = pool_objs.get(vol_spec.pool)
        if pool is None:
            logger.warning("Volume '%s': pool '%s' not found — skipping",
                           vol_spec.name, vol_spec.pool)
            continue

        arr = array_objs.get(pool.array.name)
        if arr is None:
            logger.warning("Volume '%s': array not found — skipping", vol_spec.name)
            continue

        existing = (await session.execute(
            select(Volume).where(
                Volume.array_id == arr.id,
                Volume.name == vol_spec.name,
            )
        )).scalar_one_or_none()

        if existing:
            logger.debug("Volume '%s' already exists — skipping", vol_spec.name)
            volume_objs[vol_spec.name] = existing
            summary["volumes"]["skipped"] += 1
        else:
            size_mb = int(vol_spec.size_gb * 1024)
            new_vol = Volume(
                name=vol_spec.name,
                array_id=arr.id,
                pool_id=pool.id,
                size_mb=size_mb,
                status="creating",
            )
            session.add(new_vol)
            await session.flush()

            bdev_name = spdk_ensure.ensure_lvol(
                spdk_client, new_vol, pool.name, arr.name
            )
            new_vol.bdev_name = bdev_name
            new_vol.status = "available"
            volume_objs[vol_spec.name] = new_vol
            summary["volumes"]["created"] += 1
            logger.info("Created volume '%s' (%d MiB) in pool '%s'",
                        vol_spec.name, size_mb, pool.name)

    # ---- 5. Mappings ---------------------------------------------------------
    for map_spec in spec.mappings:
        host = host_objs.get(map_spec.host)
        vol = volume_objs.get(map_spec.volume)
        if host is None or vol is None:
            logger.warning(
                "Mapping host='%s' volume='%s': one or more resources missing — skipping",
                map_spec.host, map_spec.volume,
            )
            continue

        pool = pool_objs.get(vol.pool.name)
        if pool is None:
            logger.warning("Mapping: pool for volume '%s' not found — skipping", map_spec.volume)
            continue

        arr = array_objs.get(pool.array.name)
        if arr is None:
            logger.warning("Mapping: array not found — skipping")
            continue

        # Find or create the persona endpoint (what the host sees — matches mapping protocol)
        persona_ep = (await session.execute(
            select(TransportEndpoint).where(
                TransportEndpoint.array_id == arr.id,
                TransportEndpoint.protocol == map_spec.protocol,
            )
        )).scalar_one_or_none()

        if persona_ep is None:
            # Auto-create a persona endpoint for this protocol
            persona_ep = TransportEndpoint(
                array_id=arr.id,
                protocol=map_spec.protocol,
                targets=json.dumps({}),
                addresses=json.dumps({}),
                auth=json.dumps({"method": "none"}),
            )
            session.add(persona_ep)
            await session.flush()

            # Generate targets based on protocol
            if map_spec.protocol == "iscsi":
                persona_ep.targets = json.dumps({
                    "target_iqn": f"{settings.iqn_prefix}:{arr.name}:{persona_ep.id}"
                })
                persona_ep.addresses = json.dumps({
                    "portals": [f"{settings.iscsi_portal_ip}:{settings.iscsi_portal_port}"]
                })
            elif map_spec.protocol == "nvmeof_tcp":
                persona_ep.targets = json.dumps({
                    "subsystem_nqn": f"{settings.nqn_prefix}:{arr.name}:{persona_ep.id}"
                })
                persona_ep.addresses = json.dumps({
                    "listeners": [f"{settings.nvmef_portal_ip}:{settings.nvmef_portal_port}"]
                })
            logger.info("Auto-created %s persona endpoint on array '%s'",
                        map_spec.protocol, arr.name)

        # For underlay, use the same endpoint when persona == underlay (non-FC case).
        # For FC persona backed by iSCSI underlay, find/create an iSCSI endpoint.
        if map_spec.protocol == "fc":
            underlay_ep = (await session.execute(
                select(TransportEndpoint).where(
                    TransportEndpoint.array_id == arr.id,
                    TransportEndpoint.protocol == "iscsi",
                )
            )).scalar_one_or_none()
            if underlay_ep is None:
                underlay_ep = TransportEndpoint(
                    array_id=arr.id,
                    protocol="iscsi",
                    targets=json.dumps({
                        "target_iqn": f"{settings.iqn_prefix}:{arr.name}:underlay"
                    }),
                    addresses=json.dumps({
                        "portals": [f"{settings.iscsi_portal_ip}:{settings.iscsi_portal_port}"]
                    }),
                    auth=json.dumps({"method": "none"}),
                )
                session.add(underlay_ep)
                await session.flush()
                logger.info("Auto-created iSCSI underlay endpoint on array '%s'", arr.name)
        else:
            underlay_ep = persona_ep

        # Check if mapping already exists
        existing_mapping = (await session.execute(
            select(Mapping).where(
                Mapping.volume_id == vol.id,
                Mapping.host_id == host.id,
                Mapping.persona_endpoint_id == persona_ep.id,
            )
        )).scalar_one_or_none()

        if existing_mapping:
            logger.debug("Mapping already exists — skipping")
            summary["mappings"]["skipped"] += 1
            continue

        # Allocate LUN / NSID on the persona endpoint
        existing_maps = (await session.execute(
            select(Mapping).where(Mapping.persona_endpoint_id == persona_ep.id)
        )).scalars().all()
        used_luns = [m.lun_id for m in existing_maps if m.lun_id is not None]
        lun_id = spdk_ensure.allocate_lun(used_luns)

        # Allocate underlay ID
        existing_underlay_maps = (await session.execute(
            select(Mapping).where(Mapping.underlay_endpoint_id == underlay_ep.id)
        )).scalars().all()
        used_underlay = [m.underlay_id for m in existing_underlay_maps if m.underlay_id is not None]
        underlay_id = spdk_ensure.allocate_lun(used_underlay)

        new_mapping = Mapping(
            volume_id=vol.id,
            host_id=host.id,
            persona_endpoint_id=persona_ep.id,
            underlay_endpoint_id=underlay_ep.id,
            lun_id=lun_id,
            underlay_id=underlay_id,
            desired_state="attached",
            revision=1,
        )
        session.add(new_mapping)
        await session.flush()

        # Wire up SPDK for the underlay endpoint (FC persona has no SPDK state)
        if underlay_ep.protocol == "iscsi":
            spdk_ensure.ensure_iscsi_export(spdk_client, underlay_ep, settings)
            spdk_ensure.ensure_iscsi_mapping(spdk_client, new_mapping, vol, underlay_ep)
        elif underlay_ep.protocol == "nvmeof_tcp":
            profile = merge_profile(arr.vendor, json.loads(arr.profile))
            spdk_ensure.ensure_nvmef_export(
                spdk_client, underlay_ep, settings,
                model_number=profile.model,
                serial_number=f"APOLLO-{arr.name[:8].upper()}",
            )
            spdk_ensure.ensure_nvmef_mapping(spdk_client, new_mapping, vol, underlay_ep)

        summary["mappings"]["created"] += 1
        logger.info("Created mapping: host='%s' volume='%s' protocol='%s'",
                    map_spec.host, map_spec.volume, map_spec.protocol)

    await session.commit()
    return summary
