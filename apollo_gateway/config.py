# FILE: apollo_gateway/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APOLLO_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./apollo_gateway.db"
    spdk_socket_path: str = "/var/tmp/spdk.sock"

    iscsi_portal_ip: str = "0.0.0.0"
    iscsi_portal_port: int = 3260

    nvmef_portal_ip: str = "0.0.0.0"
    nvmef_portal_port: int = 4420

    iqn_prefix: str = "iqn.2026-02.lunacysystems.apollo"
    nqn_prefix: str = "nqn.2026-02.io.lunacysystems:apollo"


settings = Settings()
