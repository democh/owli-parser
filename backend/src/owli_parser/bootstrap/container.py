"""依赖容器：集中组装设置、基础设施与各模块服务。

- 不在模块 import 时读取配置、连接 DB 或加载模型；容器在进程启动时构建一次。
- 资源池按连接 revision 建立；worker/API/scheduler 共享同一组装逻辑。
"""

from __future__ import annotations

import asyncio
import uuid as uuid_module
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from owli_parser.adapters.network.safe_fetcher import HttpxSafeFetcher
from owli_parser.adapters.retrieval.in_memory import InMemoryDenseIndex, InMemoryLexicalIndex
from owli_parser.adapters.source_secrets import EncryptedSourceStore
from owli_parser.bootstrap.model_wiring import (
    make_embedding_provider_resolver,
    make_generation_resolver,
    make_rerank_provider_resolver,
)
from owli_parser.bootstrap.settings import AppSettings, load_boot_settings
from owli_parser.core.clock import Clock, SystemClock
from owli_parser.infrastructure.crosscut import (
    SqlAuditRecorder,
    SqlIdempotencyStore,
    SqlOutboxRecorder,
)
from owli_parser.infrastructure.db import create_async_engine_from_url, create_session_factory
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.access.service import AccessService
from owli_parser.modules.artifacts.service import ArtifactService, StorageRouter
from owli_parser.modules.catalog.service import CatalogService
from owli_parser.modules.chat.service import ChatService
from owli_parser.modules.configuration.service import ConfigurationService
from owli_parser.modules.identity.service import IdentityService
from owli_parser.modules.ingestion.service import ImportService, UploadService
from owli_parser.modules.jobs.service import JobService
from owli_parser.modules.knowledge.service import KnowledgeService
from owli_parser.modules.retention.service import RetentionService
from owli_parser.modules.retrieval.indexing import IndexPublicationService
from owli_parser.modules.retrieval.service import RetrievalService
from owli_parser.ports.retrieval import DenseIndex, LexicalIndex
from owli_parser.ports.secrets import EnvSecretResolver, SecretResolver
from owli_parser.workflows.download_import import DownloadImportWorkflow
from owli_parser.workflows.run_parsing_job import RunParsingJobWorkflow
from owli_parser.workflows.submit_parsing import SubmitParsingWorkflow

if TYPE_CHECKING:
    from owli_parser.modules.chat.service import EvidenceInput
    from owli_parser.modules.retrieval.domain import FusedHit


