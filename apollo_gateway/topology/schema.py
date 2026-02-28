# FILE: apollo_gateway/topology/schema.py
"""Pydantic models for the Apollo Gateway topology specification.

A topology file declares subsystems, pools, volumes, hosts, and mappings
in a single YAML or TOML document.  Example (YAML)::

    subsystems:
      - name: svc-a
        persona: ibm_svc
        protocols: [iscsi]

    pools:
      - name: gold
        subsystem: svc-a
        backend: malloc
        size_gb: 100

    hosts:
      - name: compute-01
        iqns: ["iqn.1993-08.org.debian:01:abc123"]

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


class CapabilityProfileOverride(BaseModel):
    """Partial capability profile override for use in topology specs."""

    model: Optional[str] = None
    version: Optional[str] = None
    features: dict[str, Any] = {}
    limits: dict[str, Any] = {}
    quirks: dict[str, Any] = {}


class SubsystemSpec(BaseModel):
    """Specification for one virtual storage subsystem."""

    name: str
    persona: str = "generic"
    protocols: list[str] = ["iscsi", "nvmeof_tcp"]
    capability_profile: Optional[CapabilityProfileOverride] = None


class PoolSpec(BaseModel):
    """Specification for a storage pool within a subsystem."""

    name: str
    subsystem: str              # subsystem name (must be declared in subsystems list)
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
    pool: str           # pool name (subsystem inferred from pool.subsystem)


class HostSpec(BaseModel):
    """Specification for a storage host (initiator)."""

    name: str
    iqns: list[str] = []    # iSCSI initiator IQNs
    nqns: list[str] = []    # NVMe-oF host NQNs


class MappingSpec(BaseModel):
    """Specification for a volume-to-host mapping."""

    host: str       # host name (must exist in hosts list)
    volume: str     # volume name (must exist in volumes list)
    protocol: str   # e.g. "iscsi" or "nvmeof_tcp"


class TopologySpec(BaseModel):
    """Root model for a complete Apollo Gateway topology specification."""

    subsystems: list[SubsystemSpec] = []
    pools: list[PoolSpec] = []
    hosts: list[HostSpec] = []
    volumes: list[VolumeSpec] = []
    mappings: list[MappingSpec] = []
