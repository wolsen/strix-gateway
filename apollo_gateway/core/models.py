# FILE: apollo_gateway/core/models.py
"""Pydantic request/response schemas and domain enums."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class PoolBackendType(str, Enum):
    malloc = "malloc"
    aio_file = "aio_file"


class VolumeStatus(str, Enum):
    creating = "creating"
    available = "available"
    in_use = "in_use"
    deleting = "deleting"
    extending = "extending"
    error = "error"


class Protocol(str, Enum):
    iscsi = "iscsi"
    nvmeof_tcp = "nvmeof_tcp"


# ---------------------------------------------------------------------------
# Subsystem
# ---------------------------------------------------------------------------

class SubsystemCreate(BaseModel):
    name: str
    persona: str = "generic"
    protocols_enabled: list[str] = ["iscsi", "nvmeof_tcp"]
    # Overrides applied on top of persona defaults at query time
    capability_profile: dict[str, Any] = {}


class SubsystemUpdate(BaseModel):
    persona: Optional[str] = None
    protocols_enabled: Optional[list[str]] = None
    capability_profile: Optional[dict[str, Any]] = None


class SubsystemView(BaseModel):
    id: str
    name: str
    persona: str
    protocols_enabled: list[str]
    capability_profile: dict[str, Any]  # raw stored overrides
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CapabilitiesView(BaseModel):
    subsystem_id: str
    subsystem_name: str
    persona: str
    protocols_enabled: list[str]
    effective_profile: dict[str, Any]  # merged persona defaults + overrides


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

class PoolCreate(BaseModel):
    name: str
    backend_type: PoolBackendType
    size_mb: Optional[int] = None    # required for malloc
    aio_path: Optional[str] = None   # required for aio_file
    subsystem: Optional[str] = None  # subsystem name or id; None → use "default"


class PoolResponse(BaseModel):
    id: str
    name: str
    subsystem_id: str
    backend_type: PoolBackendType
    size_mb: Optional[int] = None
    aio_path: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

class VolumeCreate(BaseModel):
    name: str
    pool_id: str
    size_mb: int


class VolumeExtend(BaseModel):
    new_size_mb: int


class VolumeResponse(BaseModel):
    id: str
    name: str
    subsystem_id: str
    pool_id: str
    size_mb: int
    status: VolumeStatus
    bdev_name: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------

class HostCreate(BaseModel):
    name: str
    iqn: Optional[str] = None  # iSCSI initiator IQN
    nqn: Optional[str] = None  # NVMe-oF host NQN


class HostResponse(BaseModel):
    id: str
    name: str
    iqn: Optional[str] = None
    nqn: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

class MappingCreate(BaseModel):
    volume_id: str
    host_id: str
    protocol: Protocol


class MappingResponse(BaseModel):
    id: str
    subsystem_id: str
    volume_id: str
    host_id: str
    export_container_id: str
    protocol: Protocol
    lun_id: Optional[int] = None
    ns_id: Optional[int] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Connection info (OpenStack Cinder initialize_connection shapes)
# ---------------------------------------------------------------------------

class ConnectionInfoIscsi(BaseModel):
    driver_volume_type: str = "iscsi"
    data: dict[str, Any]


class ConnectionInfoNvmeof(BaseModel):
    driver_volume_type: str = "nvmeof"
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# Fault / delay injection
# ---------------------------------------------------------------------------

class FaultCreate(BaseModel):
    operation: str
    error_message: str


class DelayCreate(BaseModel):
    operation: str
    delay_seconds: float
