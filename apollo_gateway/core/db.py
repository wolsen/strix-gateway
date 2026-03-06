# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""SQLAlchemy 2.x async ORM models and session management.

Breaking change (v0.2): Subsystem → Array, ExportContainer → TransportEndpoint,
Host stores initiator lists only, Mapping carries persona + underlay endpoints.
Delete the old SQLite DB before running.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

_engine = None
_session_factory: async_sessionmaker | None = None


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class Array(Base):
    """Represents a storage array (real or emulated)."""
    __tablename__ = "arrays"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    vendor: Mapped[str] = mapped_column(String, nullable=False, default="generic")
    # JSON-encoded dict of capability profile overrides (merged with vendor defaults at query time)
    profile: Mapped[str] = mapped_column(String, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    pools: Mapped[list["Pool"]] = relationship("Pool", back_populates="array", lazy="selectin")
    endpoints: Mapped[list["TransportEndpoint"]] = relationship(
        "TransportEndpoint", back_populates="array", lazy="selectin"
    )

    @property
    def profile_dict(self) -> dict[str, Any]:
        """Parsed profile as a Python dict."""
        if isinstance(self.profile, str):
            return json.loads(self.profile)
        return self.profile or {}


class TransportEndpoint(Base):
    """Array-owned target endpoint (iSCSI, NVMe-oF TCP, or FC)."""
    __tablename__ = "transport_endpoints"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    array_id: Mapped[str] = mapped_column(ForeignKey("arrays.id"), nullable=False)
    # "iscsi" | "nvmeof_tcp" | "fc"
    protocol: Mapped[str] = mapped_column(String, nullable=False)
    # JSON dict: iscsi→{"target_iqn":..}, nvmeof_tcp→{"subsystem_nqn":..}, fc→{"target_wwpns":[..]}
    targets: Mapped[str] = mapped_column(String, nullable=False, default="{}")
    # JSON dict: iscsi→{"portals":[..]}, nvmeof_tcp→{"listeners":[..]}, fc→{"labels":[..]} or omit
    addresses: Mapped[str] = mapped_column(String, nullable=False, default="{}")
    # JSON dict: optional auth config, v1 default {"method":"none"}
    auth: Mapped[str] = mapped_column(String, nullable=False, default='{"method":"none"}')
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    array: Mapped["Array"] = relationship("Array", back_populates="endpoints", lazy="selectin")

    @property
    def targets_dict(self) -> dict[str, Any]:
        """Parsed targets as a Python dict."""
        raw = json.loads(self.targets) if isinstance(self.targets, str) else self.targets
        if isinstance(raw, dict):
            return raw
        return {}

    @property
    def addresses_dict(self) -> dict[str, Any]:
        """Parsed addresses as a Python dict."""
        raw = json.loads(self.addresses) if isinstance(self.addresses, str) else self.addresses
        if isinstance(raw, dict):
            return raw
        return {}

    @property
    def auth_dict(self) -> dict[str, Any]:
        """Parsed auth as a Python dict."""
        raw = json.loads(self.auth) if isinstance(self.auth, str) else self.auth
        if isinstance(raw, dict):
            return raw
        return {}


class Pool(Base):
    __tablename__ = "pools"
    __table_args__ = (UniqueConstraint("array_id", "name", name="uq_pool_array_name"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    array_id: Mapped[str] = mapped_column(ForeignKey("arrays.id"), nullable=False)
    backend_type: Mapped[str] = mapped_column(String, nullable=False)
    size_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    aio_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    array: Mapped["Array"] = relationship("Array", back_populates="pools", lazy="selectin")
    volumes: Mapped[list["Volume"]] = relationship("Volume", back_populates="pool", lazy="selectin")

    @property
    def spdk_lvstore_name(self) -> str:
        """Globally unique lvstore name: '{array_name}.{pool_name}'."""
        return f"{self.array.name}.{self.name}"


class Volume(Base):
    __tablename__ = "volumes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    array_id: Mapped[str] = mapped_column(ForeignKey("arrays.id"), nullable=False)
    pool_id: Mapped[str] = mapped_column(ForeignKey("pools.id"), nullable=False)
    size_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="creating")
    bdev_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    pool: Mapped["Pool"] = relationship("Pool", back_populates="volumes", lazy="selectin")
    mappings: Mapped[list["Mapping"]] = relationship("Mapping", back_populates="volume", lazy="selectin")


class Host(Base):
    """Compute host — stores initiators only (no target info)."""
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # JSON-encoded lists
    initiators_iscsi_iqns: Mapped[str] = mapped_column(String, nullable=False, default="[]")
    initiators_nvme_host_nqns: Mapped[str] = mapped_column(String, nullable=False, default="[]")
    initiators_fc_wwpns: Mapped[str] = mapped_column(String, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    mappings: Mapped[list["Mapping"]] = relationship("Mapping", back_populates="host", lazy="selectin")

    @property
    def iscsi_iqns(self) -> list[str]:
        """Parsed iSCSI IQN list."""
        return json.loads(self.initiators_iscsi_iqns) if isinstance(self.initiators_iscsi_iqns, str) else self.initiators_iscsi_iqns or []

    @property
    def nvme_nqns(self) -> list[str]:
        """Parsed NVMe host NQN list."""
        return json.loads(self.initiators_nvme_host_nqns) if isinstance(self.initiators_nvme_host_nqns, str) else self.initiators_nvme_host_nqns or []

    @property
    def fc_wwpns(self) -> list[str]:
        """Parsed FC WWPN list."""
        return json.loads(self.initiators_fc_wwpns) if isinstance(self.initiators_fc_wwpns, str) else self.initiators_fc_wwpns or []


class Mapping(Base):
    """Attachment intent: links a volume to a host via persona + underlay endpoints."""
    __tablename__ = "mappings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    volume_id: Mapped[str] = mapped_column(ForeignKey("volumes.id"), nullable=False)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), nullable=False)
    persona_endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("transport_endpoints.id"), nullable=False
    )
    underlay_endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("transport_endpoints.id"), nullable=False
    )
    lun_id: Mapped[int] = mapped_column(Integer, nullable=False)
    underlay_id: Mapped[int] = mapped_column(Integer, nullable=False)
    desired_state: Mapped[str] = mapped_column(String, nullable=False, default="attached")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    volume: Mapped["Volume"] = relationship("Volume", back_populates="mappings", lazy="selectin")
    host: Mapped["Host"] = relationship("Host", back_populates="mappings", lazy="selectin")
    persona_endpoint: Mapped["TransportEndpoint"] = relationship(
        "TransportEndpoint", foreign_keys=[persona_endpoint_id], lazy="selectin"
    )
    underlay_endpoint: Mapped["TransportEndpoint"] = relationship(
        "TransportEndpoint", foreign_keys=[underlay_endpoint_id], lazy="selectin"
    )


# ---------------------------------------------------------------------------
# Engine + session lifecycle
# ---------------------------------------------------------------------------

async def init_db(database_url: str) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session_factory() -> async_sessionmaker:
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
