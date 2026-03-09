# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Pydantic models for Strix Gateway topology files.

These models are self-contained within the CLI package so that the CLI
can validate topology YAML/TOML files without importing any server-side
modules.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, model_validator


# ------------------------------------------------------------------
# Resource specs
# ------------------------------------------------------------------

class EndpointSpec(BaseModel):
    """Transport endpoint declared inline on an array."""

    protocol: str
    targets: dict[str, Any] = {}
    addresses: dict[str, Any] = {}
    auth: dict[str, Any] = {"method": "none"}


class ArraySpec(BaseModel):
    """Specification for one storage array (real or emulated)."""

    name: str
    vendor: str = "generic"
    profile: dict[str, Any] = {}
    endpoints: list[EndpointSpec] = []


class PoolSpec(BaseModel):
    """Specification for a storage pool within an array."""

    name: str
    array: str
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
    iqns: list[str] = []
    nqns: list[str] = []
    wwpns: list[str] = []


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
    """Root model for a complete Strix Gateway topology file."""

    arrays: list[ArraySpec] = []
    pools: list[PoolSpec] = []
    hosts: list[HostSpec] = []
    volumes: list[VolumeSpec] = []
    mappings: list[MappingSpec] = []
    faults: list[FaultSpec] = []
    delays: list[DelaySpec] = []
