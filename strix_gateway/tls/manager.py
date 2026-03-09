# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Internal CA and leaf certificate management for vhost TLS."""

from __future__ import annotations

import datetime
import logging
import pathlib
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

logger = logging.getLogger("strix_gateway.tls.manager")


class TLSManager:
    """Create and rotate an internal CA and per-subsystem leaf certificates."""

    def __init__(self, tls_dir: str, rotate_before_days: int = 30):
        self.tls_dir = pathlib.Path(tls_dir)
        self.leaf_dir = self.tls_dir / "leaf"
        self.rotate_before_days = rotate_before_days
        self._ca_key: ec.EllipticCurvePrivateKey | None = None
        self._ca_cert: x509.Certificate | None = None

    # ------------------------------------------------------------------
    # CA
    # ------------------------------------------------------------------

    @property
    def ca_key_path(self) -> pathlib.Path:
        return self.tls_dir / "ca.key"

    @property
    def ca_crt_path(self) -> pathlib.Path:
        return self.tls_dir / "ca.crt"

    def ensure_ca(self) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        """Load or create the internal CA.  Returns ``(private_key, certificate)``."""
        if self.ca_key_path.exists() and self.ca_crt_path.exists():
            self._ca_key = serialization.load_pem_private_key(
                self.ca_key_path.read_bytes(), password=None
            )
            self._ca_cert = x509.load_pem_x509_certificate(
                self.ca_crt_path.read_bytes()
            )
            logger.debug("Loaded existing CA from %s", self.tls_dir)
        else:
            self.tls_dir.mkdir(parents=True, exist_ok=True)
            key = ec.generate_private_key(ec.SECP256R1())
            now = datetime.datetime.now(datetime.timezone.utc)
            subject = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "Apollo Gateway Internal CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lunacy Systems"),
            ])
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now)
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(
                    x509.BasicConstraints(ca=True, path_length=0), critical=True
                )
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True,
                        key_cert_sign=True,
                        crl_sign=True,
                        content_commitment=False,
                        key_encipherment=False,
                        data_encipherment=False,
                        key_agreement=False,
                        encipher_only=False,
                        decipher_only=False,
                    ),
                    critical=True,
                )
                .sign(key, hashes.SHA256())
            )
            self.ca_key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            self.ca_key_path.chmod(0o600)
            self.ca_crt_path.write_bytes(
                cert.public_bytes(serialization.Encoding.PEM)
            )
            self._ca_key = key
            self._ca_cert = cert
            logger.info("Created new internal CA at %s", self.tls_dir)

        return self._ca_key, self._ca_cert  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Leaf certificates
    # ------------------------------------------------------------------

    def issue_leaf(
        self, fqdn: str, san_names: list[str]
    ) -> tuple[pathlib.Path, pathlib.Path]:
        """Issue (or re-issue) a leaf cert for *fqdn* with given SANs."""
        if self._ca_key is None or self._ca_cert is None:
            raise RuntimeError("CA not initialised — call ensure_ca() first")

        self.leaf_dir.mkdir(parents=True, exist_ok=True)
        key_path = self.leaf_dir / f"{fqdn}.key"
        crt_path = self.leaf_dir / f"{fqdn}.crt"

        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.timezone.utc)
        sans = [x509.DNSName(n) for n in san_names]

        cert = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, fqdn)])
            )
            .issuer_name(self._ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName(sans), critical=False
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .sign(self._ca_key, hashes.SHA256())
        )

        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_path.chmod(0o600)
        crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        logger.info("Issued leaf cert for %s (SANs: %s)", fqdn, san_names)
        return key_path, crt_path

    def needs_reissue(self, fqdn: str, expected_sans: list[str]) -> bool:
        """Check if a leaf cert needs reissue (missing, expiring, or SANs changed)."""
        crt_path = self.leaf_dir / f"{fqdn}.crt"
        key_path = self.leaf_dir / f"{fqdn}.key"
        if not crt_path.exists() or not key_path.exists():
            return True

        cert = x509.load_pem_x509_certificate(crt_path.read_bytes())

        # Expiry check
        now = datetime.datetime.now(datetime.timezone.utc)
        remaining = cert.not_valid_after_utc - now
        if remaining.days < self.rotate_before_days:
            return True

        # SAN check
        try:
            ext = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            )
            current_sans = set(ext.value.get_values_for_type(x509.DNSName))
        except x509.ExtensionNotFound:
            current_sans = set()
        return current_sans != set(expected_sans)

    # ------------------------------------------------------------------
    # Wildcard helpers
    # ------------------------------------------------------------------

    def _wildcard_label(self, hostname: str, domain: str) -> str:
        return f"_wildcard.{hostname}.{domain}"

    def _wildcard_san(self, hostname: str, domain: str) -> list[str]:
        return [f"*.{hostname}.{domain}", f"{hostname}.{domain}"]

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync_tls_assets(
        self,
        subsystem_fqdns: dict[str, str],
        *,
        tls_mode: str = "per-subsystem",
        hostname_override: str = "",
        domain: str = "",
    ) -> list[str]:
        """Sync TLS certs for all subsystems.

        Parameters
        ----------
        subsystem_fqdns:
            ``{subsystem_name: fqdn}``
        tls_mode:
            ``"per-subsystem"`` or ``"wildcard"``
        hostname_override:
            Override for the hostname component.
        domain:
            The configured domain (needed for wildcard mode).

        Returns
        -------
        list[str]
            FQDNs (or wildcard label) that were (re)issued.
        """
        self.ensure_ca()
        issued: list[str] = []

        if tls_mode == "wildcard":
            hostname = hostname_override or socket.gethostname().split(".")[0]
            label = self._wildcard_label(hostname, domain)
            sans = self._wildcard_san(hostname, domain)
            if self.needs_reissue(label, sans):
                self.issue_leaf(label, sans)
                issued.append(label)
            return issued

        # per-subsystem mode
        for _sub_name, fqdn in subsystem_fqdns.items():
            sans = [fqdn]
            if self.needs_reissue(fqdn, sans):
                self.issue_leaf(fqdn, sans)
                issued.append(fqdn)

        return issued

    def leaf_paths(self, fqdn: str) -> tuple[pathlib.Path, pathlib.Path]:
        """Return ``(key_path, crt_path)`` for a leaf cert."""
        return self.leaf_dir / f"{fqdn}.key", self.leaf_dir / f"{fqdn}.crt"
