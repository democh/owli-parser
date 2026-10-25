"""artifacts ORM 模型：artifacts、artifact_locations、uploads。

artifact 为逻辑身份（不可变），location 为物理位置；分片、引用与过期回收。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from owli_parser.infrastructure.columns import (
    created_at_column,
    json_column,
    pk_column,
    updated_at_column,
)
from owli_parser.infrastructure.db import Base


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # kind 取值：source/ir/markdown/preview/page_image/media/export
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    preferred_location_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    ref_count: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        Index("ix_artifacts_tenant_hash", "tenant_id", "content_hash"),
        Index("ix_artifacts_tenant_kind", "tenant_id", "kind", "created_at"),
    )


class ArtifactLocationRow(Base):
    __tablename__ = "artifact_locations"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    profile_revision_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # local/s3/aliyun_oss
    bucket: Mapped[str | None] = mapped_column(String(128), nullable=True)
    root_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    object_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (Index("ix_locations_artifact", "artifact_id", "state"),)


class UploadRow(Base):
    """持久化上传会话；绑定 tenant、actor、dataset、profile 与有效期。"""

    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    profile_revision_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    declared_total_bytes: Mapped[int | None] = mapped_column(nullable=True)
    part_size_bytes: Mapped[int] = mapped_column(nullable=False)
    parts: Mapped[dict[str, Any]] = json_column(default=dict)  # part_no -> {size, sha256, path}
    state: Mapped[str] = mapped_column(String(16), default="initiated", nullable=False)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (Index("ix_uploads_actor_state", "tenant_id", "actor_kind", "actor_id", "state"),)
