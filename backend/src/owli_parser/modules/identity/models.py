"""identity ORM 模型：users、user_sessions、tenants、memberships。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from owli_parser.infrastructure.columns import (
    created_at_column,
    pk_column,
    updated_at_column,
)
from owli_parser.infrastructure.db import Base


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = pk_column()
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(default=False, nullable=False)
    auth_revision: Mapped[int] = mapped_column(default=1, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class UserSessionRow(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = pk_column()
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    token_digest: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    digest_key_version: Mapped[int] = mapped_column(default=1, nullable=False)
    csrf_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    client_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)

    __table_args__ = (Index("ix_user_sessions_user", "user_id", "revoked_at"),)


class TenantRow(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = pk_column()
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class MembershipRow(Base):
    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # admin/editor/viewer
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    auth_revision: Mapped[int] = mapped_column(default=1, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
        Index("ix_memberships_user", "user_id", "status"),
    )
