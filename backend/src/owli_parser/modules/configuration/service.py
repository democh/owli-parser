"""configuration 应用服务：存储 profile 与模型连接的生命周期。

- 配置按 (name, revision) 不可变：修改创建新草稿行，发布后该 name 指向新 revision。
- validate（无副作用，字段/引用/网络策略）与 test（显式 IO/网络调用，可能计费或
  创建临时对象）严格分开；保存配置不自动产生付费调用。
- publish 前必须通过 validate；密钥只允许引用（env:/file:），不读回明文。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select

from owli_parser.bootstrap.settings import AppSettings
from owli_parser.core.clock import Clock
from owli_parser.core.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    RevisionConflictError,
    ValidationFailedError,
)
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.artifacts.service import StorageRouter
from owli_parser.modules.configuration.models import (
    AdapterRegistryRow,
    ConfigRevisionRow,
    ModelBindingRow,
    ModelConnectionRow,
    ModelProfileRow,
    PipelineExperimentRow,
    PipelineRow,
    SecretRefRow,
    StorageProfileRow,
)
from owli_parser.modules.ingestion.models import SourceLocationRow
from owli_parser.modules.parsing import pipeline as pipeline_module
from owli_parser.modules.parsing.builtin_adapters import default_registry
from owli_parser.ports.secrets import SecretResolver, SecretUnavailable
from owli_parser.ports.storage import (
    ByteRange,
    ObjectLocator,
    ObjectTarget,
    PutOptions,
    StorageError,
)


@dataclass(frozen=True, slots=True)
class PipelineConfigInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    name: str
    version: int
    active: bool
    digest: str
    yaml_text: str
    created_at: Any


@dataclass(frozen=True, slots=True)
class PipelineExperimentInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    pipeline_name: str
    name: str
    variant_a_yaml: str
    variant_b_yaml: str
    variant_a_digest: str
    variant_b_digest: str
    traffic_percent: int
    active: bool
    revision: int
    created_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class AdapterRegistryInfo:
    """外部 adapter 注册镜像（平台级，无租户维度）。

    health 为读时生效值：心跳超时/从未心跳会被判为 unavailable/unknown，
    不会回写数据库（保留最近一次上报以便排查）。
    """

    name: str
    version: str
    mode: str
    capabilities: dict[str, Any]
    config_ref: str | None
    health: str
    reported_health: str
    heartbeat_at: datetime | None
    stale: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConfigRevisionInfo:
    id: uuid.UUID
    kind: str
    resource_id: uuid.UUID | None
    resource_name: str
    revision: int
    state: str
    validated_payload: dict[str, Any]
    payload_digest: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SecretRefInfo:
    id: uuid.UUID
    provider: str
    reference: str
    key_version: int
    purpose: str
    bound_endpoint: str | None
    state: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ModelProfileInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    model_name: str
    revision: int
    capabilities: list[str]
    context_window: int | None
    tokenizer_ref: str | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    supported_parameters: list[str]
    pricing_ref: str | None
    embedding_dimensions: int | None
    embedding_normalization: str | None
    embedding_distance: str | None
    output_kinds: list[str]
    state: str


@dataclass(frozen=True, slots=True)
class ModelBindingInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    purpose: str
    primary_profile_id: uuid.UUID
    primary_model_name: str | None
    fallback_profile_ids: list[str]
    revision: int
    # 预算（自由 JSON）：目前支持 max_tokens_per_run（单次问答 token 上限）
    budget: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StorageProfileInfo:
    id: uuid.UUID
    name: str
    provider: str
    revision: int
    state: str
    config: dict[str, Any]
    read_disabled: bool
    write_disabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StorageTestReport:
    ok: bool
    steps: dict[str, str]
    notes: list[str]


@dataclass(frozen=True, slots=True)
class ModelConnectionInfo:
    id: uuid.UUID
    name: str
    protocol: str
    vendor_label: str | None
    base_url: str
    region: str | None
    revision: int
    state: str
    credential_configured: bool
    last_test_report: dict[str, Any] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ModelTestReport:
    ok: bool
    steps: dict[str, str]
    latency_ms: int | None
    notes: list[str]


_STORAGE_REQUIRED_CONFIG: dict[str, tuple[str, ...]] = {
    "local": ("root_path",),
    "s3": ("bucket", "credential_ref"),
    "aliyun_oss": ("region", "endpoint", "bucket", "credential_ref"),
}

_MODEL_PROTOCOLS = {"openai_compatible"}


def _experiment_bucket(experiment_id: uuid.UUID, bucket_key: str) -> int:
    """稳定分桶（0-99）：同一实验与同一文档键恒定，跨进程一致。"""
    digest = hashlib.sha256(f"{experiment_id}:{bucket_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


_ADAPTER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ADAPTER_MODES = frozenset({"local", "remote", "composite"})
_ADAPTER_HEALTH = frozenset({"ready", "degraded", "maintenance", "unavailable"})


def _check_adapter_fields(*, version: str, config_ref: str | None) -> None:
    if not version or len(version) > 32:
        raise InvalidInputError("version 需为 1-32 字符")
    if config_ref is not None and len(config_ref) > 128:
        raise InvalidInputError("config_ref 最长 128 字符")


def _effective_adapter_health(
    reported: str,
    heartbeat_at: datetime | None,
    *,
    now: datetime,
    ttl_seconds: int,
) -> tuple[str, bool]:
    """读时生效健康度：never→unknown，超时→unavailable；都不回写数据库。"""
    if heartbeat_at is None:
        return "unknown", True
    beat = heartbeat_at if heartbeat_at.tzinfo is not None else heartbeat_at.replace(tzinfo=UTC)
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if (current - beat).total_seconds() > ttl_seconds:
        return "unavailable", True
    return reported, False


class ConfigurationService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        settings: AppSettings,
        secrets: SecretResolver,
        storage_router: StorageRouter,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._settings = settings
        self._secrets = secrets
        self._router = storage_router

    # ------------------------------------------------------------------
    # 配置发布审计（config_revisions）与密钥引用登记（secret_refs）
    # ------------------------------------------------------------------

    @staticmethod
    def _secret_provider(reference: str) -> str:
        if reference.startswith("env:"):
            return "env"
        if reference.startswith("file:"):
            return "file"
        return "vault"

    @staticmethod
    def _record_config_revision(
        session: Any,
        *,
        tenant_id: uuid.UUID | None,
        kind: str,
        resource_id: uuid.UUID | None,
        resource_name: str,
        revision: int,
        validated_payload: dict[str, Any],
        actor_id: uuid.UUID,
    ) -> None:
        """发布动作的不可变审计记录（同事务写入；digest 为规范化 JSON 的 sha256）。"""
        canonical = json.dumps(validated_payload, sort_keys=True, ensure_ascii=False)
        session.add(
            ConfigRevisionRow(
                tenant_id=tenant_id,
                kind=kind,
                resource_id=resource_id,
                resource_name=resource_name,
                revision=revision,
                state="published",
                validated_payload=validated_payload,
                payload_digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                created_by_kind="user",
                created_by_id=actor_id,
            )
        )

    async def list_config_revisions(
        self,
        tenant_id: uuid.UUID,
        *,
        kind: str | None = None,
        resource_name: str | None = None,
        limit: int = 50,
    ) -> list[ConfigRevisionInfo]:
        async with self._uow.read_only() as session:
            query = select(ConfigRevisionRow).where(
                (ConfigRevisionRow.tenant_id == tenant_id) | (ConfigRevisionRow.tenant_id.is_(None))
            )
            if kind:
                query = query.where(ConfigRevisionRow.kind == kind)
            if resource_name:
                query = query.where(ConfigRevisionRow.resource_name == resource_name)
            rows = (
                (
                    await session.execute(
                        query.order_by(
                            ConfigRevisionRow.created_at.desc(), ConfigRevisionRow.id.desc()
                        ).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [
                ConfigRevisionInfo(
                    id=row.id,
                    kind=row.kind,
                    resource_id=row.resource_id,
                    resource_name=row.resource_name,
                    revision=row.revision,
                    state=row.state,
                    validated_payload=row.validated_payload or {},
                    payload_digest=row.payload_digest,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    async def list_secret_refs(self, tenant_id: uuid.UUID) -> list[SecretRefInfo]:
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(SecretRefRow)
                        .where(SecretRefRow.tenant_id == tenant_id)
                        .order_by(SecretRefRow.created_at, SecretRefRow.id)
                    )
                )
                .scalars()
                .all()
            )
            return [self._secret_info(row) for row in rows]

    async def bump_secret_ref_version(self, tenant_id: uuid.UUID, secret_id: uuid.UUID) -> SecretRefInfo:
        """轮换标记：key_version 递增（实际密钥由部署侧更新 env/file 后重启生效）。"""
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(SecretRefRow).where(
                        SecretRefRow.id == secret_id,
                        SecretRefRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("密钥引用不存在")
            row.key_version += 1
            await session.flush()
            return self._secret_info(row)

    @staticmethod
    def _secret_info(row: SecretRefRow) -> SecretRefInfo:
        return SecretRefInfo(
            id=row.id,
            provider=row.provider,
            reference=row.reference,
            key_version=row.key_version,
            purpose=row.purpose,
            bound_endpoint=row.bound_endpoint,
            state=row.state,
            created_at=row.created_at,
        )

    async def _upsert_secret_ref(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        reference: str,
        purpose: str,
        bound_endpoint: str | None,
        actor_id: uuid.UUID,
    ) -> None:
        """幂等：同 (tenant, reference, purpose) 已存在仅刷新绑定端点。"""
        existing = (
            await session.execute(
                select(SecretRefRow).where(
                    SecretRefRow.tenant_id == tenant_id,
                    SecretRefRow.reference == reference,
                    SecretRefRow.purpose == purpose,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if bound_endpoint and existing.bound_endpoint != bound_endpoint:
                existing.bound_endpoint = bound_endpoint
            return
        session.add(
            SecretRefRow(
                tenant_id=tenant_id,
                provider=self._secret_provider(reference),
                reference=reference,
                key_version=1,
                purpose=purpose,
                bound_endpoint=bound_endpoint,
                state="active",
            )
        )

    # ------------------------------------------------------------------
    # 模型能力档案与用途绑定（三层：连接 → 能力 profile → 用途绑定）
    # ------------------------------------------------------------------

    async def create_model_profile(
        self,
        tenant_id: uuid.UUID,
        *,
        connection_id: uuid.UUID,
        model_name: str,
        capabilities: list[str],
        context_window: int | None,
        tokenizer_ref: str | None,
        max_input_tokens: int | None,
        max_output_tokens: int | None,
        supported_parameters: list[str],
        pricing_ref: str | None,
        embedding_dimensions: int | None,
        embedding_normalization: str | None,
        embedding_distance: str | None,
        output_kinds: list[str],
        actor_id: uuid.UUID,
    ) -> ModelProfileInfo:
        """能力档案不可变（能力/模型变更请新建档案）：避免在途索引与已发布绑定的语义混淆。"""
        clean = model_name.strip()
        if not 1 <= len(clean) <= 255:
            raise ValidationFailedError("model_name 长度需在 1-255 之间")
        if not capabilities:
            raise ValidationFailedError("capabilities 不能为空")
        unknown = set(capabilities) - _MODEL_CAPABILITIES
        if unknown:
            raise InvalidInputError(f"不支持的能力：{sorted(unknown)}")
        # 维度是索引集合的硬契约：缺失维度将导致 upsert 语义不可判定
        if "embedding" in capabilities and (embedding_dimensions is None or embedding_dimensions <= 0):
            raise ValidationFailedError("embedding 能力必须声明 embedding_dimensions（>0）")
        async with self._uow.begin() as session:
            connection = (
                await session.execute(
                    select(ModelConnectionRow).where(
                        ModelConnectionRow.id == connection_id,
                        ModelConnectionRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if connection is None:
                raise NotFoundError("模型连接不存在")
            if connection.state != "published":
                raise ValidationFailedError("模型连接尚未发布（先显式测试与发布）")
            row = ModelProfileRow(
                tenant_id=tenant_id,
                connection_id=connection_id,
                model_name=clean,
                revision=1,
                capabilities=sorted(set(capabilities)),
                context_window=context_window,
                tokenizer_ref=tokenizer_ref,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                supported_parameters=supported_parameters,
                pricing_ref=pricing_ref,
                embedding_dimensions=embedding_dimensions,
                embedding_normalization=embedding_normalization,
                embedding_distance=embedding_distance,
                output_kinds=output_kinds,
                state="published",
                created_by_kind="user",
                created_by_id=actor_id,
            )
            session.add(row)
            await session.flush()
            return self._profile_info(row)

    async def get_model_connection_invocation(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID
    ) -> tuple[str, str | None]:
        """装配层调用信息：base_url 与 credential_ref（引用，不解析明文）。"""
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(ModelConnectionRow).where(
                        ModelConnectionRow.id == connection_id,
                        ModelConnectionRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("模型连接不存在")
            if row.state != "published":
                raise ValidationFailedError("模型连接尚未发布")
            return row.base_url, (row.network_policy or {}).get("credential_ref")

    async def list_model_profiles(self, tenant_id: uuid.UUID) -> list[ModelProfileInfo]:
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(ModelProfileRow)
                        .where(ModelProfileRow.tenant_id == tenant_id)
                        .order_by(ModelProfileRow.created_at, ModelProfileRow.id)
                    )
                )
                .scalars()
                .all()
            )
            return [self._profile_info(row) for row in rows]

    async def get_model_profile(self, tenant_id: uuid.UUID, profile_id: uuid.UUID) -> ModelProfileInfo:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(ModelProfileRow).where(
                        ModelProfileRow.id == profile_id,
                        ModelProfileRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("模型档案不存在")
            return self._profile_info(row)

    async def set_model_profile_state(
        self, tenant_id: uuid.UUID, profile_id: uuid.UUID, *, state: str
    ) -> ModelProfileInfo:
        """停用/恢复；停用时若仍被用途绑定引用则拒绝（先改绑定）。"""
        if state not in {"published", "disabled"}:
            raise InvalidInputError("state 仅支持 published/disabled")
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(ModelProfileRow).where(
                        ModelProfileRow.id == profile_id,
                        ModelProfileRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("模型档案不存在")
            if state == "disabled":
                bindings = (
                    (
                        await session.execute(
                            select(ModelBindingRow).where(
                                ModelBindingRow.tenant_id == tenant_id,
                                ModelBindingRow.primary_profile_id == profile_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if bindings:
                    raise ConflictError("档案仍被用途绑定引用，请先调整绑定")
            row.state = state
            await session.flush()
            return self._profile_info(row)

    async def upsert_model_binding(
        self,
        tenant_id: uuid.UUID,
        purpose: str,
        *,
        primary_profile_id: uuid.UUID,
        fallback_profile_ids: list[str],
        budget: dict[str, Any] | None = None,
        expected_revision: int | None,
        actor_id: uuid.UUID,
    ) -> ModelBindingInfo:
        """用途绑定（租户级）：primary/fallback 必须已发布且声明对应能力。"""
        if purpose not in _MODEL_CAPABILITIES:
            raise InvalidInputError(f"不支持用途：{purpose}")
        async with self._uow.begin() as session:
            profiles: dict[uuid.UUID, ModelProfileRow] = {}
            ids: list[uuid.UUID] = [primary_profile_id]
            for raw in fallback_profile_ids:
                try:
                    ids.append(uuid.UUID(str(raw)))
                except ValueError as exc:
                    raise ValidationFailedError("fallback_profile_ids 含非法 UUID") from exc
            rows = (
                (
                    await session.execute(
                        select(ModelProfileRow).where(
                            ModelProfileRow.tenant_id == tenant_id,
                            ModelProfileRow.id.in_(ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                profiles[row.id] = row
            missing = [str(value) for value in ids if value not in profiles]
            if missing:
                raise NotFoundError(f"模型档案不存在：{missing}")
            for profile_id in ids:
                profile = profiles[profile_id]
                if profile.state != "published":
                    raise ValidationFailedError(f"档案 {profile.model_name} 未发布（state={profile.state}）")
                if purpose not in (profile.capabilities or []):
                    raise ValidationFailedError(f"档案 {profile.model_name} 未声明 {purpose} 能力")

            existing = (
                await session.execute(
                    select(ModelBindingRow).where(
                        ModelBindingRow.tenant_id == tenant_id,
                        ModelBindingRow.purpose == purpose,
                        ModelBindingRow.dataset_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                if expected_revision is not None:
                    raise RevisionConflictError("绑定尚不存在，If-Match 无效")
                binding_row = ModelBindingRow(
                    tenant_id=tenant_id,
                    dataset_id=None,
                    purpose=purpose,
                    primary_profile_id=primary_profile_id,
                    fallback_profile_ids=[str(value) for value in fallback_profile_ids],
                    budget=dict(budget or {}),
                    revision=1,
                    created_by_kind="user",
                    created_by_id=actor_id,
                )
                session.add(binding_row)
                await session.flush()
                return self._binding_info(binding_row, profiles)
            if expected_revision is None:
                raise RevisionConflictError("修改已有绑定必须携带 If-Match")
            if expected_revision != existing.revision:
                raise RevisionConflictError("绑定已被其他操作修改，请刷新")
            existing.primary_profile_id = primary_profile_id
            existing.fallback_profile_ids = [str(value) for value in fallback_profile_ids]
            if budget is not None:
                existing.budget = dict(budget)
            existing.revision += 1
            await session.flush()
            return self._binding_info(existing, profiles)

    async def list_model_bindings(self, tenant_id: uuid.UUID) -> list[ModelBindingInfo]:
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(ModelBindingRow)
                        .where(ModelBindingRow.tenant_id == tenant_id)
                        .order_by(ModelBindingRow.purpose)
                    )
                )
                .scalars()
                .all()
            )
            profiles = {
                row.id: row
                for row in (
                    (
                        await session.execute(
                            select(ModelProfileRow).where(ModelProfileRow.tenant_id == tenant_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            return [self._binding_info(row, profiles) for row in rows]

    async def get_model_binding(self, tenant_id: uuid.UUID, purpose: str) -> ModelBindingInfo | None:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(ModelBindingRow).where(
                        ModelBindingRow.tenant_id == tenant_id,
                        ModelBindingRow.purpose == purpose,
                        ModelBindingRow.dataset_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            profile = (
                await session.execute(
                    select(ModelProfileRow).where(ModelProfileRow.id == row.primary_profile_id)
                )
            ).scalar_one_or_none()
            profiles = {profile.id: profile} if profile else {}
            return self._binding_info(row, profiles)

    @staticmethod
    def _profile_info(row: ModelProfileRow) -> ModelProfileInfo:
        return ModelProfileInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            connection_id=row.connection_id,
            model_name=row.model_name,
            revision=row.revision,
            capabilities=list(row.capabilities or []),
            context_window=row.context_window,
            tokenizer_ref=row.tokenizer_ref,
            max_input_tokens=row.max_input_tokens,
            max_output_tokens=row.max_output_tokens,
            supported_parameters=list(row.supported_parameters or []),
            pricing_ref=row.pricing_ref,
            embedding_dimensions=row.embedding_dimensions,
            embedding_normalization=row.embedding_normalization,
            embedding_distance=row.embedding_distance,
            output_kinds=list(row.output_kinds or []),
            state=row.state,
        )

    @staticmethod
    def _binding_info(row: ModelBindingRow, profiles: dict[uuid.UUID, ModelProfileRow]) -> ModelBindingInfo:
        primary = profiles.get(row.primary_profile_id)
        return ModelBindingInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            purpose=row.purpose,
            primary_profile_id=row.primary_profile_id,
            primary_model_name=primary.model_name if primary else None,
            fallback_profile_ids=[str(value) for value in (row.fallback_profile_ids or [])],
            revision=row.revision,
            budget=dict(row.budget or {}),
        )

    # ------------------------------------------------------------------
    # 解析管道配置（版本化；PUT 要求 If-Match；校验期编译）
    # ------------------------------------------------------------------

    async def save_pipeline(
        self,
        tenant_id: uuid.UUID,
        *,
        name: str,
        yaml_text: str,
        expected_version: int | None,
        actor_kind: str,
        actor_id: uuid.UUID,
    ) -> PipelineConfigInfo:
        """保存新版本（不覆盖历史）：校验 YAML 可编译且 name 一致。

        expected_version=None 表示首次创建（同名已有 → 409 要求 If-Match）；
        提供时须等于当前 active 版本，否则 412（并发修改）。
        """
        try:
            compiled = pipeline_module.compile_pipeline(yaml_text, set(default_registry().keys()))
        except Exception as exc:
            raise ValidationFailedError(f"pipeline 校验失败：{exc!s:.200}") from exc
        if compiled.document.name != name:
            raise ValidationFailedError(
                f"YAML 内 name（{compiled.document.name}）与路径 name（{name}）不一致"
            )

        async with self._uow.begin() as session:
            existing = (
                (
                    await session.execute(
                        select(PipelineRow).where(
                            PipelineRow.tenant_id == tenant_id, PipelineRow.name == name
                        )
                    )
                )
                .scalars()
                .all()
            )
            current = max(existing, key=lambda row: row.version) if existing else None
            if current is None:
                if expected_version is not None:
                    raise RevisionConflictError("pipeline 尚不存在，If-Match 无效")
                version = 1
            else:
                if expected_version is None:
                    raise RevisionConflictError("修改已有 pipeline 必须携带 If-Match")
                if expected_version != current.version:
                    raise RevisionConflictError("pipeline 已被其他操作修改，请刷新")
                version = current.version + 1
                current.active = False
            row = PipelineRow(
                tenant_id=tenant_id,
                name=name,
                yaml_text=yaml_text,
                version=version,
                active=True,
                tenant_scope="tenant",
                digest=compiled.digest,
                created_by_kind=actor_kind,
                created_by_id=actor_id,
            )
            session.add(row)
            await session.flush()
            self._record_config_revision(
                session,
                tenant_id=tenant_id,
                kind="pipeline",
                resource_id=row.id,
                resource_name=row.name,
                revision=row.version,
                validated_payload={
                    "name": row.name,
                    "version": row.version,
                    "digest": row.digest,
                },
                actor_id=actor_id,
            )
            return self._pipeline_info(row)

    async def get_active_pipeline(self, tenant_id: uuid.UUID, name: str) -> PipelineConfigInfo | None:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(PipelineRow).where(
                        PipelineRow.tenant_id == tenant_id,
                        PipelineRow.name == name,
                        PipelineRow.active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            return self._pipeline_info(row) if row else None

    async def list_active_pipelines(self, tenant_id: uuid.UUID) -> list[PipelineConfigInfo]:
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineRow)
                        .where(PipelineRow.tenant_id == tenant_id, PipelineRow.active.is_(True))
                        .order_by(PipelineRow.name)
                    )
                )
                .scalars()
                .all()
            )
            return [self._pipeline_info(row) for row in rows]

    @staticmethod
    def _pipeline_info(row: PipelineRow) -> PipelineConfigInfo:
        return PipelineConfigInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            version=row.version,
            active=row.active,
            digest=row.digest,
            yaml_text=row.yaml_text,
            created_at=row.created_at,
        )

    # ------------------------------------------------------------------
    # 管线 A/B 实验
    # ------------------------------------------------------------------

    async def upsert_pipeline_experiment(
        self,
        tenant_id: uuid.UUID,
        *,
        pipeline_name: str,
        name: str,
        variant_a_yaml: str,
        variant_b_yaml: str,
        traffic_percent: int,
        active: bool,
        actor_kind: str,
        actor_id: uuid.UUID,
    ) -> PipelineExperimentInfo:
        """创建/更新实验：两个变体均编译校验且 name 一致；revision 递增。"""
        if not 0 <= traffic_percent <= 100:
            raise ValidationFailedError("traffic_percent 需在 0-100 之间")
        known = set(default_registry().keys())
        compiled: dict[str, Any] = {}
        for label, yaml_text in (("A", variant_a_yaml), ("B", variant_b_yaml)):
            try:
                variant = pipeline_module.compile_pipeline(yaml_text, known)
            except Exception as exc:
                raise ValidationFailedError(f"变体 {label} YAML 校验失败：{exc!s:.200}") from exc
            if variant.document.name != pipeline_name:
                raise ValidationFailedError(
                    f"变体 {label} 的 name（{variant.document.name}）与 pipeline（{pipeline_name}）不一致"
                )
            compiled[label] = variant

        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(PipelineExperimentRow).where(
                        PipelineExperimentRow.tenant_id == tenant_id,
                        PipelineExperimentRow.pipeline_name == pipeline_name,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = PipelineExperimentRow(
                    tenant_id=tenant_id,
                    pipeline_name=pipeline_name,
                    name=name,
                    variant_a_yaml=variant_a_yaml,
                    variant_b_yaml=variant_b_yaml,
                    variant_a_digest=compiled["A"].digest,
                    variant_b_digest=compiled["B"].digest,
                    traffic_percent=traffic_percent,
                    active=active,
                    revision=1,
                    created_by_kind=actor_kind,
                    created_by_id=actor_id,
                )
                session.add(row)
            else:
                row.name = name
                row.variant_a_yaml = variant_a_yaml
                row.variant_b_yaml = variant_b_yaml
                row.variant_a_digest = compiled["A"].digest
                row.variant_b_digest = compiled["B"].digest
                row.traffic_percent = traffic_percent
                row.active = active
                row.revision += 1
            await session.flush()
            return self._experiment_info(row)

    async def get_pipeline_experiment(
        self, tenant_id: uuid.UUID, pipeline_name: str
    ) -> PipelineExperimentInfo | None:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(PipelineExperimentRow).where(
                        PipelineExperimentRow.tenant_id == tenant_id,
                        PipelineExperimentRow.pipeline_name == pipeline_name,
                    )
                )
            ).scalar_one_or_none()
            return self._experiment_info(row) if row else None

    async def list_pipeline_experiments(self, tenant_id: uuid.UUID) -> list[PipelineExperimentInfo]:
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineExperimentRow)
                        .where(PipelineExperimentRow.tenant_id == tenant_id)
                        .order_by(PipelineExperimentRow.pipeline_name)
                    )
                )
                .scalars()
                .all()
            )
            return [self._experiment_info(row) for row in rows]

    async def select_pipeline_experiment(
        self, tenant_id: uuid.UUID, pipeline_name: str, *, bucket_key: str
    ) -> tuple[pipeline_module.CompiledPipeline, dict[str, Any]] | None:
        """按稳定分桶选择变体并编译；无实验/暂停/变体不可编译 → None（回退基础配置）。"""
        info = await self.get_pipeline_experiment(tenant_id, pipeline_name)
        if info is None or not info.active:
            return None
        bucket = _experiment_bucket(info.id, bucket_key)
        variant = "B" if bucket < info.traffic_percent else "A"
        yaml_text = info.variant_b_yaml if variant == "B" else info.variant_a_yaml
        try:
            compiled = pipeline_module.compile_pipeline(yaml_text, set(default_registry().keys()))
        except Exception:
            return None
        return compiled, {
            "experiment_id": str(info.id),
            "experiment_name": info.name,
            "experiment_revision": info.revision,
            "variant": variant,
            "bucket": bucket,
            "traffic_percent": info.traffic_percent,
        }

    @staticmethod
    def _experiment_info(row: PipelineExperimentRow) -> PipelineExperimentInfo:
        return PipelineExperimentInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            pipeline_name=row.pipeline_name,
            name=row.name,
            variant_a_yaml=row.variant_a_yaml,
            variant_b_yaml=row.variant_b_yaml,
            variant_a_digest=row.variant_a_digest,
            variant_b_digest=row.variant_b_digest,
            traffic_percent=row.traffic_percent,
            active=row.active,
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    # ------------------------------------------------------------------
    # adapter 注册表（外部实例心跳/readiness）
    # ------------------------------------------------------------------

    async def list_adapters(self) -> list[AdapterRegistryInfo]:
        async with self._uow.read_only() as session:
            rows = (
                (await session.execute(select(AdapterRegistryRow).order_by(AdapterRegistryRow.name)))
                .scalars()
                .all()
            )
        now = self._clock.now()
        ttl = self._settings.parsing.adapter_heartbeat_ttl_seconds
        return [self._adapter_info(row, now=now, ttl=ttl) for row in rows]

    async def register_adapter(
        self,
        *,
        name: str,
        version: str,
        mode: str,
        capabilities: dict[str, Any],
        config_ref: str | None,
        health: str,
    ) -> AdapterRegistryInfo:
        """注册/更新外部 adapter，并立即记录一次心跳（平台级资源）。"""
        clean = name.strip()
        if _ADAPTER_NAME_RE.fullmatch(clean) is None:
            raise InvalidInputError("adapter 名称需为 1-64 位小写字母/数字/._-")
        if clean in default_registry():
            raise ConflictError("内置 adapter 由进程内注册表管理，不允许注册同名外部实例")
        if mode not in _ADAPTER_MODES:
            raise InvalidInputError("mode 仅支持 local/remote/composite")
        if health not in _ADAPTER_HEALTH:
            raise InvalidInputError("health 仅支持 ready/degraded/maintenance/unavailable")
        _check_adapter_fields(version=version, config_ref=config_ref)
        now = self._clock.now()
        ttl = self._settings.parsing.adapter_heartbeat_ttl_seconds
        async with self._uow.begin() as session:
            row = await session.get(AdapterRegistryRow, clean)
            if row is None:
                row = AdapterRegistryRow(
                    name=clean,
                    version=version,
                    mode=mode,
                    capabilities=dict(capabilities),
                    config_ref=config_ref,
                    health=health,
                    heartbeat_at=now,
                )
                session.add(row)
            else:
                row.version = version
                row.mode = mode
                row.capabilities = dict(capabilities)
                row.config_ref = config_ref
                row.health = health
                row.heartbeat_at = now
            await session.flush()
            return self._adapter_info(row, now=now, ttl=ttl)

    async def heartbeat_adapter(
        self, *, name: str, health: str, version: str | None = None
    ) -> AdapterRegistryInfo:
        """记录一次心跳：仅更新健康度与时间戳（未注册实例返回 404，不隐式创建）。"""
        if health not in _ADAPTER_HEALTH:
            raise InvalidInputError("health 仅支持 ready/degraded/maintenance/unavailable")
        if version is not None:
            _check_adapter_fields(version=version, config_ref=None)
        clean = name.strip()
        now = self._clock.now()
        ttl = self._settings.parsing.adapter_heartbeat_ttl_seconds
        async with self._uow.begin() as session:
            row = await session.get(AdapterRegistryRow, clean)
            if row is None:
                raise NotFoundError("adapter 未注册")
            row.health = health
            if version is not None:
                row.version = version
            row.heartbeat_at = now
            await session.flush()
            return self._adapter_info(row, now=now, ttl=ttl)

    @staticmethod
    def _adapter_info(row: AdapterRegistryRow, *, now: datetime, ttl: int) -> AdapterRegistryInfo:
        health, stale = _effective_adapter_health(row.health, row.heartbeat_at, now=now, ttl_seconds=ttl)
        return AdapterRegistryInfo(
            name=row.name,
            version=row.version,
            mode=row.mode,
            capabilities=dict(row.capabilities or {}),
            config_ref=row.config_ref,
            health=health,
            reported_health=row.health,
            heartbeat_at=row.heartbeat_at,
            stale=stale,
            updated_at=row.updated_at,
        )

    # ------------------------------------------------------------------
    # 存储 profile
    # ------------------------------------------------------------------

    async def create_storage_profile(
        self,
        tenant_id: uuid.UUID,
        *,
        name: str,
        provider: str,
        config: dict[str, Any],
        actor_id: uuid.UUID,
    ) -> StorageProfileInfo:
        clean = name.strip()
        if not 1 <= len(clean) <= 128:
            raise ValidationFailedError("名称长度需在 1-128 之间")
        if provider not in _STORAGE_REQUIRED_CONFIG:
            raise InvalidInputError(f"不支持的 provider: {provider}")
        _reject_secret_values(config)
        async with self._uow.begin() as session:
            existing = (
                (
                    await session.execute(
                        select(StorageProfileRow).where(
                            StorageProfileRow.tenant_id == tenant_id,
                            StorageProfileRow.name == clean,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                raise ConflictError("同名存储 profile 已存在，请通过更新创建新 revision")
            row = StorageProfileRow(
                tenant_id=tenant_id,
                name=clean,
                provider=provider,
                revision=1,
                state="draft",
                config=config,
                created_by_kind="user",
                created_by_id=actor_id,
            )
            session.add(row)
            await session.flush()
            storage_secret = str(config.get("credential_ref", "") or "")
            if storage_secret:
                await self._upsert_secret_ref(
                    session,
                    tenant_id=tenant_id,
                    reference=storage_secret,
                    purpose="storage_credential",
                    bound_endpoint=provider,
                    actor_id=actor_id,
                )
            return self._storage_info(row)

    async def list_storage_profiles(self, tenant_id: uuid.UUID) -> list[StorageProfileInfo]:
        """每个 name 返回最新 revision 行（历史 revision 保留但不在此列出）。"""
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(StorageProfileRow)
                        .where(StorageProfileRow.tenant_id == tenant_id)
                        .order_by(StorageProfileRow.name, StorageProfileRow.revision.desc())
                    )
                )
                .scalars()
                .all()
            )
        latest: dict[str, StorageProfileRow] = {}
        for row in rows:
            latest.setdefault(row.name, row)
        return [self._storage_info(r) for r in sorted(latest.values(), key=lambda r: r.name)]

    async def get_storage_profile(self, tenant_id: uuid.UUID, profile_id: uuid.UUID) -> StorageProfileInfo:
        async with self._uow.read_only() as session:
            row = await self._get_storage_row(session, tenant_id, profile_id)
            return self._storage_info(row)

    async def update_storage_profile(
        self,
        tenant_id: uuid.UUID,
        profile_id: uuid.UUID,
        *,
        config: dict[str, Any] | None,
        actor_id: uuid.UUID,
    ) -> StorageProfileInfo:
        """修改创建新草稿 revision（不可变版本链）。"""
        if config is not None:
            _reject_secret_values(config)
        async with self._uow.begin() as session:
            current = await self._get_storage_row(session, tenant_id, profile_id)
            max_revision = (
                await session.execute(
                    select(StorageProfileRow.revision)
                    .where(
                        StorageProfileRow.tenant_id == tenant_id,
                        StorageProfileRow.name == current.name,
                    )
                    .order_by(StorageProfileRow.revision.desc())
                    .limit(1)
                )
            ).scalar_one()
            row = StorageProfileRow(
                tenant_id=tenant_id,
                name=current.name,
                provider=current.provider,
                revision=int(max_revision) + 1,
                state="draft",
                config=config if config is not None else dict(current.config or {}),
                read_disabled=current.read_disabled,
                write_disabled=current.write_disabled,
                created_by_kind="user",
                created_by_id=actor_id,
            )
            session.add(row)
            await session.flush()
            return self._storage_info(row)

    async def validate_storage_profile(self, tenant_id: uuid.UUID, profile_id: uuid.UUID) -> ValidationReport:
        async with self._uow.read_only() as session:
            row = await self._get_storage_row(session, tenant_id, profile_id)
        return self._validate_storage(row)

    def _validate_storage(self, row: StorageProfileRow) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        config = row.config or {}
        for key in _STORAGE_REQUIRED_CONFIG.get(row.provider, ()):
            if not config.get(key):
                errors.append(f"缺少必填配置: {key}")
        credential_ref = str(config.get("credential_ref", ""))
        if credential_ref and not self._secrets.exists(credential_ref):
            errors.append("credential_ref 无法解析（密钥未配置或引用无效）")
        if row.provider == "local":
            root_ref = str(config.get("root_ref", ""))
            allowed = {r.id for r in self._settings.storage.local.roots}
            if root_ref and root_ref not in allowed:
                errors.append("root_ref 不在平台批准目录池中")
            root_path = str(config.get("root_path", ""))
            if root_path and root_ref:
                approved = next(r for r in self._settings.storage.local.roots if r.id == root_ref)
                if str(approved.path) != root_path:
                    errors.append("root_path 与 root_ref 批准目录不一致")
        else:
            if self._settings.network.mode == "offline":
                warnings.append("offline 模式下云端存储写入需在平台网络策略中显式批准对应端点")
        if not errors:
            try:
                self._router.binding_from_config(provider=row.provider, config=config, name=row.name)
            except Exception as exc:
                errors.append(f"配置无法构造存储后端: {exc.__class__.__name__}")
        return ValidationReport(ok=not errors, errors=errors, warnings=warnings)

    async def test_storage_profile(
        self,
        tenant_id: uuid.UUID,
        profile_id: uuid.UUID,
        *,
        include_write: bool,
    ) -> StorageTestReport:
        """显式 IO 测试：使用隔离前缀创建临时对象并清理，不触碰业务对象。"""
        async with self._uow.read_only() as session:
            row = await self._get_storage_row(session, tenant_id, profile_id)
            config = dict(row.config or {})
            name = row.name
            provider = row.provider
        report = self._validate_storage(row)
        if not report.ok:
            return StorageTestReport(
                ok=False, steps={"validate": "; ".join(report.errors)}, notes=report.warnings
            )
        binding = self._router.binding_from_config(provider=provider, config=config, name=name)
        steps: dict[str, str] = {"connect": "ok"}
        notes = [
            "写测试使用隔离前缀 owli-connection-test/ 并即时清理，不触碰业务对象",
        ]
        probe_key = f"owli-connection-test/{uuid.uuid4().hex}.bin"
        target = ObjectTarget(
            profile_revision_id=binding.profile_id or uuid.uuid5(uuid.NAMESPACE_URL, binding.name),
            bucket=binding.bucket,
            root_ref=binding.root_ref,
            key=probe_key,
        )
        locator = ObjectLocator(
            profile_revision_id=target.profile_revision_id,
            bucket=binding.bucket,
            root_ref=binding.root_ref,
            key=probe_key,
        )
        payload = b"owli-connection-test"
        try:
            if include_write:
                stored = binding.backend.put(
                    target,
                    iter([payload]),
                    PutOptions(content_type="application/octet-stream"),
                )
                steps["write"] = f"ok ({stored.size} bytes)"
            stat = binding.backend.stat(locator)
            steps["stat"] = "ok" if stat.exists else "missing"
            if stat.exists:
                reader = binding.backend.open_reader(locator, ByteRange(start=0, end_inclusive=7))
                try:
                    head = reader.read(8)
                finally:
                    reader.close()
                steps["range_read"] = "ok" if head == payload[:8] else "content_mismatch"
            binding.backend.delete(locator)
            steps["cleanup"] = "ok"
        except StorageError as exc:
            steps["io_error"] = exc.__class__.__name__
            return StorageTestReport(ok=False, steps=steps, notes=notes)
        ok = all(v.startswith("ok") for k, v in steps.items() if k != "notes")
        return StorageTestReport(ok=ok, steps=steps, notes=notes)

    async def publish_storage_profile(
        self, tenant_id: uuid.UUID, profile_id: uuid.UUID
    ) -> StorageProfileInfo:
        async with self._uow.begin() as session:
            row = await self._get_storage_row(session, tenant_id, profile_id)
            report = self._validate_storage(row)
            if not report.ok:
                raise ValidationFailedError("; ".join(report.errors))
            row.state = "published"
            row.updated_at = self._clock.now()
            await session.flush()
            self._record_config_revision(
                session,
                tenant_id=tenant_id,
                kind="storage_profile",
                resource_id=row.id,
                resource_name=row.name,
                revision=row.revision,
                validated_payload={
                    "name": row.name,
                    "provider": row.provider,
                    "revision": row.revision,
                    "config": row.config,
                },
                actor_id=row.created_by_id,
            )
            return self._storage_info(row)

    async def set_storage_profile_flags(
        self,
        tenant_id: uuid.UUID,
        profile_id: uuid.UUID,
        *,
        read_disabled: bool | None,
        write_disabled: bool | None,
    ) -> StorageProfileInfo:
        """停用写/读：关闭新写入不破坏历史读取，两者独立控制。"""
        async with self._uow.begin() as session:
            row = await self._get_storage_row(session, tenant_id, profile_id)
            if read_disabled is not None:
                row.read_disabled = read_disabled
            if write_disabled is not None:
                row.write_disabled = write_disabled
            row.updated_at = self._clock.now()
            await session.flush()
            return self._storage_info(row)

    # ------------------------------------------------------------------
    # 对象导入位置（source locations）
    # ------------------------------------------------------------------

    async def create_source_location(
        self,
        tenant_id: uuid.UUID,
        *,
        dataset_id: uuid.UUID,
        profile_revision_id: uuid.UUID,
        bucket: str,
        inbox_prefix: str,
    ) -> SourceLocationRow:
        """管理员为数据集绑定对象导入 inbox；不得与受管原件前缀重叠。"""
        clean_prefix = inbox_prefix.strip().strip("/")
        if not clean_prefix or ".." in clean_prefix.split("/"):
            raise InvalidInputError("inbox_prefix 必须是规范化相对前缀")
        if clean_prefix.startswith("tenants/"):
            raise InvalidInputError("inbox_prefix 不得与受管原件私有前缀（tenants/）重叠")
        async with self._uow.begin() as session:
            profile = await self._get_storage_row(session, tenant_id, profile_revision_id)
            if profile.provider == "local":
                raise InvalidInputError("local profile 不支持对象导入位置（需 S3/OSS）")
            row = SourceLocationRow(
                tenant_id=tenant_id,
                dataset_id=dataset_id,
                profile_revision_id=profile_revision_id,
                bucket=bucket,
                inbox_prefix=clean_prefix,
                enabled=True,
            )
            session.add(row)
            await session.flush()
            return row

    async def list_source_locations(
        self, tenant_id: uuid.UUID, *, dataset_id: uuid.UUID | None
    ) -> list[SourceLocationRow]:
        async with self._uow.read_only() as session:
            conditions = [SourceLocationRow.tenant_id == tenant_id]
            if dataset_id is not None:
                conditions.append(SourceLocationRow.dataset_id == dataset_id)
            rows = (
                (
                    await session.execute(
                        select(SourceLocationRow).where(*conditions).order_by(SourceLocationRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)

    async def set_source_location_enabled(
        self, tenant_id: uuid.UUID, location_id: uuid.UUID, *, enabled: bool
    ) -> SourceLocationRow:
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(SourceLocationRow).where(
                        SourceLocationRow.id == location_id,
                        SourceLocationRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("导入位置不存在")
            row.enabled = enabled
            row.updated_at = self._clock.now()
            await session.flush()
            return row

    # ------------------------------------------------------------------
    # 模型连接
    # ------------------------------------------------------------------

    async def create_model_connection(
        self,
        tenant_id: uuid.UUID,
        *,
        name: str,
        protocol: str,
        base_url: str,
        vendor_label: str | None,
        region: str | None,
        credential_ref: str | None,
        actor_id: uuid.UUID,
    ) -> ModelConnectionInfo:
        clean = name.strip()
        if not 1 <= len(clean) <= 128:
            raise ValidationFailedError("名称长度需在 1-128 之间")
        if protocol not in _MODEL_PROTOCOLS:
            raise InvalidInputError(f"不支持的协议: {protocol}（首期仅 openai_compatible）")
        if not base_url.startswith(("http://", "https://")):
            raise InvalidInputError("base_url 必须为 http(s) 地址")
        async with self._uow.begin() as session:
            row = ModelConnectionRow(
                tenant_id=tenant_id,
                name=clean,
                revision=1,
                protocol=protocol,
                vendor_label=vendor_label,
                base_url=base_url.rstrip("/"),
                region=region,
                network_policy={},
                state="draft",
                created_by_kind="user",
                created_by_id=actor_id,
            )
            if credential_ref:
                # 不做明文校验（write-only）；仅记录引用形状并做存在性检查
                if not self._secrets.exists(credential_ref):
                    raise ValidationFailedError("credential_ref 无法解析（密钥未配置）")
                row.network_policy = {**(row.network_policy or {}), "credential_ref": credential_ref}
                await self._upsert_secret_ref(
                    session,
                    tenant_id=tenant_id,
                    reference=credential_ref,
                    purpose="model_credential",
                    bound_endpoint=row.base_url,
                    actor_id=actor_id,
                )
            session.add(row)
            await session.flush()
            return self._connection_info(row)

    async def list_model_connections(self, tenant_id: uuid.UUID) -> list[ModelConnectionInfo]:
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(ModelConnectionRow)
                        .where(ModelConnectionRow.tenant_id == tenant_id)
                        .order_by(ModelConnectionRow.name, ModelConnectionRow.revision.desc())
                    )
                )
                .scalars()
                .all()
            )
        latest: dict[str, ModelConnectionRow] = {}
        for row in rows:
            latest.setdefault(row.name, row)
        return [self._connection_info(r) for r in sorted(latest.values(), key=lambda r: r.name)]

    async def validate_model_connection(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID
    ) -> ValidationReport:
        async with self._uow.read_only() as session:
            row = await self._get_connection_row(session, tenant_id, connection_id)
        return self._validate_connection(row)

    def _validate_connection(self, row: ModelConnectionRow) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        if row.protocol not in _MODEL_PROTOCOLS:
            errors.append(f"不支持的协议: {row.protocol}")
        if not row.base_url.startswith(("http://", "https://")):
            errors.append("base_url 必须为 http(s) 地址")
        from urllib.parse import urlparse

        host = (urlparse(row.base_url).hostname or "").lower()
        credential_ref = str((row.network_policy or {}).get("credential_ref", ""))
        if credential_ref and not self._secrets.exists(credential_ref):
            errors.append("credential_ref 无法解析")
        if self._settings.network.mode == "offline":
            approved = {entry.split(":")[0] for entry in self._settings.network.internal_allowlist}
            if host not in approved and host not in {"127.0.0.1", "localhost"}:
                warnings.append("offline 模式下该模型端点需在平台网络策略（internal_allowlist）中获批")
        return ValidationReport(ok=not errors, errors=errors, warnings=warnings)

    async def test_model_connection(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> ModelTestReport:
        """显式连接/认证验证：向端点发起一次轻量请求（可能计入配额/计费）。"""
        import time as _time

        import httpx

        async with self._uow.read_only() as session:
            row = await self._get_connection_row(session, tenant_id, connection_id)
            base_url = row.base_url
            credential_ref = str((row.network_policy or {}).get("credential_ref", ""))
            report = self._validate_connection(row)
        if not report.ok:
            return ModelTestReport(
                ok=False,
                steps={"validate": "; ".join(report.errors)},
                latency_ms=None,
                notes=report.warnings,
            )
        headers: dict[str, str] = {}
        if credential_ref:
            try:
                headers["Authorization"] = f"Bearer {self._secrets.resolve(credential_ref)}"
            except SecretUnavailable:
                return ModelTestReport(
                    ok=False,
                    steps={"auth": "credential_unresolvable"},
                    latency_ms=None,
                    notes=report.warnings,
                )
        notes = [
            "测试会对端点发起一次 GET /models 请求；部分供应商将其计入配额",
            "模型列表可手动录入；列表失败不代表有效模型不可用",
        ]
        steps: dict[str, str] = {}
        started = _time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self._settings.models.connect_timeout_seconds,
                    read=self._settings.models.request_timeout_seconds,
                    write=self._settings.models.request_timeout_seconds,
                    pool=self._settings.models.connect_timeout_seconds,
                ),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(f"{base_url}/models", headers=headers)
            latency_ms = int((_time.monotonic() - started) * 1000)
            steps["http"] = f"status={response.status_code}"
            if response.status_code in (401, 403):
                steps["auth"] = "rejected"
                return ModelTestReport(ok=False, steps=steps, latency_ms=latency_ms, notes=notes)
            if response.status_code >= 500:
                steps["server"] = "unavailable"
                return ModelTestReport(ok=False, steps=steps, latency_ms=latency_ms, notes=notes)
            steps["auth"] = "ok" if response.status_code != 401 else "rejected"
            steps["models_endpoint"] = "ok"
            # 测试报告不记录密钥与响应正文，只留计数
            try:
                body = response.json()
                count = len(body.get("data", [])) if isinstance(body, dict) else 0
                steps["models_count"] = str(count)
            except ValueError:
                steps["models_endpoint"] = "ok_non_json"
            return ModelTestReport(ok=True, steps=steps, latency_ms=latency_ms, notes=notes)
        except httpx.TimeoutException:
            latency_ms = int((_time.monotonic() - started) * 1000)
            steps["http"] = "timeout"
            return ModelTestReport(ok=False, steps=steps, latency_ms=latency_ms, notes=notes)
        except httpx.HTTPError:
            latency_ms = int((_time.monotonic() - started) * 1000)
            steps["http"] = "transport_error"
            return ModelTestReport(ok=False, steps=steps, latency_ms=latency_ms, notes=notes)

    async def update_model_connection(
        self,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        base_url: str | None,
        vendor_label: str | None,
        region: str | None,
        credential_ref: str | None,
        actor_id: uuid.UUID,
    ) -> ModelConnectionInfo:
        """修改创建新草稿 revision（不可变版本链，与存储 profile 同一模式）。"""
        if base_url is not None and not base_url.startswith(("http://", "https://")):
            raise InvalidInputError("base_url 必须为 http(s) 地址")
        async with self._uow.begin() as session:
            current = await self._get_connection_row(session, tenant_id, connection_id)
            max_revision = (
                await session.execute(
                    select(ModelConnectionRow.revision)
                    .where(
                        ModelConnectionRow.tenant_id == tenant_id,
                        ModelConnectionRow.name == current.name,
                    )
                    .order_by(ModelConnectionRow.revision.desc())
                    .limit(1)
                )
            ).scalar_one()
            policy = dict(current.network_policy or {})
            if credential_ref is not None:
                if credential_ref and not self._secrets.exists(credential_ref):
                    raise ValidationFailedError("credential_ref 无法解析（密钥未配置）")
                if credential_ref:
                    policy["credential_ref"] = credential_ref
                    await self._upsert_secret_ref(
                        session,
                        tenant_id=tenant_id,
                        reference=credential_ref,
                        purpose="model_credential",
                        bound_endpoint=base_url or current.base_url,
                        actor_id=actor_id,
                    )
                else:
                    policy.pop("credential_ref", None)
            row = ModelConnectionRow(
                tenant_id=tenant_id,
                name=current.name,
                revision=int(max_revision) + 1,
                protocol=current.protocol,
                vendor_label=vendor_label if vendor_label is not None else current.vendor_label,
                base_url=(base_url.rstrip("/") if base_url is not None else current.base_url),
                region=region if region is not None else current.region,
                network_policy=policy,
                state="draft",
                created_by_kind="user",
                created_by_id=actor_id,
            )
            session.add(row)
            await session.flush()
            return self._connection_info(row)

    async def publish_model_connection(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID
    ) -> ModelConnectionInfo:
        async with self._uow.begin() as session:
            row = await self._get_connection_row(session, tenant_id, connection_id)
            report = self._validate_connection(row)
            if not report.ok:
                raise ValidationFailedError("; ".join(report.errors))
            row.state = "published"
            row.updated_at = self._clock.now()
            await session.flush()
            self._record_config_revision(
                session,
                tenant_id=tenant_id,
                kind="model_connection",
                resource_id=row.id,
                resource_name=row.name,
                revision=row.revision,
                validated_payload={
                    "name": row.name,
                    "protocol": row.protocol,
                    "base_url": row.base_url,
                    "vendor_label": row.vendor_label,
                    "region": row.region,
                    "credential_configured": bool((row.network_policy or {}).get("credential_ref")),
                },
                actor_id=row.created_by_id,
            )
            return self._connection_info(row)

    async def record_connection_test(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, report: ModelTestReport
    ) -> None:
        """保存最近测试报告（脱敏：不含密钥与响应正文）；不改变发布状态。"""
        async with self._uow.begin() as session:
            row = await self._get_connection_row(session, tenant_id, connection_id)
            row.last_test_report = {
                "ok": report.ok,
                "steps": report.steps,
                "latency_ms": report.latency_ms,
                "notes": report.notes,
                "tested_at": self._clock.now().isoformat(),
            }
            row.updated_at = self._clock.now()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _get_storage_row(
        self, session: Any, tenant_id: uuid.UUID, profile_id: uuid.UUID
    ) -> StorageProfileRow:
        row = (
            await session.execute(
                select(StorageProfileRow).where(
                    StorageProfileRow.id == profile_id,
                    StorageProfileRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("存储 profile 不存在")
        return cast(StorageProfileRow, row)

    async def _get_connection_row(
        self, session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID
    ) -> ModelConnectionRow:
        row = (
            await session.execute(
                select(ModelConnectionRow).where(
                    ModelConnectionRow.id == connection_id,
                    ModelConnectionRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("模型连接不存在")
        return cast(ModelConnectionRow, row)

    @staticmethod
    def _storage_info(row: StorageProfileRow) -> StorageProfileInfo:
        return StorageProfileInfo(
            id=row.id,
            name=row.name,
            provider=row.provider,
            revision=row.revision,
            state=row.state,
            config=dict(row.config or {}),
            read_disabled=row.read_disabled,
            write_disabled=row.write_disabled,
            created_at=row.created_at,
        )

    @staticmethod
    def _connection_info(row: ModelConnectionRow) -> ModelConnectionInfo:
        return ModelConnectionInfo(
            id=row.id,
            name=row.name,
            protocol=row.protocol,
            vendor_label=row.vendor_label,
            base_url=row.base_url,
            region=row.region,
            revision=row.revision,
            state=row.state,
            credential_configured=bool((row.network_policy or {}).get("credential_ref")),
            last_test_report=row.last_test_report,
            created_at=row.created_at,
        )


_MODEL_CAPABILITIES = frozenset({"generation", "embedding", "rerank", "vision"})

_SECRET_LIKE_KEYS = frozenset(
    {"secret", "secret_key", "access_key", "access_key_id", "access_key_secret", "password", "token"}
)


def _reject_secret_values(config: dict[str, Any]) -> None:
    """配置快照只允许引用：检测疑似明文密钥字段，一律拒绝保存。"""
    for key, value in config.items():
        if key.lower() in _SECRET_LIKE_KEYS and isinstance(value, str) and value and ":" not in value:
            raise InvalidInputError(f"config.{key} 疑似明文密钥；请改用 credential_ref（env:/file: 引用）")
