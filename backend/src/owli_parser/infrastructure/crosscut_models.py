"""横切基础设施表：outbox_events、audit_logs、idempotency_records。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from owli_parser.infrastructure.columns import (
    created_at_column,
    json_column,
    pk_column,
)
from owli_parser.infrastructure.db import Base, utcnow


class OutboxEventRow(Base):
    """事务内登记的异步动作。派发器扫描 pending 并标记 delivered。"""

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = pk_column()
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = json_column(default=dict)
    delivery_state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        Index("ix_outbox_state_available", "delivery_state", "available_at"),
        Index("ix_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict[str, Any]] = json_column(default=dict)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )


class IdempotencyRecordRow(Base):
    __tablename__ = "idempotency_records"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_ref: Mapped[dict[str, Any]] = json_column(default=dict)
    state: Mapped[str] = mapped_column(String(16), default="in_progress", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "actor_kind",
            "actor_id",
            "operation",
            "key",
            name="uq_idempotency_scope",
        ),
    )
