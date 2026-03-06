# FILE: apollo_gateway/tls/vhost.py
"""Virtual-host FQDN derivation and array-to-hostname registry."""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apollo_gateway.core.db import Array

logger = logging.getLogger("apollo_gateway.tls.vhost")

_DNS_LABEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def is_dns_safe(name: str) -> bool:
    """Return True if *name* is a valid DNS label."""
    return bool(_DNS_LABEL_RE.match(name))


def resolve_hostname(hostname_override: str = "") -> str:
    """Return the short hostname to use in FQDN construction."""
    if hostname_override:
        return hostname_override
    return socket.gethostname().split(".")[0]


def resolve_array_fqdn(
    array_name: str,
    domain: str,
    hostname_override: str = "",
) -> str:
    """Build the FQDN for an array: ``<array>.<hostname>.<domain>``."""
    hostname = resolve_hostname(hostname_override)
    return f"{array_name}.{hostname}.{domain}"


@dataclass(frozen=True)
class ArrayInfo:
    """Lightweight snapshot of an array for vhost lookup (not an ORM object)."""

    id: str
    name: str
    fqdn: str


class VhostRegistry:
    """Maps FQDN → ArrayInfo.  Rebuilt from DB on startup and on array CRUD."""

    def __init__(self, domain: str, hostname_override: str = ""):
        self._domain = domain
        self._hostname_override = hostname_override
        self._map: dict[str, ArrayInfo] = {}

    async def rebuild(self, session_factory: async_sessionmaker) -> None:
        """Query all arrays and rebuild the FQDN map."""
        async with session_factory() as session:
            result = await session.execute(select(Array))
            arrays = result.scalars().all()

        new_map: dict[str, ArrayInfo] = {}
        for arr in arrays:
            if not is_dns_safe(arr.name):
                logger.warning(
                    "Array '%s' has non-DNS-safe name, skipping vhost", arr.name
                )
                continue
            fqdn = resolve_array_fqdn(
                arr.name, self._domain, self._hostname_override
            )
            new_map[fqdn] = ArrayInfo(id=arr.id, name=arr.name, fqdn=fqdn)
        self._map = new_map
        logger.info("Vhost registry rebuilt: %d mapping(s)", len(self._map))

    def lookup(self, host: str) -> ArrayInfo | None:
        """Look up an array by hostname (case-insensitive)."""
        return self._map.get(host.lower())

    def all_mappings(self) -> dict[str, ArrayInfo]:
        """Return a copy of the current FQDN→ArrayInfo map."""
        return dict(self._map)

    def fqdn_for_name(self, array_name: str) -> str:
        """Derive the FQDN for an array name (does not query DB)."""
        return resolve_array_fqdn(
            array_name, self._domain, self._hostname_override
        )
