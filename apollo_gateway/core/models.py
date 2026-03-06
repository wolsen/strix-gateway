# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Pydantic request/response schemas and domain enums.

Breaking change (v0.2): Subsystem → Array, ExportContainer → TransportEndpoint,
Host stores initiator lists, Mapping carries persona + underlay endpoints.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, field_validator


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
    fc = "fc"


class DesiredState(str, Enum):
    attached = "attached"
    detached = "detached"


# ---------------------------------------------------------------------------
# Array (formerly Subsystem)
# ---------------------------------------------------------------------------

_DNS_LABEL_RE = __import__("re").compile(r"^[a-z][a-z0-9-]{0,62}$")


class ArrayCreate(BaseModel):
    name: str
    vendor: str = "generic"
    profile: dict[str, Any] = {}

    @field_validator("name")
    @classmethod
    def validate_dns_safe_name(cls, v: str) -> str:
        if not _DNS_LABEL_RE.match(v):
            raise ValueError(
                "Array name must be DNS-label-safe: lowercase letters, "
                "digits, hyphens; start with letter; max 63 chars"
            )
        return v


class ArrayView(BaseModel):
    id: str
    name: str
    vendor: str
    profile: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CapabilitiesView(BaseModel):
    array_id: str
    array_name: str
    vendor: str
    effective_profile: dict[str, Any]


# ---------------------------------------------------------------------------
# Transport Endpoint
# ---------------------------------------------------------------------------

class TransportEndpointCreate(BaseModel):
    protocol: Protocol
    targets: dict[str, Any]
    addresses: dict[str, Any] = {}
    auth: dict[str, Any] = {"method": "none"}


class TransportEndpointView(BaseModel):
    id: str
    array_id: str
    protocol: Protocol
    targets: dict[str, Any]
    addresses: dict[str, Any]
    auth: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

class PoolCreate(BaseModel):
    name: str
    backend_type: PoolBackendType
    size_mb: Optional[int] = None    # required for malloc
    aio_path: Optional[str] = None   # required for aio_file


class PoolResponse(BaseModel):
    id: str
    name: str
    array_id: str
    backend_type: PoolBackendType
    size_mb: Optional[int] = None
    aio_path: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Volume (API exposes size_gb, DB stores size_mb)
# ---------------------------------------------------------------------------

class VolumeCreate(BaseModel):
    name: str
    pool_id: str
    size_gb: int


class VolumeExtend(BaseModel):
    new_size_gb: int


class VolumeResponse(BaseModel):
    id: str
    name: str
    array_id: str
    pool_id: str
    size_gb: int
    status: VolumeStatus
    bdev_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_volume(cls, vol) -> "VolumeResponse":
        """Convert a DB Volume (which stores size_mb) to an API response (size_gb)."""
        return cls(
            id=vol.id,
            name=vol.name,
            array_id=vol.array_id,
            pool_id=vol.pool_id,
            size_gb=vol.size_mb // 1024 if vol.size_mb else 0,
            status=VolumeStatus(vol.status),
            bdev_name=vol.bdev_name,
            created_at=vol.created_at,
            updated_at=vol.updated_at,
        )


# ---------------------------------------------------------------------------
# Host (initiators only)
# ---------------------------------------------------------------------------

class HostCreate(BaseModel):
    name: str
    initiators_iscsi_iqns: list[str] = []
    initiators_nvme_host_nqns: list[str] = []
    initiators_fc_wwpns: list[str] = []


class HostUpdate(BaseModel):
    initiators_iscsi_iqns: Optional[list[str]] = None
    initiators_nvme_host_nqns: Optional[list[str]] = None
    initiators_fc_wwpns: Optional[list[str]] = None


class HostResponse(BaseModel):
    id: str
    name: str
    initiators_iscsi_iqns: list[str] = []
    initiators_nvme_host_nqns: list[str] = []
    initiators_fc_wwpns: list[str] = []
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_host(cls, host) -> "HostResponse":
        import json
        return cls(
            id=host.id,
            name=host.name,
            initiators_iscsi_iqns=json.loads(host.initiators_iscsi_iqns),
            initiators_nvme_host_nqns=json.loads(host.initiators_nvme_host_nqns),
            initiators_fc_wwpns=json.loads(host.initiators_fc_wwpns),
            created_at=host.created_at,
        )


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

class MappingCreate(BaseModel):
    """Create a mapping.

    Either provide explicit endpoint IDs (persona_endpoint_id +
    underlay_endpoint_id) *or* protocol selectors (persona_protocol +
    underlay_protocol) and the server will pick endpoints from the volume's
    array.
    """
    host_id: str
    volume_id: str
    # Explicit endpoint IDs (option A)
    persona_endpoint_id: Optional[str] = None
    underlay_endpoint_id: Optional[str] = None
    # Protocol selectors (option B — server picks endpoints)
    persona_protocol: Optional[Protocol] = None
    underlay_protocol: Optional[Protocol] = None


class MappingResponse(BaseModel):
    id: str
    volume_id: str
    host_id: str
    persona_endpoint_id: str
    underlay_endpoint_id: str
    lun_id: int
    underlay_id: int
    desired_state: DesiredState
    revision: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Attachments view (compute-side agent polling payload)
# ---------------------------------------------------------------------------

class AttachmentPersona(BaseModel):
    protocol: str
    target_wwpns: list[str] = []
    lun_id: int


class AttachmentUnderlay(BaseModel):
    protocol: str
    targets: dict[str, Any]
    addresses: dict[str, Any]
    auth: dict[str, Any]
    target_lun: Optional[int] = None
    nsid: Optional[int] = None


class AttachmentView(BaseModel):
    attachment_id: str
    volume_id: str
    array_id: str
    revision: int
    desired_state: str
    persona: AttachmentPersona
    underlay: AttachmentUnderlay


class AttachmentsResponse(BaseModel):
    host_id: str
    generated_at: datetime
    attachments: list[AttachmentView]


# ---------------------------------------------------------------------------
# SVC run (SSH shell -> remote API execution)
# ---------------------------------------------------------------------------

class SvcRunRequest(BaseModel):
    array: str
    command: str
    remote_user: str | None = None
    remote_addr: str | None = None
    remote_port: str | None = None


class SvcRunResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


# ---------------------------------------------------------------------------
# Fault / delay injection
# ---------------------------------------------------------------------------

class FaultCreate(BaseModel):
    operation: str
    error_message: str


class DelayCreate(BaseModel):
    operation: str
    delay_seconds: float


