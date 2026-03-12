# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Hitachi ID mapping and response translation layer.

Maintains bidirectional mappings between canonical UUIDs and Hitachi-native
integer/string IDs.  All mappings are persisted in ``vendor_metadata`` JSON
on the corresponding ORM models and rebuilt from the database on startup.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.core.db import (
    Array,
    Host,
    Mapping,
    Pool,
    TransportEndpoint,
    Volume,
)

logger = logging.getLogger("strix_gateway.personalities.hitachi.translate")

# LDEV range per Hitachi spec
_LDEV_MAX = 65279


def _storage_device_id(array: Array) -> str:
    """Derive a 6-digit storage device serial from the array UUID."""
    meta = array.profile_dict
    if "storage_device_id" in meta:
        return str(meta["storage_device_id"])
    # First 6 hex digits of the UUID, zero-padded
    return array.id.replace("-", "")[:6].upper()


class HitachiIdMapper:
    """Bidirectional Hitachi ↔ canonical UUID mapper.

    All state is read from ``vendor_metadata`` on the ORM models.
    ``rebuild()`` must be called at startup and after mutations that
    affect ID assignment.
    """

    def __init__(self, array_id: str) -> None:
        self.array_id = array_id
        # LDEV ID ↔ Volume UUID
        self._ldev_to_vol: dict[int, str] = {}
        self._vol_to_ldev: dict[str, int] = {}
        # Pool ID ↔ Pool UUID
        self._pool_to_uuid: dict[int, str] = {}
        self._uuid_to_pool: dict[str, int] = {}
        # Port ID (string) ↔ Endpoint UUID
        self._port_to_ep: dict[str, str] = {}
        self._ep_to_port: dict[str, str] = {}
        # Storage device ID (cached)
        self.storage_device_id: str = ""

    # ------------------------------------------------------------------
    # Rebuild from database
    # ------------------------------------------------------------------

    async def rebuild(self, session: AsyncSession) -> None:
        """Rebuild all maps from vendor_metadata in the database."""
        # Array
        arr_result = await session.execute(
            select(Array).where(Array.id == self.array_id)
        )
        arr = arr_result.scalar_one_or_none()
        if arr is None:
            logger.warning("Array %s not found during rebuild", self.array_id)
            return
        self.storage_device_id = _storage_device_id(arr)

        await self._rebuild_pools(session)
        await self._rebuild_volumes(session)
        await self._rebuild_endpoints(session)

    async def _rebuild_pools(self, session: AsyncSession) -> None:
        self._pool_to_uuid.clear()
        self._uuid_to_pool.clear()
        result = await session.execute(select(Pool).where(Pool.array_id == self.array_id))
        next_pool_id = 0
        pools = list(result.scalars().all())
        if not pools:
            # Compatibility fallback: some vhost flows expose array identity
            # separately from where pooled capacity is persisted.
            result = await session.execute(select(Pool))
            pools = list(result.scalars().all())
        # Sort by created_at for deterministic ordering
        pools.sort(key=lambda p: p.created_at)
        for pool in pools:
            meta = pool.vendor_meta_dict
            pid = meta.get("pool_id")
            if pid is not None:
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    pid = None
            if pid is None:
                pid = next_pool_id
                # Persist assignment
                meta["pool_id"] = pid
                pool.vendor_metadata = json.dumps(meta)
            self._pool_to_uuid[pid] = pool.id
            self._uuid_to_pool[pool.id] = pid
            next_pool_id = max(next_pool_id, pid) + 1
        await session.flush()

    async def _rebuild_volumes(self, session: AsyncSession) -> None:
        self._ldev_to_vol.clear()
        self._vol_to_ldev.clear()
        result = await session.execute(select(Volume).where(Volume.array_id == self.array_id))
        next_ldev = 0
        volumes = list(result.scalars().all())
        if not volumes:
            # Compatibility fallback for mixed vhost/core persistence scopes.
            result = await session.execute(select(Volume))
            volumes = list(result.scalars().all())
        volumes.sort(key=lambda v: v.created_at)
        for vol in volumes:
            meta = vol.vendor_meta_dict
            ldev = meta.get("ldev_id")
            if ldev is None:
                ldev = next_ldev
                meta["ldev_id"] = ldev
                vol.vendor_metadata = json.dumps(meta)
            self._ldev_to_vol[ldev] = vol.id
            self._vol_to_ldev[vol.id] = ldev
            next_ldev = max(next_ldev, ldev) + 1
        await session.flush()

    async def _rebuild_endpoints(self, session: AsyncSession) -> None:
        """Rebuild port ID maps; auto-assign if missing (Step 2.10)."""
        self._port_to_ep.clear()
        self._ep_to_port.clear()
        result = await session.execute(
            select(TransportEndpoint).where(
                TransportEndpoint.array_id == self.array_id
            )
        )
        endpoints = list(result.scalars().all())
        endpoints.sort(key=lambda e: e.created_at)

        # Separate FC and iSCSI for auto-assignment
        fc_idx = 1  # CL1-A, CL2-A, ...
        iscsi_idx = 3  # CL3-A, CL4-A, ...

        for ep in endpoints:
            meta = ep.vendor_meta_dict
            port_id = meta.get("hitachi_port_id")
            if port_id is None:
                # Auto-assign based on protocol
                if ep.protocol == "fc":
                    port_id = f"CL{fc_idx}-A"
                    fc_idx += 1
                else:
                    port_id = f"CL{iscsi_idx}-A"
                    iscsi_idx += 1
                meta["hitachi_port_id"] = port_id
                ep.vendor_metadata = json.dumps(meta)
            # Advance counters past already-assigned IDs
            if ep.protocol == "fc":
                # Parse index from "CL{N}-A"
                try:
                    n = int(port_id[2:].split("-")[0])
                    fc_idx = max(fc_idx, n + 1)
                except ValueError:
                    pass
            else:
                try:
                    n = int(port_id[2:].split("-")[0])
                    iscsi_idx = max(iscsi_idx, n + 1)
                except ValueError:
                    pass

            self._port_to_ep[port_id] = ep.id
            self._ep_to_port[ep.id] = port_id

        await session.flush()

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def ldev_for_volume(self, volume_id: str) -> int | None:
        return self._vol_to_ldev.get(volume_id)

    def volume_for_ldev(self, ldev_id: int) -> str | None:
        return self._ldev_to_vol.get(ldev_id)

    def pool_id_for_uuid(self, pool_uuid: str) -> int | None:
        return self._uuid_to_pool.get(pool_uuid)

    def pool_uuid_for_id(self, pool_id: int) -> str | None:
        return self._pool_to_uuid.get(pool_id)

    def port_id_for_endpoint(self, endpoint_id: str) -> str | None:
        return self._ep_to_port.get(endpoint_id)

    def endpoint_for_port(self, port_id: str) -> str | None:
        return self._port_to_ep.get(port_id)

    def next_ldev_id(self) -> int:
        """Return the next available LDEV number."""
        if not self._ldev_to_vol:
            return 0
        return max(self._ldev_to_vol.keys()) + 1

    def register_ldev(self, ldev_id: int, volume_id: str) -> None:
        """Record a newly assigned LDEV ID (call after volume creation)."""
        self._ldev_to_vol[ldev_id] = volume_id
        self._vol_to_ldev[volume_id] = ldev_id

    def unregister_ldev(self, volume_id: str) -> None:
        """Remove LDEV mapping (call after volume deletion)."""
        ldev_id = self._vol_to_ldev.pop(volume_id, None)
        if ldev_id is not None:
            self._ldev_to_vol.pop(ldev_id, None)

    # ------------------------------------------------------------------
    # Response builders
    # ------------------------------------------------------------------

    def volume_to_ldev(self, volume: Volume, pool: Pool) -> dict[str, Any]:
        """Build Hitachi LDEV JSON from a canonical Volume."""
        ldev_id = self._vol_to_ldev.get(volume.id, 0)
        pool_id = self._uuid_to_pool.get(volume.pool_id, 0)
        size_bytes = volume.size_mb * 1024 * 1024
        # blockCapacity in 512-byte blocks
        block_cap = size_bytes // 512
        return {
            "ldevId": ldev_id,
            "label": volume.name,
            "status": "NML" if volume.status == "available" else "BLK",
            "poolId": pool_id,
            "byteFormatCapacity": str(size_bytes),
            "blockCapacity": block_cap,
            "numOfPorts": len(volume.mappings),
            "attributes": ["CVS"] if volume.status == "available" else [],
            "clprId": 0,
            "emulationType": "OPEN-V",
        }

    def pool_to_hitachi(self, pool: Pool, stats: dict | None = None) -> dict[str, Any]:
        """Build Hitachi pool JSON from a canonical Pool."""
        pool_id = self._uuid_to_pool.get(pool.id, 0)
        total = (pool.size_mb or 0) * 1024 * 1024
        used = (stats.get("used_capacity_mb", 0) * 1024 * 1024) if stats else 0
        free = total - used
        return {
            "poolId": pool_id,
            "poolName": pool.name,
            "poolType": "HDP",
            "poolStatus": "POLN",
            "totalPoolCapacity": total,
            "totalLocatedCapacity": total,
            "availableVolumeCapacity": free,
            "usedCapacityRate": int((used / total * 100) if total else 0),
            "numOfLdevs": stats.get("volume_count", 0) if stats else 0,
        }

    def host_to_host_group(
        self,
        host: Host,
        port_id: str,
        hg_number: int,
        iscsi_name: str | None = None,
    ) -> dict[str, Any]:
        """Build Hitachi host-group JSON."""
        data = {
            "hostGroupId": f"{port_id},{hg_number}",
            "portId": port_id,
            "hostGroupNumber": hg_number,
            "hostGroupName": host.name,
            "hostMode": "LINUX/IRIX",
        }
        if iscsi_name:
            data["iscsiName"] = iscsi_name
        return data

    def host_to_iscsi_target(
        self, host: Host, port_id: str, target_number: int
    ) -> dict[str, Any]:
        """Build Hitachi iSCSI-target JSON."""
        return {
            "iscsiTargetId": f"{port_id},{target_number}",
            "portId": port_id,
            "iscsiTargetNumber": target_number,
            "iscsiTargetName": host.name,
            "hostMode": "LINUX/IRIX",
        }

    def mapping_to_lun(
        self,
        mapping: Mapping,
        port_id: str,
        hg_number: int,
    ) -> dict[str, Any]:
        """Build Hitachi LUN JSON."""
        ldev_id = self._vol_to_ldev.get(mapping.volume_id, 0)
        return {
            "lunId": f"{port_id},{hg_number},{mapping.lun_id}",
            "portId": port_id,
            "hostGroupNumber": hg_number,
            "hostGroupName": "",
            "lun": mapping.lun_id,
            "ldevId": ldev_id,
        }

    def array_to_storage(self, array: Array) -> dict[str, Any]:
        """Build Hitachi storage system JSON."""
        return {
            "storageDeviceId": self.storage_device_id,
            "model": "VSP G900",
            "serialNumber": int(self.storage_device_id, 16)
            if self.storage_device_id.isalnum()
            else 0,
            "svpIp": "0.0.0.0",
            "ctl1Ip": "0.0.0.0",
            "ctl2Ip": "0.0.0.0",
            "dkcMicroVersion": "93-06-01-80/00",
        }

    def port_to_hitachi(self, ep: TransportEndpoint) -> dict[str, Any]:
        """Build Hitachi port JSON for a transport endpoint."""
        port_id = self._ep_to_port.get(ep.id, "CL0-A")
        targets = ep.targets_dict
        result: dict[str, Any] = {
            "portId": port_id,
            "portType": "FIBRE" if ep.protocol == "fc" else "ISCSI",
            "portSpeed": "AUTO",
            # Cinder's Hitachi driver expects this key to exist for each port.
            "lunSecuritySetting": True,
        }
        if ep.protocol == "fc":
            wwpns = targets.get("target_wwpns", [])
            result["wwn"] = wwpns[0] if wwpns else ""
        elif ep.protocol == "iscsi":
            result["iscsiIpAddress"] = ""
            result["portAttributes"] = ["TAR"]
            result["ipv4Address"] = ""
            result["tcpPort"] = ""
            addrs = ep.addresses_dict
            portals = addrs.get("portals", [])
            if portals:
                # portal format: "ip:port"
                if ":" in portals[0]:
                    ip, port = portals[0].split(":", 1)
                else:
                    ip, port = portals[0], "3260"
                result["iscsiIpAddress"] = ip
                result["ipv4Address"] = ip
                result["tcpPort"] = str(port)
            else:
                # E2E topologies may omit explicit portal addresses.
                # Provide sane defaults required by the Hitachi Cinder driver.
                result["iscsiIpAddress"] = "127.0.0.1"
                result["ipv4Address"] = "127.0.0.1"
                result["tcpPort"] = "3260"
        return result
