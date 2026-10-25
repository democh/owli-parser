"""启动配置模型：类型化快照，集中校验。

- 优先级：init 参数 > OWLI_ 环境变量 > OWLI_CONFIG_FILE 覆盖 YAML > resources/defaults.yaml。
- 所有模型 extra="forbid"：未知键、非法单位直接报错，不做静默忽略。
- 嵌套对象深合并、数组整体替换；不做无条件字典覆盖。
- 交叉校验：heartbeat < lease、overlap < target <= max、top_k <= candidate_limit、
  part 与总大小约束、Session idle < absolute、Token 默认 TTL <= 上限。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from owli_parser.core.errors import ConfigInvalidError

BOOT_SCHEMA_VERSION = "1.0"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppSection(_StrictModel):
    name: str = "owli-parser"
    environment: Literal["development", "staging", "production"] = "development"
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8000, ge=1, le=65535)
    public_base_url: str = "http://127.0.0.1:8000"


class HttpSection(_StrictModel):
    cors_allowed_origins: list[str] = Field(default_factory=list)
    trusted_proxies: list[str] = Field(default_factory=list)
    request_timeout_seconds: int = Field(default=120, gt=0)


class DatabaseSection(_StrictModel):
    url: str = "sqlite+aiosqlite:///./owli_dev.db"
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=20, ge=0)
    pool_timeout_seconds: int = Field(default=30, gt=0)
    pool_recycle_seconds: int = Field(default=1800, gt=0)
    statement_timeout_ms: int = Field(default=15000, gt=0)


class BrokerSection(_StrictModel):
    url: str = "redis://127.0.0.1:6379/0"
    visibility_timeout_seconds: int = Field(default=3600, gt=0)
    socket_timeout_seconds: int = Field(default=5, gt=0)
    worker_prefetch_multiplier: int = Field(default=1, ge=1)


class SessionSection(_StrictModel):
    cookie_name: str = "owli_session"
    idle_ttl_seconds: int = Field(default=86400, gt=0)
    absolute_ttl_seconds: int = Field(default=604800, gt=0)
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"


class PasswordSection(_StrictModel):
    min_length: int = Field(default=12, ge=8)
    max_length: int = Field(default=256, ge=64)


class LoginRateLimitSection(_StrictModel):
    max_attempts: int = Field(default=10, ge=1)
    window_seconds: int = Field(default=300, gt=0)


class AuthSection(_StrictModel):
    session: SessionSection = Field(default_factory=SessionSection)
    password: PasswordSection = Field(default_factory=PasswordSection)
    login_rate_limit: LoginRateLimitSection = Field(default_factory=LoginRateLimitSection)
    session_pepper_ref: str = "env:OWLI_SESSION_PEPPER"
    csrf_header_name: str = "x-owli-csrf"
    ws_ticket_ttl_seconds: int = Field(default=60, gt=0)


class TokensSection(_StrictModel):
    ttl_default_seconds: int = Field(default=7776000, gt=0)
    ttl_max_seconds: int = Field(default=31536000, gt=0)
    rotation_overlap_seconds: int = Field(default=86400, ge=0)
    pepper_ref: str = "env:OWLI_TOKEN_PEPPER"
    prefix: str = "owli_pat"


class UploadSection(_StrictModel):
    direct_max_bytes: int = Field(default=104857600, gt=0)
    total_max_bytes: int = Field(default=1073741824, gt=0)
    part_size_bytes: int = Field(default=8388608, gt=0)
    part_max_count: int = Field(default=10000, ge=1)
    session_ttl_seconds: int = Field(default=86400, gt=0)
    temp_file_ttl_seconds: int = Field(default=86400, gt=0)


class ImportsSection(_StrictModel):
    max_redirects: int = Field(default=3, ge=0, le=10)
    connect_timeout_seconds: int = Field(default=10, gt=0)
    read_timeout_seconds: int = Field(default=30, gt=0)
    total_timeout_seconds: int = Field(default=600, gt=0)
    stalled_timeout_seconds: int = Field(default=60, gt=0)
    max_download_bytes: int = Field(default=1073741824, gt=0)
    max_decompressed_bytes: int = Field(default=2147483648, gt=0)
    allowed_ports: list[int] = Field(default_factory=lambda: [80, 443])
    allowed_schemes: list[str] = Field(default_factory=lambda: ["http", "https"])
    parallel_downloads: int = Field(default=2, ge=1)
    retryable_status_codes: list[int] = Field(default_factory=lambda: [408, 425, 429, 500, 502, 503, 504])
    mime_allowlist: list[str] = Field(default_factory=list)
    url_secret_key_ref: str = "env:OWLI_SOURCE_URL_KEY"  # noqa: S105 - 密钥引用名而非密钥
    temp_dir_ref: str = "local:imports"


class NetworkSection(_StrictModel):
    mode: Literal["offline", "hybrid"] = "offline"
    allow_private_networks: bool = False
    internal_allowlist: list[str] = Field(default_factory=list)
    proxy_url: str | None = None
    tls_ca_bundle: str | None = None


class QueuesSection(_StrictModel):
    route: str = "q.route"
    cpu: str = "q.cpu"
    gpu: str = "q.gpu"
    post: str = "q.post"
    dlq: str = "q.dlq"


class JobsSection(_StrictModel):
    queues: QueuesSection = Field(default_factory=QueuesSection)
    max_attempts: int = Field(default=3, ge=1)
    # 复核登记：raw_score 严格小于该阈值的块进入复核队列；未评估置信度（None）不登记
    review_low_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    lease_seconds: int = Field(default=120, gt=0)
    heartbeat_seconds: int = Field(default=30, gt=0)
    stage_deadline_seconds: int = Field(default=1800, gt=0)
    queue_timeout_seconds: int = Field(default=3600, gt=0)
    retry_backoff_seconds: int = Field(default=5, gt=0)
    retry_backoff_max_seconds: int = Field(default=300, gt=0)
    cpu_slots: int = Field(default=4, ge=0)
    gpu_slots: int = Field(default=0, ge=0)
    page_range_size: int = Field(default=50, ge=1)
    page_range_overlap: int = Field(default=1, ge=0, le=2)
    ocr_dpi: int = Field(default=200, ge=72, le=600)
    partial_publish_min_coverage: float = Field(default=0.6, ge=0.0, le=1.0)
    outbox_poll_seconds: int = Field(default=2, gt=0)
    outbox_batch_size: int = Field(default=50, ge=1)
    lease_sweep_seconds: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def _check_timing(self) -> JobsSection:
        if not self.heartbeat_seconds < self.lease_seconds:
            raise ValueError("heartbeat_seconds 必须小于 lease_seconds")
        if self.retry_backoff_seconds > self.retry_backoff_max_seconds:
            raise ValueError("retry_backoff_seconds 不得大于 retry_backoff_max_seconds")
        return self


class ParsingSection(_StrictModel):
    default_pipeline: str = "default"
    max_html_bytes: int = Field(default=2097152, gt=0)
    archive_max_entries: int = Field(default=1000, ge=1)
    archive_max_depth: int = Field(default=3, ge=1)
    archive_max_ratio: int = Field(default=100, ge=1)
    safe_text_encoding: str = "utf-8"
    # 外部 adapter 心跳有效期：超过该时长未上报视为 unavailable（读时判定，不改写行）
    adapter_heartbeat_ttl_seconds: int = Field(default=90, gt=0)


class ChunkSection(_StrictModel):
    tokenizer_ref: str = "approx:chars4-v1"
    target_tokens: int = Field(default=400, gt=0)
    max_tokens: int = Field(default=800, gt=0)
    overlap_tokens: int = Field(default=50, ge=0)
    parent_max_tokens: int = Field(default=2000, gt=0)
    table_row_group_target_tokens: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def _check_budget(self) -> ChunkSection:
        if not self.overlap_tokens < self.target_tokens:
            raise ValueError("overlap_tokens 必须小于 target_tokens")
        if self.target_tokens > self.max_tokens:
            raise ValueError("target_tokens 不得大于 max_tokens")
        return self


class QdrantSection(_StrictModel):
    url: str = "http://127.0.0.1:6333"
    collection: str = "owli_chunks"
    api_key_ref: str | None = None
    timeout_seconds: float = Field(default=10, gt=0)
    verify_tls: bool = True


class OpenSearchSection(_StrictModel):
    url: str = "http://127.0.0.1:9200"
    index: str = "owli_chunks"
    username_ref: str | None = None
    password_ref: str | None = None
    api_key_ref: str | None = None
    timeout_seconds: float = Field(default=10, gt=0)
    verify_tls: bool = True
    refresh_on_write: bool = True

    @model_validator(mode="after")
    def _check_auth(self) -> OpenSearchSection:
        if self.api_key_ref and (self.username_ref or self.password_ref):
            raise ValueError("OpenSearch api_key 与 username/password 不得同时配置")
        if bool(self.username_ref) != bool(self.password_ref):
            raise ValueError("OpenSearch username_ref 与 password_ref 必须成对配置")
        return self


class RetrievalSection(_StrictModel):
    enabled: bool = False
    # memory 仅供开发/测试；生产必须显式切换到真实引擎，避免多进程索引不共享。
    dense_backend: Literal["memory", "qdrant"] = "memory"
    lexical_backend: Literal["memory", "opensearch"] = "memory"
    qdrant: QdrantSection = Field(default_factory=QdrantSection)
    opensearch: OpenSearchSection = Field(default_factory=OpenSearchSection)
    # 索引写入端启用的通道：lexical（BM25，无模型依赖）默认可用；
    # dense 需要 embedding 用途绑定（M3 模型配置）后才可加入，
    # 配置要求 dense 但未绑定时任务显式失败，不静默降级。
    channels: list[str] = Field(default_factory=lambda: ["lexical"])
    per_channel_limit: int = Field(default=50, ge=1)
    candidate_limit: int = Field(default=100, ge=1)
    top_k: int = Field(default=10, ge=1)
    rrf_k: int = Field(default=60, ge=1)
    channel_weights: dict[str, float] = Field(default_factory=lambda: {"dense": 1.0, "lexical": 1.0})
    rerank_model_ref: str | None = None
    min_relevance_threshold: float = 0.0

    @model_validator(mode="after")
    def _check_limits(self) -> RetrievalSection:
        if self.top_k > self.candidate_limit:
            raise ValueError("top_k 不得大于 candidate_limit")
        if self.per_channel_limit > self.candidate_limit:
            raise ValueError("per_channel_limit 不得大于 candidate_limit")
        unknown = set(self.channels) - {"dense", "lexical"}
        if unknown:
            raise ValueError(f"未知索引通道：{sorted(unknown)}")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("通道不得重复")
        return self


class LocalRootSection(_StrictModel):
    id: str
    path: str
    quota_bytes: int | None = None


class LocalStorageSection(_StrictModel):
    roots: list[LocalRootSection] = Field(default_factory=list)


class StorageSection(_StrictModel):
    default_profile_ref: str = "local-primary"
    presign_ttl_seconds: int = Field(default=300, gt=0)
    proxy_download_default: bool = True
    local: LocalStorageSection = Field(default_factory=LocalStorageSection)


class ModelsSection(_StrictModel):
    request_timeout_seconds: int = Field(default=120, gt=0)
    connect_timeout_seconds: int = Field(default=10, gt=0)
    max_concurrent_calls: int = Field(default=4, ge=1)
    default_generation_profile_ref: str | None = None
    default_embedding_profile_ref: str | None = None
    default_rerank_profile_ref: str | None = None
    default_vision_profile_ref: str | None = None


class RetentionSection(_StrictModel):
    orphan_artifact_ttl_seconds: int = Field(default=86400, gt=0)
    upload_session_ttl_seconds: int = Field(default=86400, gt=0)
    job_event_retention_days: int = Field(default=30, ge=1)
    audit_retention_days: int = Field(default=365, ge=1)
    idempotency_retention_seconds: int = Field(default=604800, gt=0)
    cleanup_batch_size: int = Field(default=500, ge=1)
    disk_low_watermark_ratio: float = Field(default=0.1, ge=0.0, lt=1.0)
    # 软删文档：超过保留期后由 scheduler 回收索引/对象/行
    deleted_document_retention_days: int = Field(default=30, ge=1)
    gc_interval_seconds: int = Field(default=3600, ge=1)


class ObservabilitySection(_StrictModel):
    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True
    trace_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)


class UiSection(_StrictModel):
    page_size: int = Field(default=50, ge=1)
    list_max_limit: int = Field(default=200, ge=1)
    job_poll_interval_seconds: int = Field(default=5, gt=0)
    ws_reconnect_max_seconds: int = Field(default=30, gt=0)


class AppSettings(BaseSettings):
    """完整启动配置快照。业务代码只消费本对象，不直接 os.getenv。"""

    model_config = SettingsConfigDict(
        env_prefix="OWLI_",
        env_nested_delimiter="__",
        extra="forbid",
        validate_default=True,
    )

    schema_version: str = BOOT_SCHEMA_VERSION
    app: AppSection = Field(default_factory=AppSection)
    http: HttpSection = Field(default_factory=HttpSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    broker: BrokerSection = Field(default_factory=BrokerSection)
    auth: AuthSection = Field(default_factory=AuthSection)
    tokens: TokensSection = Field(default_factory=TokensSection)
    upload: UploadSection = Field(default_factory=UploadSection)
    imports: ImportsSection = Field(default_factory=ImportsSection)
    network: NetworkSection = Field(default_factory=NetworkSection)
    jobs: JobsSection = Field(default_factory=JobsSection)
    parsing: ParsingSection = Field(default_factory=ParsingSection)
    chunk: ChunkSection = Field(default_factory=ChunkSection)
    retrieval: RetrievalSection = Field(default_factory=RetrievalSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    models: ModelsSection = Field(default_factory=ModelsSection)
    retention: RetentionSection = Field(default_factory=RetentionSection)
    observability: ObservabilitySection = Field(default_factory=ObservabilitySection)
    ui: UiSection = Field(default_factory=UiSection)

    @model_validator(mode="after")
    def _check_cross(self) -> AppSettings:
        if self.auth.session.idle_ttl_seconds > self.auth.session.absolute_ttl_seconds:
            raise ValueError("session idle TTL 不得大于 absolute TTL")
        if self.tokens.ttl_default_seconds > self.tokens.ttl_max_seconds:
            raise ValueError("Token 默认 TTL 不得大于上限")
        up = self.upload
        if not up.part_size_bytes <= up.direct_max_bytes <= up.total_max_bytes:
            raise ValueError("part_size <= direct_max <= total_max 约束不满足")
        if up.part_size_bytes * up.part_max_count < up.total_max_bytes:
            raise ValueError("part_size * part_max_count 必须覆盖 total_max_bytes")
        if self.database.url.startswith("sqlite") and self.app.environment == "production":
            raise ValueError("生产环境不允许 sqlite 数据库")
        if self.network.mode == "offline" and self.network.proxy_url:
            raise ValueError("offline 模式不得配置出口代理")
        if self.parsing.archive_max_ratio < 1:
            raise ValueError("archive_max_ratio 必须 >= 1")
        return self


def _load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigInvalidError(f"无法读取配置文件: {path.name}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigInvalidError(f"配置文件 YAML 解析失败: {path.name}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigInvalidError(f"配置文件顶层必须是对象: {path.name}")
    return data


class YamlSettingsSource(PydanticBaseSettingsSource):
    """显式 YAML 配置源（不依赖 pydantic-settings 的隐式自动读取）。"""

    def __init__(
        self, settings_cls: type[BaseSettings], yaml_path: Path | None, required: bool = False
    ) -> None:
        super().__init__(settings_cls)
        self._yaml_path = yaml_path
        self._required = required

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # 整字典由 __call__ 提供，这里不使用逐字段协议
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if self._yaml_path is None:
            if self._required:
                raise ConfigInvalidError("OWLI_CONFIG_FILE 指向的配置文件不存在")
            return {}
        if not self._yaml_path.is_file():
            if self._required:
                raise ConfigInvalidError("OWLI_CONFIG_FILE 指向的配置文件不存在")
            return {}
        return _load_yaml_file(self._yaml_path)


def defaults_yaml_path() -> Path:
    from importlib.resources import files

    return Path(str(files("owli_parser.resources").joinpath("defaults.yaml")))


def load_boot_settings(**overrides: Any) -> AppSettings:
    """加载启动配置：defaults.yaml -> OWLI_CONFIG_FILE -> OWLI_ 环境变量 -> init。

    加载顺序在 customise_settings 中体现为优先级从低到高。
    """
    override_path_raw = overrides.pop("config_file", None) or os.environ.get("OWLI_CONFIG_FILE")
    override_path = Path(override_path_raw) if override_path_raw else None

    class _Configured(AppSettings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            # 顺序即优先级：靠前优先级高
            return (
                init_settings,
                env_settings,
                # OWLI_CONFIG_FILE 由管理员指定；显式指向但文件缺失时报错
                YamlSettingsSource(settings_cls, override_path, required=override_path is not None),
                YamlSettingsSource(settings_cls, defaults_yaml_path()),
                file_secret_settings,
            )

    try:
        return _Configured(**overrides)
    except ConfigInvalidError:
        raise
    except Exception as exc:  # pydantic ValidationError -> 稳定错误码
        raise ConfigInvalidError(f"启动配置校验失败: {exc}") from exc


def validate_config(settings: AppSettings) -> list[str]:
    """独立校验入口（owli config validate）：返回告警列表；不通过则抛错。"""
    warnings: list[str] = []
    if settings.network.mode == "offline" and settings.network.internal_allowlist:
        warnings.append("offline 模式下的内部 allowlist 仅在平台批准后生效")
    if not settings.storage.local.roots and settings.storage.default_profile_ref == "local-primary":
        warnings.append("未配置 local 存储 root，本地存储 profile 将在首次使用时失败")
    if settings.retrieval.enabled is False:
        warnings.append("retrieval profile 未启用，检索相关接口将返回 capability 不可用")
    elif settings.app.environment == "production":
        if settings.retrieval.dense_backend == "memory":
            warnings.append("生产环境 dense 仍为 memory：索引不跨进程共享，需切换为 qdrant")
        if settings.retrieval.lexical_backend == "memory":
            warnings.append("生产环境 lexical 仍为 memory：索引不跨进程共享，需切换为 opensearch")
    return warnings
