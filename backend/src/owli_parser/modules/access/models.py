"""access ORM 模型：applications、application_tokens、application_grants。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from owli_parser.infrastructure.columns import (
    created_at_column,
    json_column,
    pk_column,
    updated_at_column,
)
from owli_parser.infrastructure.db import Base


class ApplicationRow(Base):
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    allowed_scopes: Mapped[list[str]] = json_column(default=list)
    rate_limit_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    auth_revision: Mapped[int] = mapped_column(default=1, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_application_tenant_name"),)


class ApplicationTokenRow(Base):
    __tablename__ = "application_tokens"

    id: Mapped[uuid.UUID] = pk_column()
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    application_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    digest_key_version: Mapped[int] = mapped_column(default=1, nullable=False)
    scopes: Mapped[list[str]] = json_column(default=list)
    dataset_ids: Mapped[list[str]] = json_column(default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (Index("ix_app_tokens_app", "application_id", "revoked_at"),)


class ApplicationGrantRow(Base):
    __tablename__ = "application_grants"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    actions: Mapped[list[str]] = json_column(default=list)  # read/write/delete
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "application_id", "dataset_id", name="uq_grant_app_dataset"),
    )


class ApplicationUsageRow(Base):
    """应用调用记录（脱敏聚合；不保存请求正文、源 URL query 或 Authorization）。"""

    __tablename__ = "application_usage"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    token_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    status_code: Mapped[int] = mapped_column(nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (Index("ix_app_usage_app_time", "application_id", "created_at"),)
