# FILE: apollo_gateway/core/faults.py
"""In-memory fault and delay injection engine.

Faults and delays are stored in module-level dicts keyed by operation name.
Call ``check_fault(operation)`` at the top of any handler that should be
injectable — it will sleep for any configured delay then raise
``FaultInjectionError`` if a fault is registered.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("apollo_gateway.faults")

_faults: dict[str, str] = {}
_delays: dict[str, float] = {}


class FaultInjectionError(Exception):
    """Raised when a fault has been injected for the given operation."""


def inject_fault(operation: str, error_message: str) -> None:
    logger.warning("Injecting fault for operation=%s: %s", operation, error_message)
    _faults[operation] = error_message


def clear_fault(operation: str) -> None:
    _faults.pop(operation, None)
    logger.info("Cleared fault for operation=%s", operation)


def inject_delay(operation: str, delay_seconds: float) -> None:
    logger.warning("Injecting delay for operation=%s: %.2fs", operation, delay_seconds)
    _delays[operation] = delay_seconds


def clear_delay(operation: str) -> None:
    _delays.pop(operation, None)
    logger.info("Cleared delay for operation=%s", operation)


async def check_fault(operation: str) -> None:
    """Sleep for any configured delay then raise FaultInjectionError if set."""
    if operation in _delays:
        logger.debug("Delaying operation=%s by %.2fs", operation, _delays[operation])
        await asyncio.sleep(_delays[operation])
    if operation in _faults:
        msg = _faults[operation]
        logger.info("Fault triggered for operation=%s: %s", operation, msg)
        raise FaultInjectionError(msg)


def list_faults() -> dict[str, str]:
    return dict(_faults)


def list_delays() -> dict[str, float]:
    return dict(_delays)
