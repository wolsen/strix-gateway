# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""SNI-based TLS certificate selection for uvicorn."""

from __future__ import annotations

import logging
import pathlib
import ssl

logger = logging.getLogger("apollo_gateway.tls.sni")


class SNIRouter:
    """Manages per-hostname SSLContexts and provides an SNI callback."""

    def __init__(self, tls_dir: str, default_fqdn: str | None = None):
        self.tls_dir = pathlib.Path(tls_dir)
        self.leaf_dir = self.tls_dir / "leaf"
        self._contexts: dict[str, ssl.SSLContext] = {}
        self._default_ctx: ssl.SSLContext | None = None
        self.default_fqdn = default_fqdn

    def _make_context(
        self, key_path: pathlib.Path, crt_path: pathlib.Path
    ) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(crt_path), str(key_path))
        ca_path = self.tls_dir / "ca.crt"
        if ca_path.exists():
            ctx.load_verify_locations(str(ca_path))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def build(self, fqdns: list[str]) -> ssl.SSLContext:
        """Build SSLContexts for all *fqdns* and return the primary context.

        The returned context has an ``sni_callback`` that switches to the
        correct per-hostname context during the TLS handshake.
        """
        self._contexts.clear()

        for fqdn in fqdns:
            key_path = self.leaf_dir / f"{fqdn}.key"
            crt_path = self.leaf_dir / f"{fqdn}.crt"
            if key_path.exists() and crt_path.exists():
                self._contexts[fqdn] = self._make_context(key_path, crt_path)

        # Choose default context
        if self.default_fqdn and self.default_fqdn in self._contexts:
            self._default_ctx = self._contexts[self.default_fqdn]
        elif self._contexts:
            self._default_ctx = next(iter(self._contexts.values()))
        else:
            raise RuntimeError(
                "No TLS leaf certificates found — cannot start HTTPS server"
            )

        # Build a fresh primary context that uses the SNI callback.
        # We need a separate primary context because set_servername_callback
        # is set on the context used by the server socket.
        if self.default_fqdn and self.default_fqdn in self._contexts:
            default_key = self.leaf_dir / f"{self.default_fqdn}.key"
            default_crt = self.leaf_dir / f"{self.default_fqdn}.crt"
        else:
            first_fqdn = next(iter(self._contexts))
            default_key = self.leaf_dir / f"{first_fqdn}.key"
            default_crt = self.leaf_dir / f"{first_fqdn}.crt"

        primary = self._make_context(default_key, default_crt)
        primary.sni_callback = self._sni_callback  # type: ignore[attr-defined]

        logger.info(
            "SNI router built with %d context(s): %s",
            len(self._contexts),
            list(self._contexts.keys()),
        )
        return primary

    def _sni_callback(
        self,
        ssl_socket: ssl.SSLObject,
        server_name: str | None,
        ssl_context: ssl.SSLContext,
    ) -> int | None:
        """Called by OpenSSL during TLS handshake with the client's SNI hostname."""
        if server_name is not None:
            ctx = self._contexts.get(server_name)
            if ctx is not None:
                ssl_socket.context = ctx  # type: ignore[attr-defined]
            else:
                logger.debug("SNI: no context for '%s', using default", server_name)
        return None  # continue handshake

    def reload(self, fqdns: list[str]) -> ssl.SSLContext:
        """Hot-reload contexts after cert re-sync.  Returns new primary context."""
        return self.build(fqdns)
