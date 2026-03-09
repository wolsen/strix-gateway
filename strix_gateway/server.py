# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Programmatic uvicorn launcher with TLS/SNI support.

Invoked as ``python -m strix_gateway.server`` when ``vhost_enabled`` is True.
This module bootstraps TLS assets *before* uvicorn starts so the SSLContext
(with SNI callback) can be passed directly to the server.
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from sqlalchemy import select

from strix_gateway.config import settings
from strix_gateway.core.db import Array, get_session_factory, init_db
from strix_gateway.tls.manager import TLSManager
from strix_gateway.tls.sni import SNIRouter
from strix_gateway.tls.vhost import resolve_array_fqdn

logger = logging.getLogger("strix_gateway.server")


async def _bootstrap_mappings() -> dict[str, str]:
    """Quick DB query to get ``{array_name: fqdn}`` for all arrays."""
    await init_db(settings.database_url)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(select(Array))
        arrays = result.scalars().all()
    return {
        arr.name: resolve_array_fqdn(
            arr.name, settings.vhost_domain, settings.vhost_hostname_override
        )
        for arr in arrays
    }


def main() -> None:
    """Bootstrap TLS, then start uvicorn with the SNI-enabled SSLContext."""
    if not settings.vhost_domain:
        raise SystemExit(
            "STRIX_VHOST_DOMAIN must be set when vhost mode is enabled"
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    # 1. Bootstrap DB and get array→FQDN mappings
    logger.info("Bootstrapping TLS for vhost mode (domain=%s)", settings.vhost_domain)
    mappings = asyncio.run(_bootstrap_mappings())
    logger.info("Found %d array(s): %s", len(mappings), list(mappings.keys()))

    # 2. Sync TLS assets (CA + leaf certs)
    mgr = TLSManager(settings.tls_dir, settings.tls_rotate_before_days)
    issued = mgr.sync_tls_assets(
        mappings,
        tls_mode=settings.tls_mode,
        hostname_override=settings.vhost_hostname_override,
        domain=settings.vhost_domain,
    )
    if issued:
        logger.info("Issued/rotated %d cert(s): %s", len(issued), issued)

    # 3. Build SSLContext with SNI
    if settings.tls_mode == "wildcard":
        # Wildcard: single cert, derive label
        import socket

        hostname = settings.vhost_hostname_override or socket.gethostname().split(".")[0]
        wildcard_label = mgr._wildcard_label(hostname, settings.vhost_domain)
        fqdns = [wildcard_label]
        default_fqdn = wildcard_label
    else:
        fqdns = list(mappings.values())
        default_fqdn = mappings.get("default")

    sni_router = SNIRouter(settings.tls_dir, default_fqdn)
    ssl_context = sni_router.build(fqdns)

    # Store manager and router so lifespan() can put them on app.state
    # We use module-level variables that main.py lifespan reads.
    import strix_gateway.server as _self

    _self._tls_manager = mgr  # type: ignore[attr-defined]
    _self._sni_router = sni_router  # type: ignore[attr-defined]

    # 4. Start uvicorn
    # Uvicorn requires ssl_certfile/ssl_keyfile to enable TLS mode.
    # We pass the default leaf cert files to satisfy this, then after
    # config.load() we replace config.ssl with our SNI-enabled context.
    default_key, default_crt = mgr.leaf_paths(default_fqdn or fqdns[0])
    logger.info(
        "Starting uvicorn on 0.0.0.0:%d (TLS enabled)", settings.bind_https_port
    )
    config = uvicorn.Config(
        "strix_gateway.main:app",
        host="0.0.0.0",
        port=settings.bind_https_port,
        ssl_keyfile=str(default_key),
        ssl_certfile=str(default_crt),
        log_level="info",
    )
    config.load()
    config.ssl = ssl_context
    server = uvicorn.Server(config)
    server.run()


# Allow ``python -m strix_gateway.server``
if __name__ == "__main__":
    main()
