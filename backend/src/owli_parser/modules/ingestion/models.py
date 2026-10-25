"""ingestion ORM 模型：source_imports、source_locations。

URL/对象接入任务独立于解析任务；来源 URL 脱敏，下载有租约/checkpoint。
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


class SourceLocationRow(Base):
    """经管理员批准的对象导入源；不得与受管原件/IR 私有前缀重叠。"""

    __tablename__ = "source_locations"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    profile_revision_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    inbox_prefix: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "dataset_id", "bucket", "inbox_prefix", name="uq_source_location"),
    )


class SourceImportRow(Base):
    __tablename__ = "source_imports"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # url/storage_object
    # URL 可能等价于密码（预签名参数）：完整值加密保存或放 SecretResolver，
    # 表内只保留受控引用与脱敏展示字段。
    source_secret_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_display_host: Mapped[str] = mapped_column(String(255), nullable=False)
    source_display_path: Mapped[str] = mapped_column(String(512), nullable=False)
    source_location_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    relative_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    object_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filename_hint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pipeline_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    expected_revision: Mapped[int | None] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    attempt: Mapped[int] = mapped_column(default=1, nullable=False)
    bytes_received: Mapped[int] = mapped_column(default=0, nullable=False)
    total_bytes: Mapped[int | None] = mapped_column(nullable=True)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    parsing_job_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, unique=True)
    execution_snapshot: Mapped[dict[str, Any]] = json_column(default=dict)
    error: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fence: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        Index("ix_imports_tenant_state", "tenant_id", "state", "created_at"),
        Index("ix_imports_dataset", "dataset_id", "created_at"),
    )
