"""catalog ORM 模型：datasets、documents、doc_versions。

负责资源身份与版本；不拉取 URL，不直接调用推理 SDK。
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


class DatasetRow(Base):
    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", nullable=False)
    auth_revision: Mapped[int] = mapped_column(default=1, nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    chunk_policy: Mapped[dict[str, Any]] = json_column(default=dict)
    storage_profile_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    model_binding_revision: Mapped[int | None] = mapped_column(nullable=True)
    allow_partial: Mapped[bool] = mapped_column(default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_dataset_tenant_name"),
        Index("ix_datasets_tenant", "tenant_id", "deleted_at"),
    )


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visible_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    desired_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    acl_revision: Mapped[int] = mapped_column(default=1, nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        # 内容 hash 使用租户作用域的普通索引，不作为业务文档全局唯一键
        Index("ix_documents_tenant_hash", "tenant_id", "content_hash"),
        Index("ix_documents_dataset", "dataset_id", "deleted_at", "created_at"),
    )


class DocVersionRow(Base):
    __tablename__ = "doc_versions"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_no: Mapped[int] = mapped_column(nullable=False)
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    ir_artifact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    effective_config_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_policy: Mapped[dict[str, Any]] = json_column(default=dict)
    tokenizer_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality_report: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    coverage: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_doc_version_no"),
        Index("ix_doc_versions_doc", "document_id", "created_at"),
    )
