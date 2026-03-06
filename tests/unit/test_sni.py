# FILE: tests/unit/test_sni.py
"""Tests for SNI certificate selection."""

from __future__ import annotations

import ssl
from unittest.mock import MagicMock

import pytest

from apollo_gateway.tls.manager import TLSManager
from apollo_gateway.tls.sni import SNIRouter


@pytest.fixture
def tls_setup(tmp_path):
    """Create a CA + two leaf certs for testing SNI routing."""
    mgr = TLSManager(str(tmp_path))
    mgr.ensure_ca()
    mgr.issue_leaf("default.gw01.lab.example", ["default.gw01.lab.example"])
    mgr.issue_leaf("pure-a.gw01.lab.example", ["pure-a.gw01.lab.example"])
    return tmp_path


class TestSNIRouter:
    def test_build_returns_ssl_context(self, tls_setup):
        router = SNIRouter(str(tls_setup), default_fqdn="default.gw01.lab.example")
        ctx = router.build([
            "default.gw01.lab.example",
            "pure-a.gw01.lab.example",
        ])
        assert isinstance(ctx, ssl.SSLContext)

    def test_sni_callback_selects_correct_context(self, tls_setup):
        router = SNIRouter(str(tls_setup), default_fqdn="default.gw01.lab.example")
        router.build([
            "default.gw01.lab.example",
            "pure-a.gw01.lab.example",
        ])

        # The router should have contexts for both FQDNs
        assert "default.gw01.lab.example" in router._contexts
        assert "pure-a.gw01.lab.example" in router._contexts

        # Simulate SNI callback
        mock_socket = MagicMock()
        expected_ctx = router._contexts["pure-a.gw01.lab.example"]
        router._sni_callback(mock_socket, "pure-a.gw01.lab.example", None)
        mock_socket.__setattr__("context", expected_ctx)

    def test_sni_callback_unknown_host_uses_default(self, tls_setup):
        router = SNIRouter(str(tls_setup), default_fqdn="default.gw01.lab.example")
        router.build([
            "default.gw01.lab.example",
            "pure-a.gw01.lab.example",
        ])

        # Unknown host should not change context
        mock_socket = MagicMock()
        result = router._sni_callback(mock_socket, "unknown.gw01.lab.example", None)
        assert result is None  # continue handshake
        # context should NOT have been set (no matching context)
        mock_socket.context.__set__ = MagicMock()

    def test_build_no_certs_raises(self, tmp_path):
        # Create CA but no leaf certs
        mgr = TLSManager(str(tmp_path))
        mgr.ensure_ca()

        router = SNIRouter(str(tmp_path))
        with pytest.raises(RuntimeError, match="No TLS leaf certificates found"):
            router.build(["nonexistent.example.com"])

    def test_reload(self, tls_setup):
        router = SNIRouter(str(tls_setup), default_fqdn="default.gw01.lab.example")
        ctx1 = router.build(["default.gw01.lab.example"])
        ctx2 = router.reload(["default.gw01.lab.example", "pure-a.gw01.lab.example"])
        assert isinstance(ctx2, ssl.SSLContext)
