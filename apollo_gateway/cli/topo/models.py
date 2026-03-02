# FILE: apollo_gateway/cli/topo/models.py
"""Pydantic models for Apollo Gateway topology files.

These models are self-contained within the CLI package so that the CLI
can validate topology YAML/TOML files without importing any server-side
modules.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, model_validator


# ------------------------------------------------------------------
# Capability profile (inline in topology)
# ------------------------------------------------------------------

class CapabilityProfileSpec(BaseModel):
    """Partial capability profile override carried inside a topology file."""

    model: Optional[str] = None
    version: Optional[str] = None
    features: dict[str, Any] = {}
    limits: dict[str, Any] = {}
    quirks: dict[str, Any] = {}


# ------------------------------------------------------------------
# Resource specs
# ------------------------------------------------------------------

class SubsystemSpec(BaseModel):
    """Specification for one virtual storage subsystem."""

    name: str
    persona: str = "generic"
    protocols: list[str] = ["iscsi", "nvmeof_tcp"]
    capability_profile: Optional[CapabilityProfileSpec] = None


class PoolSpec(BaseModel):
    """Specification for a storage pool within a subsystem."""

    name: str
    subsystem: str
    backend: Literal["malloc", "aio"] = "malloc"
    size_gb: float
    aio_path: Optional[str] = None
    thin: Optional[bool] = None

    @model_validator(mode="after")
    def _check_aio_path(self) -> PoolSpec:
        if self.backend == "aio" and not self.aio_path:
            raise ValueError("aio_path is required when backend is 'aio'")
        return self


class HostSpec(BaseModel):
    """Specification for a storage host (initiator endpoints)."""

    name: str
    initiators: dict[str, list[str]] = {}
    # Convenience aliases compatible with existing topology schemas
    iqns: list[str] = []
    nqns: list[str] = []

    @model_validator(mode="after")
    def _normalise_initiators(self) -> HostSpec:
        if self.iqns and "iscsi" not in self.initiators:
            self.initiators["iscsi"] = list(self.iqns)
        if self.nqns and "nvme" not in self.initiators:
            self.initiators["nvme"] = list(self.nqns)
        return self


class VolumeSpec(BaseModel):
    """Specification for a volume within a pool."""

    name: str
    size_gb: float
    pool: str
    thin: Optional[bool] = None


class MappingSpec(BaseModel):
    """Specification for a volume-to-host mapping."""

    host: str
    volume: str
    protocol: str


class FaultSpec(BaseModel):
    """Fault injection entry (optional section)."""

    operation: str
    error_message: str


class DelaySpec(BaseModel):
    """Delay injection entry (optional section)."""

    operation: str
    delay_seconds: float


# ------------------------------------------------------------------
# Root document
# ------------------------------------------------------------------

class TopologyFile(BaseModel):
    """Root model for a complete Apollo Gateway topology file."""

    subsystems: list[SubsystemSpec] = []
    pools: list[PoolSpec] = []
    hosts: list[HostSpec] = []
    volumes: list[VolumeSpec] = []
    mappings: list[MappingSpec] = []
    faults: list[FaultSpec] = []
    delays: list[DelaySpec] = []
