# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Pydantic request/response models for HPE 3PAR WSAPI.

JSON shapes match those expected by the ``hpe3par`` Cinder driver
(python-3parclient).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateCredentialRequest(BaseModel):
    """POST /api/v1/credentials — authenticate."""
    user: str = ""
    password: str = ""


class CreateVolumeRequest(BaseModel):
    """POST /api/v1/volumes — create a VV."""
    name: str
    cpg: str = Field(..., alias="cpg")
    size_mib: int = Field(..., alias="sizeMiB")
    tpvv: bool = True

    model_config = {"populate_by_name": True}


class GrowVolumeRequest(BaseModel):
    """PUT /api/v1/volumes/{name} — grow a VV (action=growvv)."""
    action: str = "growvv"
    size_mib: int = Field(..., alias="sizeMiB")

    model_config = {"populate_by_name": True}


class CreateHostRequest(BaseModel):
    """POST /api/v1/hosts — register a host."""
    name: str
    persona: int = 1
    i_scsi_names: list[str] = Field(default_factory=list, alias="iSCSINames")
    fc_wwns: list[str] = Field(default_factory=list, alias="FCWWNs")

    model_config = {"populate_by_name": True}


class ModifyHostRequest(BaseModel):
    """PUT /api/v1/hosts/{name} — modify a host (add paths)."""
    path_operation: int = Field(1, alias="pathOperation")
    i_scsi_names: list[str] = Field(default_factory=list, alias="iSCSINames")
    fc_wwns: list[str] = Field(default_factory=list, alias="FCWWNs")

    model_config = {"populate_by_name": True}


class CreateVlunRequest(BaseModel):
    """POST /api/v1/vluns — create a VLUN (mapping)."""
    volume_name: str = Field(..., alias="volumeName")
    hostname: str = ""
    lun: int = 0
    auto_lun: bool = Field(True, alias="autoLun")

    model_config = {"populate_by_name": True}
