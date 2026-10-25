"""knowledge ORM 模型：chunks、chunk_revisions、review_items、index_manifests、
golden_sets、golden_runs、index_actions。

管理修订和完整索引代；发布通过 catalog 服务完成，不越过权限门禁。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from owli_parser.infrastructure.columns import (
    created_at_column,
    json_column,
    pk_column,
    updated_at_column,
)
from owli_parser.infrastructure.db import Base


class ChunkRow(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    doc_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    current_revision: Mapped[int] = mapped_column(default=1, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="child", nullable=False)  # parent/child
    acl_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    embedding_state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    structure: Mapped[dict[str, Any]] = json_column(default=dict)  # heading_path、切分原因等
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        Index("ix_chunks_tenant_version_ordinal", "tenant_id", "version_id", "ordinal", "id"),
        Index("ix_chunks_parent", "parent_id"),
        Index("ix_chunks_doc", "doc_id", "version_id"),
    )


class ChunkRevisionRow(Base):
    """不可变正文与来源；(chunk_id, revision) 唯一，保留历史引用和人工修订。"""

    __tablename__ = "chunk_revisions"

    id: Mapped[uuid.UUID] = pk_column()
    chunk_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(nullable=False)
    token_count: Mapped[int] = mapped_column(nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = json_column(default=list)
    explanation: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (UniqueConstraint("chunk_id", "revision", name="uq_chunk_revision"),)


class ReviewItemRow(Base):
    """复核对象需绑定版本；处置使用乐观锁。"""

    __tablename__ = "review_items"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    block_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int | None] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    resolution: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        Index("ix_review_tenant_status", "tenant_id", "status", "created_at"),
        Index("ix_review_version", "version_id"),
    )


class IndexManifestRow(Base):
    """确认必需索引完整后才发布；expected set 可排除失败单位但仍展示缺失。"""

    __tablename__ = "index_manifests"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    index_generation: Mapped[int] = mapped_column(nullable=False)
    required_channels: Mapped[list[str]] = json_column(default=list)
    expected_ids_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_ids: Mapped[list[str]] = json_column(default=list)
    expected_count: Mapped[int] = mapped_column(default=0, nullable=False)
    acknowledgements: Mapped[dict[str, Any]] = json_column(default=dict)
    model_revisions: Mapped[dict[str, Any]] = json_column(default=dict)
    state: Mapped[str] = mapped_column(String(16), default="building", nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (UniqueConstraint("version_id", "index_generation", name="uq_manifest_generation"),)


class IndexActionRow(Base):
    """编辑派生的重索引行动：与编辑同事务登记，worker 消费后标记 done。

    - 消费时以 PG 当前 revision 为准（desired_revision 仅作登记快照）；
    - 禁用（enabled=false）不登记行动：权威层立即生效，旧条目不可召回；
    - 失败重试由 outbox 重派驱动，attempts 超限后置 failed（可手动 reembed）。
    """

    __tablename__ = "index_actions"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    chunk_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    desired_revision: Mapped[int] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(16), default="upsert", nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        Index("ix_index_actions_state", "state", "created_at"),
        Index("ix_index_actions_chunk", "chunk_id", "state"),
    )


class GoldenSetRow(Base):
    __tablename__ = "golden_sets"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    cases: Mapped[list[dict[str, Any]]] = json_column(default=list)
    expectations: Mapped[dict[str, Any]] = json_column(default=dict)
    created_at: Mapped[datetime] = created_at_column()


class GoldenRunRow(Base):
    __tablename__ = "golden_runs"

    id: Mapped[uuid.UUID] = pk_column()
    golden_set_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    adapter_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    config_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metrics: Mapped[dict[str, Any]] = json_column(default=dict)
    diff_report_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    created_at: Mapped[datetime] = created_at_column()
