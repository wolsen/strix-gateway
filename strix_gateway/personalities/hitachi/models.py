# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Pydantic request/response models for Hitachi Configuration Manager API.

These mirror the JSON shapes that the Cinder Hitachi VSP driver expects.
Envelopes use the ``{"data": [...]}`` pattern.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field


_CAPACITY_RE = re.compile(r"^\s*(\d+)\s*([KMGT]?)(?:B)?\s*$", re.IGNORECASE)


def _parse_capacity_bytes(value: str) -> int:
    """Parse Hitachi capacity strings (e.g. "1073741824", "1G") to bytes."""
    match = _CAPACITY_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"Invalid capacity format: {value!r}")

    amount = int(match.group(1))
    unit = match.group(2).upper()
    multipliers = {
        "": 1,
        "K": 1024,
        "M": 1024 ** 2,
        "G": 1024 ** 3,
        "T": 1024 ** 4,
    }
    return amount * multipliers[unit]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    """POST /sessions — currently no credential validation."""
    pass


class CreateLdevRequest(BaseModel):
    """POST /ldevs — create an LDEV (volume)."""
    pool_id: int = Field(..., alias="poolId")
    byte_format_capacity: str = Field(..., alias="byteFormatCapacity")
    ldev_number: int | None = Field(None, alias="ldevNumber")
    label: str | None = None

    model_config = {"populate_by_name": True}

    @property
    def size_bytes(self) -> int:
        """Parse ``byteFormatCapacity`` (e.g. ``"1073741824"`` or ``"1G"``)."""
        return _parse_capacity_bytes(self.byte_format_capacity)


class ExpandLdevParameters(BaseModel):
    additional_byte_format_capacity: str = Field(
        ..., alias="additionalByteFormatCapacity"
    )

    model_config = {"populate_by_name": True}

    @property
    def additional_bytes(self) -> int:
        return _parse_capacity_bytes(self.additional_byte_format_capacity)


class ExpandLdevRequest(BaseModel):
    """PUT /ldevs/{ldevId}/actions/expand/invoke."""
    parameters: ExpandLdevParameters

    model_config = {"populate_by_name": True}


class ModifyLdevRequest(BaseModel):
    """PUT /ldevs/{ldevId} — update mutable LDEV attributes."""
    label: str | None = None

    model_config = {"populate_by_name": True}


class CreateHostGroupRequest(BaseModel):
    """POST /host-groups."""
    port_id: str = Field(..., alias="portId")
    host_group_name: str = Field(..., alias="hostGroupName")
    host_mode: str | None = Field("LINUX/IRIX", alias="hostMode")
    iscsi_name: str | None = Field(None, alias="iscsiName")

    model_config = {"populate_by_name": True}


class AddWwnRequest(BaseModel):
    """POST /host-groups/{id}/wwns."""
    host_wwn: str = Field(..., alias="hostWwn")

    model_config = {"populate_by_name": True}


class CreateIscsiTargetRequest(BaseModel):
    """POST /iscsi-targets."""
    port_id: str = Field(..., alias="portId")
    iscsi_target_name: str = Field(..., alias="iscsiTargetName")
    host_mode: str | None = Field("LINUX/IRIX", alias="hostMode")

    model_config = {"populate_by_name": True}


class AddIscsiNameRequest(BaseModel):
    """POST /iscsi-targets/{id}/iscsi-names."""
    iscsi_name: str = Field(..., alias="iscsiName")

    model_config = {"populate_by_name": True}


class AddHostIscsiRequest(BaseModel):
    """POST /host-iscsis."""
    iscsi_name: str = Field(..., alias="iscsiName")
    port_id: str = Field(..., alias="portId")
    host_group_number: int = Field(..., alias="hostGroupNumber")

    model_config = {"populate_by_name": True}


class CreateLunRequest(BaseModel):
    """POST /luns."""
    port_id: str = Field(..., alias="portId")
    host_group_number: int = Field(..., alias="hostGroupNumber")
    ldev_id: int = Field(..., alias="ldevId")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

class DataEnvelope(BaseModel):
    """Standard Hitachi list envelope: ``{"data": [...]}``."""
    data: list


class JobResponse(BaseModel):
    """GET /jobs/{jobId}."""
    job_id: int = Field(..., alias="jobId")
    status: str
    state: str
    affected_resources: list[str] = Field(default_factory=list, alias="affectedResources")
    error_resource: str | None = Field(None, alias="errorResource")

    model_config = {"populate_by_name": True}