class Container:
    def __init__(self, settings: AppSettings | None = None, clock: Clock | None = None) -> None:
        self.settings = settings or load_boot_settings()
        self.clock = clock or SystemClock()
        self.engine = create_async_engine_from_url(
            self.settings.database.url,
            pool_size=self.settings.database.pool_size,
            max_overflow=self.settings.database.max_overflow,
            pool_timeout_seconds=self.settings.database.pool_timeout_seconds,
            pool_recycle_seconds=self.settings.database.pool_recycle_seconds,
        )
        self.session_factory = create_session_factory(self.engine)
        self.uow = UnitOfWork(self.session_factory)
        self.secrets: SecretResolver = EnvSecretResolver()

        # 横切基础设施
        self.audit = SqlAuditRecorder(self.session_factory)
        self.outbox = SqlOutboxRecorder(self.session_factory)
        self.idempotency = SqlIdempotencyStore(
            self.session_factory,
            self.clock,
            retention_seconds=self.settings.retention.idempotency_retention_seconds,
        )

        # 存储与产物
        self.storage_router = StorageRouter(self.settings, self.secrets, self.session_factory)
        self.artifacts = ArtifactService(self.uow, self.clock, self.storage_router)

        # 模块服务
        self.catalog = CatalogService(self.uow, self.clock)
        self.configuration = ConfigurationService(
            self.uow, self.clock, self.settings, self.secrets, self.storage_router
        )
        self.jobs = JobService(self.uow, self.clock, lease_seconds=self.settings.jobs.lease_seconds)
        self.knowledge = KnowledgeService(self.uow, self.clock, self.settings.chunk, outbox=self.outbox)
        # 检索索引按集中配置装配：memory 仅开发/测试，生产使用 Qdrant/OpenSearch。
        self.dense_index, self.lexical_index = self._build_retrieval_indexes()
        self.retrieval = RetrievalService(
            self.settings.retrieval,
            dense_index=self.dense_index,
            lexical_index=self.lexical_index,
            rerank_provider_resolver=make_rerank_provider_resolver(self),
            text_loader=self._load_chunk_texts,
        )
        # embedding 提供者按租户从“用途绑定 → 档案 → 连接”解析（见 model_wiring）：
        # 未配置绑定时 dense 不可用，配置显式要求 dense 时任务以
        # capability_not_ready 失败（不静默降级）。
        # embedding_transport 为测试注入点（MockTransport）；惰性创建共享 client。
        self.embedding_transport: httpx.AsyncBaseTransport | None = None
        self._model_http: httpx.AsyncClient | None = None
        self.index_publication = IndexPublicationService(
            self.uow,
            self.knowledge,
            self.settings.retrieval,
            dense_index=self.dense_index,
            lexical_index=self.lexical_index,
            embedding_provider_resolver=make_embedding_provider_resolver(self),
            outbox=self.outbox,
        )

        # 软删文档保留期回收（scheduler 周期调用；索引/对象/行）
        self.retention = RetentionService(
            self.uow,
            self.clock,
            retention_days=self.settings.retention.deleted_document_retention_days,
            batch_size=self.settings.retention.cleanup_batch_size,
            artifacts=self.artifacts,
            index_publication=self.index_publication,
        )

        # 问答（M4）：证据检索与生成均注入；未配置 generation 绑定时运行以
        # capability_not_ready 失败（不静默降级）。
        self.chat = ChatService(
            self.uow,
            self.clock,
            search_evidence=self._search_evidence,
            generate_resolver=make_generation_resolver(self),
        )

        # 认证与授权（pepper 在启动时解析一次；失败即拒绝启动，不静默降级）
        session_pepper = self.secrets.resolve(self.settings.auth.session_pepper_ref)
        token_pepper = self.secrets.resolve(self.settings.tokens.pepper_ref)
        self.identity = IdentityService(
            self.uow,
            self.clock,
            self.settings.auth,
            pepper=session_pepper,
            audit=self.audit,
        )
        self.access = AccessService(
            self.uow,
            self.clock,
            self.settings.tokens,
            pepper=token_pepper,
            audit=self.audit,
        )

        # 接入
        self._temp_root = self._resolve_temp_root()
        self.fetcher = HttpxSafeFetcher()
        self.source_secrets = EncryptedSourceStore(
            self._temp_root / "source_secrets",
            self.settings.imports.url_secret_key_ref,
            self.secrets,
        )
        self.uploads = UploadService(
            self.uow,
            self.clock,
            self.settings,
            self.catalog,
            self.artifacts,
            self.audit,
            self._temp_root,
        )
        self.imports = ImportService(
            self.uow,
            self.clock,
            self.settings,
            self.catalog,
            self.artifacts,
            self.fetcher,
            self.source_secrets,
            self.audit,
            self._temp_root,
        )

        # 工作流
        self.submit_parsing = SubmitParsingWorkflow(
            self.uow,
            self.clock,
            self.settings,
            self.catalog,
            self.jobs,
            self.artifacts,
            self.outbox,
            configuration=self.configuration,
        )
        self.run_parsing_job = RunParsingJobWorkflow(
            self.uow,
            self.clock,
            self.settings,
            self.session_factory,
            self.catalog,
            self.jobs,
            self.artifacts,
            self.knowledge,
            self.outbox,
            index_publication=self.index_publication,
        )
        self.download_import = DownloadImportWorkflow(
            self.uow,
            self.clock,
            self.settings,
            self.imports,
            self.submit_parsing,
            self.outbox,
            self.secrets,
        )

    def _build_retrieval_indexes(self) -> tuple[DenseIndex | None, LexicalIndex | None]:
        """按配置装配真实索引；关闭检索时保留内存实现以支持离线测试。"""
        settings = self.settings.retrieval
        if not settings.enabled:
            return InMemoryDenseIndex(), InMemoryLexicalIndex()

        dense: DenseIndex | None
        if settings.dense_backend == "qdrant":
            from owli_parser.adapters.retrieval.qdrant import QdrantDenseIndex

            api_key = (
                self.secrets.resolve(settings.qdrant.api_key_ref) if settings.qdrant.api_key_ref else None
            )
            dense = QdrantDenseIndex(
                url=settings.qdrant.url,
                collection=settings.qdrant.collection,
                api_key=api_key,
                timeout_seconds=settings.qdrant.timeout_seconds,
                verify_tls=settings.qdrant.verify_tls,
            )
        else:
            dense = InMemoryDenseIndex()

        lexical: LexicalIndex | None
        if settings.lexical_backend == "opensearch":
            from owli_parser.adapters.retrieval.opensearch import OpenSearchLexicalIndex

            os_settings = settings.opensearch
            username = self.secrets.resolve(os_settings.username_ref) if os_settings.username_ref else None
            password = self.secrets.resolve(os_settings.password_ref) if os_settings.password_ref else None
            api_key = self.secrets.resolve(os_settings.api_key_ref) if os_settings.api_key_ref else None
            lexical = OpenSearchLexicalIndex(
                url=os_settings.url,
                index=os_settings.index,
                username=username,
                password=password,
                api_key=api_key,
                timeout_seconds=os_settings.timeout_seconds,
                verify_tls=os_settings.verify_tls,
                refresh_on_write=os_settings.refresh_on_write,
            )
        else:
            lexical = InMemoryLexicalIndex()
        return dense, lexical

    async def check_retrieval_health(self) -> bool:
        """就绪探针：真实检索适配器有心跳方法时必须可用；内存实现不探测。"""
        if not self.settings.retrieval.enabled:
            return True
        for index in (self.dense_index, self.lexical_index):
            probe = getattr(index, "check_health", None)
            if callable(probe) and not await asyncio.to_thread(probe):
                return False
        return True

    def _resolve_temp_root(self) -> Path:
        roots = self.settings.storage.local.roots
        if roots:
            root = Path(roots[0].path)
        else:
            import tempfile

            root = Path(tempfile.gettempdir()) / "owli-parser"
        temp_root = root / ".tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        return temp_root

    async def _search_evidence(
        self, tenant_id: uuid_module.UUID, dataset_ids: list[str], query: str
    ) -> list[EvidenceInput]:
        """问答证据检索：检索管线 + PG 权威校验 + 证据快照材料组装。

        - query 向量：存在 embedding 绑定时实时生成；失败则仅用词法通道（降级不伪造）；
        - 权威校验：索引 id 解析 (chunk_id, revision) → PG 校验 enabled/current_revision；
        - 证据材料：正文与 source_refs 从 chunk_revisions 精确 revision 读取。
        """
        from sqlalchemy import select

        from owli_parser.modules.chat.service import EvidenceInput
        from owli_parser.modules.knowledge.models import ChunkRevisionRow, ChunkRow

        def parse_index_id(raw: str) -> tuple[uuid_module.UUID, int] | None:
            parts = raw.split(":")
            if len(parts) != 5:
                return None
            try:
                return uuid_module.UUID(parts[2]), int(parts[3])
            except ValueError:
                return None

        query_vector: list[float] | None = None
        try:
            embedding_provider = await make_embedding_provider_resolver(self)(tenant_id)
            if embedding_provider is not None:
                query_vector = list((await embedding_provider([query]))[0])
        except Exception:
            query_vector = None

        async def authority(candidates: Sequence[FusedHit]) -> set[str]:
            specs: dict[uuid_module.UUID, int] = {}
            for hit in candidates:
                parsed = parse_index_id(hit.id)
                if parsed is not None:
                    specs[parsed[0]] = parsed[1]
            if not specs:
                return set()
            async with self.uow.read_only() as session:
                rows = (
                    await session.execute(
                        select(ChunkRow.id, ChunkRow.current_revision, ChunkRow.enabled).where(
                            ChunkRow.tenant_id == tenant_id,
                            ChunkRow.id.in_(list(specs)),
                        )
                    )
                ).all()
            allowed = {
                chunk_id
                for chunk_id, current_revision, enabled in rows
                if enabled and specs.get(chunk_id) == current_revision
            }
            resolved: set[str] = set()
            for hit in candidates:
                parsed_hit = parse_index_id(hit.id)
                if parsed_hit is not None and parsed_hit[0] in allowed:
                    resolved.add(hit.id)
            return resolved

        search = await self.retrieval.search(
            tenant_id=str(tenant_id),
            query=query,
            query_vector=query_vector,
            dataset_ids=dataset_ids or None,
            acl_refs=[f"dataset:{value}" for value in dataset_ids] or None,
            authoritative_filter=authority,
        )
        if not search.items:
            return []

        chunk_ids = [parsed[0] for item in search.items if (parsed := parse_index_id(item.id))]
        if not chunk_ids:
            return []
        async with self.uow.read_only() as session:
            chunk_rows = (
                await session.execute(
                    select(
                        ChunkRow.id,
                        ChunkRow.doc_id,
                        ChunkRow.version_id,
                        ChunkRow.acl_ref,
                        ChunkRow.current_revision,
                        ChunkRow.enabled,
                    ).where(
                        ChunkRow.tenant_id == tenant_id,
                        ChunkRow.id.in_(chunk_ids),
                    )
                )
            ).all()
            revision_rows = (
                await session.execute(
                    select(
                        ChunkRevisionRow.chunk_id,
                        ChunkRevisionRow.revision,
                        ChunkRevisionRow.text,
                        ChunkRevisionRow.source_refs,
                    ).where(ChunkRevisionRow.chunk_id.in_(chunk_ids))
                )
            ).all()
        chunk_map = {row[0]: row for row in chunk_rows}
        revision_map = {(row[0], row[1]): (row[2], row[3]) for row in revision_rows}

        evidences: list[EvidenceInput] = []
        for item in search.items:
            parsed = parse_index_id(item.id)
            if parsed is None:
                continue
            chunk_id, revision = parsed
            chunk_row = chunk_map.get(chunk_id)
            if chunk_row is None or not chunk_row[5] or chunk_row[4] != revision:
                continue
            material = revision_map.get((chunk_id, revision))
            if material is None or not material[0]:
                continue
            evidences.append(
                EvidenceInput(
                    doc_id=chunk_row[1],
                    version_id=chunk_row[2],
                    chunk_id=chunk_id,
                    chunk_revision=revision,
                    acl_refs=[chunk_row[3]],
                    text=material[0],
                    source_refs=list(material[1] or []),
                    score=item.rrf_score,
                )
            )
        return evidences

    async def _load_chunk_texts(self, tenant_id: str, ids: Sequence[str]) -> dict[str, str]:
        """rerank 正文加载（PG 权威回查；id 格式 tenant:version:chunk:revision:gen）。

        索引态文本不作为事实源；无法解析或缺失的 id 不进结果（上层按缺失降级）。
        """
        import uuid as uuid_module

        from sqlalchemy import select

        from owli_parser.modules.knowledge.models import ChunkRevisionRow

        parsed: list[tuple[str, uuid_module.UUID, int]] = []
        for raw in ids:
            parts = raw.split(":")
            if len(parts) != 5:
                continue
            try:
                chunk_id = uuid_module.UUID(parts[2])
                revision = int(parts[3])
            except ValueError:
                continue
            parsed.append((raw, chunk_id, revision))
        if not parsed:
            return {}
        async with self.uow.read_only() as session:
            rows = (
                await session.execute(
                    select(
                        ChunkRevisionRow.chunk_id,
                        ChunkRevisionRow.revision,
                        ChunkRevisionRow.text,
                    ).where(ChunkRevisionRow.chunk_id.in_([chunk_id for _, chunk_id, _ in parsed]))
                )
            ).all()
        texts = {(row[0], row[1]): row[2] for row in rows}
        result: dict[str, str] = {}
        for raw, chunk_id, revision in parsed:
            text = texts.get((chunk_id, revision))
            if text:
                result[raw] = text
        return result

    @property
    def model_http(self) -> httpx.AsyncClient:
        """共享模型网关 HTTP client（惰性创建；dispose 时关闭）。"""
        if self._model_http is None:
            self._model_http = httpx.AsyncClient(transport=self.embedding_transport, timeout=60.0)
        return self._model_http

    async def dispose(self) -> None:
        if self._model_http is not None:
            await self._model_http.aclose()
            self._model_http = None
        for index in (self.dense_index, self.lexical_index):
            close = getattr(index, "close", None)
            if callable(close):
                close()
        await self.engine.dispose()
