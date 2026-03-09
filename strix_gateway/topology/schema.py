# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Pydantic models for the Strix Gateway topology specification.

A topology file declares arrays, endpoints, pools, volumes, hosts, and
mappings in a single YAML or TOML document.  Example (YAML)::

    arrays:
      - name: svc-a
        vendor: ibm_svc
        endpoints:
          - protocol: iscsi
            targets: {"target_iqn": "iqn.2024-01.com.strix:svc-a"}
            addresses: {"portals": ["10.0.0.1:3260"]}
          - protocol: fc
            targets: {"target_wwpns": ["50:00:00:00:00:00:00:01"]}

    pools:
      - name: gold
        array: svc-a
        backend: malloc
        size_gb: 100

    hosts:
      - name: compute-01
        iqns: ["iqn.1993-08.org.debian:01:abc123"]
        wwpns: ["21:00:00:00:00:00:00:01"]

    volumes:
      - name: vol-001
        size_gb: 20
        pool: gold

    mappings:
      - host: compute-01
        volume: vol-001
        protocol: iscsi
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, model_validator


class EndpointSpec(BaseModel):
    """Transport endpoint declared inline on an array."""

    protocol: str                            # "iscsi" | "nvmeof_tcp" | "fc"
    targets: dict[str, Any] = {}             # e.g. {"target_iqn": "..."} or {"target_wwpns": [...]}
    addresses: dict[str, Any] = {}           # e.g. {"portals": ["ip:port"]}
    auth: dict[str, Any] = {"method": "none"}


class ArraySpec(BaseModel):
    """Specification for one storage array (real or emulated)."""

    name: str
    vendor: str = "generic"
    profile: dict[str, Any] = {}             # capability profile overrides
    endpoints: list[EndpointSpec] = []       # pre-declared transport endpoints


class PoolSpec(BaseModel):
    """Specification for a storage pool within an array."""

    name: str
    array: str                  # array name (must be declared in arrays list)
    backend: Literal["malloc", "aio"] = "malloc"
    size_gb: float              # used for malloc backend
    aio_path: Optional[str] = None  # required when backend == "aio"

    @model_validator(mode="after")
    def _check_aio_path(self) -> "PoolSpec":
        if self.backend == "aio" and not self.aio_path:
            raise ValueError("aio_path is required when backend is 'aio'")
        return self


class VolumeSpec(BaseModel):
    """Specification for a volume within a pool."""

    name: str
    size_gb: float
    pool: str           # pool name (array inferred from pool.array)


class HostSpec(BaseModel):
    """Specification for a storage host (initiator)."""

    name: str
    iqns: list[str] = []    # iSCSI initiator IQNs
    nqns: list[str] = []    # NVMe-oF host NQNs
    wwpns: list[str] = []   # FC initiator WWPNs


class MappingSpec(BaseModel):
    """Specification for a volume-to-host mapping."""

    host: str       # host name (must exist in hosts list)
    volume: str     # volume name (must exist in volumes list)
    protocol: str   # e.g. "iscsi", "nvmeof_tcp", or "fc"


class TopologySpec(BaseModel):
    """Root model for a complete Strix Gateway topology specification."""

    arrays: list[ArraySpec] = []
    pools: list[PoolSpec] = []
    hosts: list[HostSpec] = []
    volumes: list[VolumeSpec] = []
    mappings: list[MappingSpec] = []
