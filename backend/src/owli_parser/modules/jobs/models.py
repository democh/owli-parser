"""jobs ORM 模型：parsing_jobs、job_events、job_steps。

PG 为任务权威，状态提交使用 CAS；租约 + 单调 fence 保护发布。
"""

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
    updated_at_column,
)
from owli_parser.infrastructure.db import Base


class ParsingJobRow(Base):
    __tablename__ = "parsing_jobs"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True)
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt: Mapped[int] = mapped_column(default=1, nullable=False)
    execution_plan: Mapped[dict[str, Any]] = json_column(default=dict)
    effective_config_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    pool: Mapped[str] = mapped_column(String(16), default="cpu", nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fence: Mapped[int] = mapped_column(default=0, nullable=False)
    progress: Mapped[dict[str, Any]] = json_column(default=dict)
    total_units: Mapped[int | None] = mapped_column(nullable=True)
    completed_units: Mapped[int] = mapped_column(default=0, nullable=False)
    coverage: Mapped[float | None] = mapped_column(nullable=True)
    error: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parse_only: Mapped[bool] = mapped_column(default=False, nullable=False)
    retrieval_ready: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        Index("ix_jobs_tenant_state", "tenant_id", "state", "created_at"),
        Index("ix_jobs_lease", "lease_until"),
        Index("ix_jobs_document", "document_id", "created_at"),
    )


class JobEventRow(Base):
    __tablename__ = "job_events"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    seq: Mapped[int] = mapped_column(nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int | None] = mapped_column(nullable=True)
    from_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[dict[str, Any]] = json_column(default=dict)

    __table_args__ = (
        UniqueConstraint("job_id", "seq", name="uq_job_event_seq"),
        Index("ix_job_events_job_ts", "job_id", "ts"),
    )


class JobStepRow(Base):
    """页/时间窗级续跑与 adapter 降级记录。"""

    __tablename__ = "job_steps"

    id: Mapped[uuid.UUID] = pk_column()
    job_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    checkpoint_ref: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("job_id", "stage", "unit_id", "attempt", name="uq_job_step"),)
