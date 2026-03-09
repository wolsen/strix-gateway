# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Apply a validated topology specification to a live Strix Gateway instance.

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

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.core.db import Array, Host, Mapping, Pool, Volume
from strix_gateway.core.exceptions import AlreadyExistsError, NotFoundError
from strix_gateway.core import (
    arrays as arrays_svc,
    endpoints as endpoints_svc,
    hosts as hosts_svc,
    mappings as mappings_svc,
    pools as pools_svc,
    volumes as volumes_svc,
)
from strix_gateway.core.models import PoolBackendType
from strix_gateway.topology.schema import TopologySpec

if TYPE_CHECKING:
    from strix_gateway.config import Settings
    from strix_gateway.spdk.rpc import SPDKClient

logger = logging.getLogger("strix_gateway.topology.apply")


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
        Validated :class:`~strix_gateway.topology.schema.TopologySpec`.
    session:
        Active :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
    spdk_client:
        Connected :class:`~strix_gateway.spdk.rpc.SPDKClient`.
    settings:
        Gateway :class:`~strix_gateway.config.Settings`.

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
        try:
            existing = await arrays_svc.resolve_array(session, arr_spec.name)
            logger.debug("Array '%s' already exists — skipping", arr_spec.name)
            array_objs[arr_spec.name] = existing
            summary["arrays"]["skipped"] += 1
        except NotFoundError:
            arr = await arrays_svc.create_array(
                session, name=arr_spec.name,
                vendor=arr_spec.vendor, profile=arr_spec.profile,
            )
            array_objs[arr_spec.name] = arr
            summary["arrays"]["created"] += 1
            logger.info("Created array '%s' (vendor=%s)", arr_spec.name, arr_spec.vendor)

        # Ensure declared endpoints exist on the array
        arr_obj = array_objs[arr_spec.name]
        for ep_spec in arr_spec.endpoints:
            existing_eps = await endpoints_svc.list_endpoints(session, arr_obj.id)
            already = any(e.protocol == ep_spec.protocol for e in existing_eps)
            if not already:
                await endpoints_svc.create_endpoint(
                    session,
                    array_id=arr_obj.id,
                    protocol=ep_spec.protocol,
                    targets=ep_spec.targets,
                    addresses=ep_spec.addresses,
                    auth=ep_spec.auth,
                )
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

        try:
            existing = (await session.execute(
                select(Pool).where(Pool.array_id == arr.id, Pool.name == pool_spec.name)
            )).scalar_one_or_none()
            if existing:
                logger.debug("Pool '%s' already exists in array '%s' — skipping",
                             pool_spec.name, pool_spec.array)
                pool_objs[pool_spec.name] = existing
                summary["pools"]["skipped"] += 1
                continue
        except Exception:
            pass

        size_mb = int(pool_spec.size_gb * 1024)
        backend_type = (
            PoolBackendType.malloc if pool_spec.backend == "malloc"
            else PoolBackendType.aio_file
        )
        pool = await pools_svc.create_pool(
            session, spdk_client,
            name=pool_spec.name,
            array_id=arr.id,
            backend_type=backend_type,
            size_mb=size_mb if pool_spec.backend == "malloc" else None,
            aio_path=pool_spec.aio_path,
        )
        pool_objs[pool_spec.name] = pool
        summary["pools"]["created"] += 1
        logger.info("Created pool '%s' in array '%s'", pool_spec.name, arr.name)

    # ---- 3. Hosts ------------------------------------------------------------
    host_objs: dict[str, Host] = {}
    for host_spec in spec.hosts:
        try:
            existing = await hosts_svc.get_host_by_name(session, host_spec.name)
            logger.debug("Host '%s' already exists — skipping", host_spec.name)
            host_objs[host_spec.name] = existing
            summary["hosts"]["skipped"] += 1
        except NotFoundError:
            host = await hosts_svc.create_host(
                session,
                name=host_spec.name,
                iscsi_iqns=host_spec.iqns,
                nvme_nqns=host_spec.nqns,
                fc_wwpns=host_spec.wwpns,
            )
            host_objs[host_spec.name] = host
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

        # Resolve the array for this pool
        arr_name = None
        for name, a in array_objs.items():
            if a.id == pool.array_id:
                arr_name = name
                break
        arr = array_objs.get(arr_name) if arr_name else None
        if arr is None:
            logger.warning("Volume '%s': array not found — skipping", vol_spec.name)
            continue

        try:
            existing = await volumes_svc.get_volume_by_name(session, vol_spec.name, arr.id)
            logger.debug("Volume '%s' already exists — skipping", vol_spec.name)
            volume_objs[vol_spec.name] = existing
            summary["volumes"]["skipped"] += 1
        except NotFoundError:
            size_mb = int(vol_spec.size_gb * 1024)
            vol = await volumes_svc.create_volume(
                session, spdk_client,
                name=vol_spec.name,
                pool_id=pool.id,
                size_mb=size_mb,
            )
            volume_objs[vol_spec.name] = vol
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

        # Check if mapping already exists
        existing_mapping = await mappings_svc.find_mapping_by_host_and_volume(
            session, host.id, vol.id,
        )
        if existing_mapping:
            logger.debug("Mapping already exists — skipping")
            summary["mappings"]["skipped"] += 1
            continue

        await mappings_svc.create_mapping(
            session, spdk_client, settings,
            host_id=host.id,
            volume_id=vol.id,
            persona_protocol=map_spec.protocol,
        )
        summary["mappings"]["created"] += 1
        logger.info("Created mapping: host='%s' volume='%s' protocol='%s'",
                    map_spec.host, map_spec.volume, map_spec.protocol)

    await session.commit()
    return summary
