# FILE: apollo_gateway/core/db.py
"""SQLAlchemy 2.x async ORM models and session management."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import AsyncGenerator, Optional

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

class Subsystem(Base):
    __tablename__ = "subsystems"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    persona: Mapped[str] = mapped_column(String, nullable=False, default="generic")
    # JSON-encoded list of strings, e.g. '["iscsi","nvmeof_tcp"]'
    protocols_enabled: Mapped[str] = mapped_column(
        String, nullable=False, default='["iscsi","nvmeof_tcp"]'
    )
    # JSON-encoded dict of capability profile overrides (merged with persona defaults at query time)
    capability_profile: Mapped[str] = mapped_column(String, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    pools: Mapped[list[Pool]] = relationship("Pool", back_populates="subsystem", lazy="selectin")


class Pool(Base):
    __tablename__ = "pools"
    __table_args__ = (UniqueConstraint("subsystem_id", "name", name="uq_pool_subsystem_name"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    subsystem_id: Mapped[str] = mapped_column(ForeignKey("subsystems.id"), nullable=False)
    backend_type: Mapped[str] = mapped_column(String, nullable=False)
    size_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    aio_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    subsystem: Mapped[Subsystem] = relationship("Subsystem", back_populates="pools", lazy="selectin")
    volumes: Mapped[list[Volume]] = relationship("Volume", back_populates="pool", lazy="selectin")


class Volume(Base):
    __tablename__ = "volumes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    subsystem_id: Mapped[str] = mapped_column(ForeignKey("subsystems.id"), nullable=False)
    pool_id: Mapped[str] = mapped_column(ForeignKey("pools.id"), nullable=False)
    size_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="creating")
    bdev_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    pool: Mapped[Pool] = relationship("Pool", back_populates="volumes", lazy="selectin")
    mappings: Mapped[list[Mapping]] = relationship("Mapping", back_populates="volume", lazy="selectin")


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    iqn: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    nqn: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    mappings: Mapped[list[Mapping]] = relationship("Mapping", back_populates="host", lazy="selectin")


class ExportContainer(Base):
    __tablename__ = "export_containers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    subsystem_id: Mapped[str] = mapped_column(ForeignKey("subsystems.id"), nullable=False)
    protocol: Mapped[str] = mapped_column(String, nullable=False)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), nullable=False)
    target_iqn: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    target_nqn: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    portal_ip: Mapped[str] = mapped_column(String, nullable=False)
    portal_port: Mapped[int] = mapped_column(Integer, nullable=False)

    mappings: Mapped[list[Mapping]] = relationship("Mapping", back_populates="export_container", lazy="selectin")


class Mapping(Base):
    __tablename__ = "mappings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    subsystem_id: Mapped[str] = mapped_column(ForeignKey("subsystems.id"), nullable=False)
    volume_id: Mapped[str] = mapped_column(ForeignKey("volumes.id"), nullable=False)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), nullable=False)
    export_container_id: Mapped[str] = mapped_column(ForeignKey("export_containers.id"), nullable=False)
    protocol: Mapped[str] = mapped_column(String, nullable=False)
    lun_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ns_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    volume: Mapped[Volume] = relationship("Volume", back_populates="mappings", lazy="selectin")
    host: Mapped[Host] = relationship("Host", lazy="selectin")
    export_container: Mapped[ExportContainer] = relationship(
        "ExportContainer", back_populates="mappings", lazy="selectin"
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
