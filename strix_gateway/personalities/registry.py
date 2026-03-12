# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Personality app factory protocol and registry.

Vendor personalities register a factory that creates a self-contained
ASGI sub-application with vendor-native routes.  The
:class:`PersonalityDispatcher` middleware (in ``middleware/``) uses the
registry to dispatch requests based on the array's vendor type.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from starlette.types import ASGIApp

if TYPE_CHECKING:
    from strix_gateway.config import Settings

logger = logging.getLogger("strix_gateway.personalities.registry")


@runtime_checkable
class PersonalityAppFactory(Protocol):
    """Protocol that vendor personality modules implement."""

    def create_app(self, settings: "Settings") -> ASGIApp:
        """Build an ASGI sub-application with vendor-native routes."""
        ...


class PersonalityRegistry:
    """Maps vendor strings to :class:`PersonalityAppFactory` instances."""

    def __init__(self) -> None:
        self._factories: dict[str, PersonalityAppFactory] = {}

    def register(self, vendor: str, factory: PersonalityAppFactory) -> None:
        """Register a factory for *vendor*."""
        if vendor in self._factories:
            logger.warning("Overwriting personality factory for vendor=%s", vendor)
        self._factories[vendor] = factory
        logger.info("Registered personality factory: %s", vendor)

    def get(self, vendor: str) -> PersonalityAppFactory | None:
        """Return the factory for *vendor*, or ``None``."""
        return self._factories.get(vendor)

    def has(self, vendor: str) -> bool:
        """Return ``True`` if *vendor* has a registered factory."""
        return vendor in self._factories

    def vendors(self) -> list[str]:
        """Return list of registered vendor names."""
        return list(self._factories.keys())


#: Module-level singleton — import and use directly.
personality_registry = PersonalityRegistry()
