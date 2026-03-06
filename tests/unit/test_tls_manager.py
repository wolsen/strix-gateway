# FILE: tests/unit/test_tls_manager.py
"""Tests for the TLS certificate manager (CA + leaf cert lifecycle)."""

from __future__ import annotations

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization

from apollo_gateway.tls.manager import TLSManager


class TestEnsureCA:
    def test_creates_new_ca(self, tmp_path):
        mgr = TLSManager(str(tmp_path))
        key, cert = mgr.ensure_ca()

        assert isinstance(key, ec.EllipticCurvePrivateKey)
        assert cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value == (
            "Apollo Gateway Internal CA"
        )
        assert (tmp_path / "ca.key").exists()
        assert (tmp_path / "ca.crt").exists()

        # Key file should have restrictive permissions
        assert (tmp_path / "ca.key").stat().st_mode & 0o777 == 0o600

        # CA cert should be self-signed
        assert cert.issuer == cert.subject

        # BasicConstraints: CA=True
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is True

    def test_loads_existing_ca(self, tmp_path):
        mgr = TLSManager(str(tmp_path))
        _key1, cert1 = mgr.ensure_ca()
        serial1 = cert1.serial_number

        # Second call should load, not regenerate
        mgr2 = TLSManager(str(tmp_path))
        _key2, cert2 = mgr2.ensure_ca()
        assert cert2.serial_number == serial1


class TestIssueLeaf:
    def test_issues_leaf_with_sans(self, tmp_path):
        mgr = TLSManager(str(tmp_path))
        mgr.ensure_ca()

        key_path, crt_path = mgr.issue_leaf(
            "pure-a.gw01.lab.example",
            ["pure-a.gw01.lab.example"],
        )
        assert key_path.exists()
        assert crt_path.exists()
        assert key_path.stat().st_mode & 0o777 == 0o600

        # Load and verify
        cert = x509.load_pem_x509_certificate(crt_path.read_bytes())
        cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        assert cn == "pure-a.gw01.lab.example"

        # Check SANs
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san_ext.value.get_values_for_type(x509.DNSName)
        assert "pure-a.gw01.lab.example" in dns_names

        # Signed by our CA
        assert cert.issuer == mgr._ca_cert.subject

        # Not a CA
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is False

    def test_issues_leaf_with_multiple_sans(self, tmp_path):
        mgr = TLSManager(str(tmp_path))
        mgr.ensure_ca()

        sans = ["pure-a.gw01.lab.example", "pure-a.gw01"]
        mgr.issue_leaf("pure-a.gw01.lab.example", sans)

        cert = x509.load_pem_x509_certificate(
            (tmp_path / "leaf" / "pure-a.gw01.lab.example.crt").read_bytes()
        )
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = set(san_ext.value.get_values_for_type(x509.DNSName))
        assert dns_names == set(sans)


class TestNeedsReissue:
    def test_missing_cert(self, tmp_path):
        mgr = TLSManager(str(tmp_path))
        mgr.ensure_ca()
        assert mgr.needs_reissue("nonexistent.example.com", ["nonexistent.example.com"])

    def test_san_mismatch(self, tmp_path):
        mgr = TLSManager(str(tmp_path))
        mgr.ensure_ca()

        fqdn = "test.gw01.lab.example"
        mgr.issue_leaf(fqdn, [fqdn])
        # SANs changed
        assert mgr.needs_reissue(fqdn, [fqdn, "alias.gw01.lab.example"])

    def test_valid_cert_no_reissue(self, tmp_path):
        mgr = TLSManager(str(tmp_path), rotate_before_days=30)
        mgr.ensure_ca()

        fqdn = "test.gw01.lab.example"
        mgr.issue_leaf(fqdn, [fqdn])
        assert not mgr.needs_reissue(fqdn, [fqdn])

    def test_expiring_cert(self, tmp_path):
        mgr = TLSManager(str(tmp_path), rotate_before_days=400)
        mgr.ensure_ca()

        fqdn = "test.gw01.lab.example"
        mgr.issue_leaf(fqdn, [fqdn])
        # Cert has 365-day validity, rotate_before_days=400 → needs reissue
        assert mgr.needs_reissue(fqdn, [fqdn])


class TestSyncTlsAssets:
    def test_sync_creates_all_certs(self, tmp_path):
        mgr = TLSManager(str(tmp_path))
        mappings = {
            "default": "default.gw01.lab.example",
            "pure-a": "pure-a.gw01.lab.example",
        }
        issued = mgr.sync_tls_assets(mappings)
        assert len(issued) == 2
        assert (tmp_path / "leaf" / "default.gw01.lab.example.crt").exists()
        assert (tmp_path / "leaf" / "pure-a.gw01.lab.example.crt").exists()

    def test_sync_idempotent(self, tmp_path):
        mgr = TLSManager(str(tmp_path))
        mappings = {"default": "default.gw01.lab.example"}
        mgr.sync_tls_assets(mappings)
        # Second sync should not reissue
        issued = mgr.sync_tls_assets(mappings)
        assert len(issued) == 0

    def test_sync_wildcard_mode(self, tmp_path):
        mgr = TLSManager(str(tmp_path))
        mappings = {"default": "default.gw01.lab.example"}
        issued = mgr.sync_tls_assets(
            mappings,
            tls_mode="wildcard",
            hostname_override="gw01",
            domain="lab.example",
        )
        assert len(issued) == 1
        label = issued[0]
        assert label == "_wildcard.gw01.lab.example"
        assert (tmp_path / "leaf" / f"{label}.crt").exists()

        # Verify wildcard SAN
        cert = x509.load_pem_x509_certificate(
            (tmp_path / "leaf" / f"{label}.crt").read_bytes()
        )
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = set(san_ext.value.get_values_for_type(x509.DNSName))
        assert "*.gw01.lab.example" in dns_names
        assert "gw01.lab.example" in dns_names
