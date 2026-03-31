# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STRIX_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./strix_gateway.db"
    spdk_socket_path: str = "/var/tmp/spdk.sock"

    iscsi_portal_ip: str = "0.0.0.0"
    iscsi_portal_port: int = 3260
    iscsi_underlay_lun_base: int = 0

    nvmef_portal_ip: str = "0.0.0.0"
    nvmef_portal_port: int = 4420

    iqn_prefix: str = "iqn.2026-02.lunacysystems.strix"
    nqn_prefix: str = "nqn.2026-02.io.lunacysystems:strix"

    api_base_url: str = "http://localhost:8080"

    # Vhost multiplexing
    vhost_enabled: bool = False
    vhost_domain: str = ""
    vhost_hostname_override: str = ""
    vhost_require_match: bool = True

    # TLS
    tls_mode: str = "per-array"
    tls_rotate_before_days: int = 30
    tls_dir: str = "./tls"

    # Bind (used by server.py when vhost_enabled)
    bind_https_port: int = 443
    bind_http_port: int = 0


settings = Settings()
