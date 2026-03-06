# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Admin routes for fault and delay injection."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from apollo_gateway.core import faults as fault_engine
from apollo_gateway.core.models import DelayCreate, FaultCreate

logger = logging.getLogger("apollo_gateway.api.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/faults", status_code=status.HTTP_201_CREATED)
async def inject_fault(body: FaultCreate):
    fault_engine.inject_fault(body.operation, body.error_message)
    return {"operation": body.operation, "error_message": body.error_message}


@router.delete("/faults/{operation}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_fault(operation: str):
    fault_engine.clear_fault(operation)


@router.get("/faults")
async def list_faults():
    return fault_engine.list_faults()


@router.post("/delays", status_code=status.HTTP_201_CREATED)
async def inject_delay(body: DelayCreate):
    fault_engine.inject_delay(body.operation, body.delay_seconds)
    return {"operation": body.operation, "delay_seconds": body.delay_seconds}


@router.delete("/delays/{operation}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_delay(operation: str):
    fault_engine.clear_delay(operation)


@router.get("/delays")
async def list_delays():
    return fault_engine.list_delays()
