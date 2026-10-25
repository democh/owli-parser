"""configuration ORM 模型：config_revisions、secret_refs、storage_profiles、
model_connections、model_profiles、model_bindings、pipelines、adapters。

普通配置不可变版本；密钥只存受控引用或密文，权限独立。
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


class ConfigRevisionRow(Base):
    """不可变配置发布记录（validate 后发布；回滚生成新记录）。"""

    __tablename__ = "config_revisions"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    resource_name: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="published", nullable=False)
    validated_payload: Mapped[dict[str, Any]] = json_column(default=dict)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (Index("ix_config_revisions_lookup", "tenant_id", "kind", "resource_name", "revision"),)


class SecretRefRow(Base):
    """密钥引用元信息；不存明文。"""

    __tablename__ = "secret_refs"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # env/file/vault
    reference: Mapped[str] = mapped_column(String(256), nullable=False)
    key_version: Mapped[int] = mapped_column(default=1, nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    last_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class StorageProfileRow(Base):
    """存储配置按 (name, revision) 不可变：修改发布新 revision 行。"""

    __tablename__ = "storage_profiles"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # local/s3/aliyun_oss
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    config: Mapped[dict[str, Any]] = json_column(default=dict)  # 无密钥配置
    credential_ref_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    read_disabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    write_disabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (UniqueConstraint("tenant_id", "name", "revision", name="uq_storage_profile_revision"),)


class ModelConnectionRow(Base):
    __tablename__ = "model_connections"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)  # openai_compatible 等
    vendor_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credential_ref_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    network_policy: Mapped[dict[str, Any]] = json_column(default=dict)
    state: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    last_test_report: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (UniqueConstraint("tenant_id", "name", "revision", name="uq_model_connection_revision"),)


class ModelProfileRow(Base):
    __tablename__ = "model_profiles"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    # capabilities 取值：generation/embedding/rerank/vision
    capabilities: Mapped[list[str]] = json_column(default=list)
    context_window: Mapped[int | None] = mapped_column(nullable=True)
    tokenizer_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    supported_parameters: Mapped[list[str]] = json_column(default=list)
    pricing_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(nullable=True)
    embedding_normalization: Mapped[str | None] = mapped_column(String(32), nullable=True)
    embedding_distance: Mapped[str | None] = mapped_column(String(32), nullable=True)
    output_kinds: Mapped[list[str]] = json_column(default=list)
    state: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    last_test_report: Mapped[dict[str, Any] | None] = json_column(nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("connection_id", "model_name", "revision", name="uq_model_profile_revision"),
    )


class ModelBindingRow(Base):
    __tablename__ = "model_bindings"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    primary_profile_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    fallback_profile_ids: Mapped[list[str]] = json_column(default=list)
    prompt_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parameter_overrides: Mapped[dict[str, Any]] = json_column(default=dict)
    budget: Mapped[dict[str, Any]] = json_column(default=dict)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (UniqueConstraint("tenant_id", "dataset_id", "purpose", name="uq_model_binding_scope"),)


class PipelineRow(Base):
    __tablename__ = "pipelines"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)  # NULL 为平台模板
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    yaml_text: Mapped[str] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    active: Mapped[bool] = mapped_column(default=False, nullable=False)
    tenant_scope: Mapped[str] = mapped_column(String(16), default="tenant", nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (UniqueConstraint("tenant_id", "name", "version", name="uq_pipeline_version"),)


class PipelineExperimentRow(Base):
    """管线 A/B 实验：同一 pipeline 的两个 YAML 变体 + B 组流量占比。

    - 每个 (tenant, pipeline) 至多一个实验；revision 随更新递增；
    - 变体 YAML 在保存时编译校验并记录 digest，提交时冻结进任务快照。
    """

    __tablename__ = "pipeline_experiments"

    id: Mapped[uuid.UUID] = pk_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    pipeline_name: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    variant_a_yaml: Mapped[str] = mapped_column(nullable=False)
    variant_b_yaml: Mapped[str] = mapped_column(nullable=False)
    variant_a_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    variant_b_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    traffic_percent: Mapped[int] = mapped_column(nullable=False)  # B 组占比 0-100
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (UniqueConstraint("tenant_id", "pipeline_name", name="uq_pipeline_experiment"),)


class AdapterRegistryRow(Base):
    """adapter 注册表镜像：实例带心跳与 readiness。"""

    __tablename__ = "adapters"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)  # local/remote/composite
    capabilities: Mapped[dict[str, Any]] = json_column(default=dict)
    config_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    health: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = updated_at_column()
