"""chat ORM 模型：chat_runs、chat_events、evidence_snapshots（M4 引入）。

事件重放与固定引用映射，不从可变 chunk 正文重建历史答案。
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


class ChatRunRow(Base):
    __tablename__ = "chat_runs"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # 多轮：父运行（同租户；历史问题/答案仅供上下文，本轮仍只依据本轮证据）
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    dataset_ids: Mapped[list[str]] = json_column(default=list)
    request_snapshot: Mapped[dict[str, Any]] = json_column(default=dict)
    error: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (Index("ix_chat_runs_tenant", "tenant_id", "created_at"),)


class ChatEventRow(Base):
    __tablename__ = "chat_events"

    id: Mapped[uuid.UUID] = pk_column()
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    seq: Mapped[int] = mapped_column(nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = json_column(default=dict)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_chat_event_seq"),)


class EvidenceSnapshotRow(Base):
    """生成前固定的引用映射：绑定 doc/version/chunk/revision/source_refs。"""

    __tablename__ = "evidence_snapshots"

    id: Mapped[uuid.UUID] = pk_column()
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    chunk_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    chunk_revision: Mapped[int] = mapped_column(nullable=False)
    acl_refs: Mapped[list[str]] = json_column(default=list)
    text_snapshot: Mapped[str] = mapped_column(nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = json_column(default=list)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (UniqueConstraint("run_id", "evidence_id", name="uq_run_evidence"),)
