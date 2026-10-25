"""索引发布：把 PG 权威 chunk 快照写入已启用通道并驱动 manifest 确认。

- 发布链路 INDEXING 阶段调用 `publish_version`：读 PG（唯一事实源）→
  旧代清理（delete_by_generation）→ 逐通道 upsert → acknowledge_channel；
- 全部必需通道确认后 manifest state=ready，任务方可声明 retrieval_ready=true；
- 写入集合与 manifest 冻结的 expected_ids 按 digest 校验：并发编辑导致
  revision 漂移时确认被拒（任务失败，安全方向优先）；
- dense 通道需要 embedding 用途绑定（M3 模型配置）；未绑定时不进入必需集合，
  配置显式要求 dense 而未绑定时直接报 capability_not_ready，不静默降级；
- 内存索引仅同进程可见（开发/测试）；生产由 Qdrant/OpenSearch adapter 承接，
  同步 HTTP 调用一律经线程池，避免阻塞 API/worker 事件循环。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select

from owli_parser.bootstrap.settings import RetrievalSection
from owli_parser.core.errors import CapabilityNotReadyError, ConflictError, NotFoundError
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.knowledge import chunking
from owli_parser.modules.knowledge.models import ChunkRevisionRow, ChunkRow
from owli_parser.modules.knowledge.service import KnowledgeService, ManifestInfo
from owli_parser.ports.outbox import OutboxRecorder
from owli_parser.ports.retrieval import DenseIndex, IndexDocument, LexicalIndex

# embedding 提供者：批量文本 → 向量（按租户从模型用途绑定解析；None 时 dense 不可用）
EmbeddingProvider = Callable[[Sequence[str]], Awaitable[Sequence[Sequence[float]]]]
EmbeddingProviderResolver = Callable[[uuid.UUID], Awaitable[EmbeddingProvider | None]]

_KNOWN_CHANNELS = ("dense", "lexical")

# 单条索引行动的最大消费尝试次数（失败由 outbox 重派驱动）
_MAX_ACTION_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    manifest_id: str
    state: str
    channels_written: list[str]
    indexed_count: int


class IndexPublicationService:
    def __init__(
        self,
        uow: UnitOfWork,
        knowledge: KnowledgeService,
        settings: RetrievalSection,
        *,
        dense_index: DenseIndex | None,
        lexical_index: LexicalIndex | None,
        embedding_provider_resolver: EmbeddingProviderResolver | None = None,
        outbox: OutboxRecorder | None = None,
    ) -> None:
        self._uow = uow
        self._knowledge = knowledge
        self._settings = settings
        self._dense = dense_index
        self._lexical = lexical_index
        self._embedding_provider_resolver = embedding_provider_resolver
        self._outbox = outbox

    async def _resolve_provider(self, tenant_id: uuid.UUID) -> EmbeddingProvider | None:
        if self._embedding_provider_resolver is None:
            return None
        return await self._embedding_provider_resolver(tenant_id)

    # ------------------------------------------------------------------
    # 必需通道解析
    # ------------------------------------------------------------------

    async def required_channels(self, tenant_id: uuid.UUID) -> list[str]:
        """配置通道 ∩ 写入器可用通道；不可用即显式失败（不静默降级）。"""
        wanted = list(self._settings.channels)
        if "dense" in wanted and await self._resolve_provider(tenant_id) is None:
            raise CapabilityNotReadyError(
                "dense 通道需要 embedding 用途绑定（模型能力档案 + 连接），当前未配置"
            )
        if "dense" in wanted and self._dense is None:
            raise CapabilityNotReadyError("dense 索引写入器未装配")
        if "lexical" in wanted and self._lexical is None:
            raise CapabilityNotReadyError("lexical 索引写入器未装配")
        return wanted

    async def delete_version_indexes(self, *, tenant_id: uuid.UUID, version_id: uuid.UUID) -> int:
        """GC：删除该版本在已装配通道中的全部代（返回删除条数）。

        仅用于软删保留期后的受控回收；即使残留旧条目，检索侧权威校验仍会拒绝。
        """
        target_generation = 2**31 - 1
        deleted = 0
        for index in (self._dense, self._lexical):
            if index is None:
                continue
            deleted += await asyncio.to_thread(
                index.delete_by_generation,
                tenant_id=str(tenant_id),
                version_id=str(version_id),
                before_generation=target_generation,
            )
        return deleted

    # ------------------------------------------------------------------
    # 发布写入
    # ------------------------------------------------------------------

    async def publish_version(
        self,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        generation: int,
        manifest: ManifestInfo,
    ) -> PublishOutcome:
        channels = list(manifest.required_channels or [])
        async with self._uow.read_only() as session:
            # 正文在不可变 chunk_revisions；以 current_revision 联表取权威文本
            rows = (
                await session.execute(
                    select(
                        ChunkRow.id,
                        ChunkRow.current_revision,
                        ChunkRow.dataset_id,
                        ChunkRow.doc_id,
                        ChunkRow.kind,
                        ChunkRow.acl_ref,
                        ChunkRevisionRow.text,
                    )
                    .join(
                        ChunkRevisionRow,
                        (ChunkRevisionRow.chunk_id == ChunkRow.id)
                        & (ChunkRevisionRow.revision == ChunkRow.current_revision),
                    )
                    .where(ChunkRow.version_id == version_id, ChunkRow.enabled.is_(True))
                )
            ).all()
        expected = sorted(f"{row[0]}:{row[1]}" for row in rows)
        digest = chunking.canonical_ids_digest(expected)

        documents = [
            IndexDocument(
                id=f"{tenant_id}:{version_id}:{row[0]}:{row[1]}:{generation}",
                vector=None,
                text=row[6],
                tenant_id=str(tenant_id),
                dataset_id=str(row[2]),
                version_id=str(version_id),
                chunk_id=str(row[0]),
                chunk_revision=row[1],
                index_generation=generation,
                acl_ref=row[5],
                payload={"document_id": str(row[3]), "kind": row[4]},
            )
            for row in rows
        ]
        vectors: list[Sequence[float]] | None = None
        if "dense" in channels and documents:
            provider = await self._resolve_provider(tenant_id)
            if provider is None:  # required_channels 已保证，防御性兜底
                raise CapabilityNotReadyError("dense 通道缺少 embedding 提供者")
            vectors = list(await provider([doc.text for doc in documents]))

        written: list[str] = []
        for channel in channels:
            index = self._dense if channel == "dense" else self._lexical
            if index is None:  # required_channels 已校验；运行时防御（assert 会被 -O 剥离）
                raise CapabilityNotReadyError(f"{channel} 索引写入器未装配")
            # 旧代清理：过期写入不得覆盖新代（端口契约）
            await asyncio.to_thread(
                index.delete_by_generation,
                tenant_id=str(tenant_id),
                version_id=str(version_id),
                before_generation=generation,
            )
            if channel == "dense":
                if vectors is None:  # 同上：防御性检查
                    raise CapabilityNotReadyError("dense 通道缺少向量")
                await asyncio.to_thread(
                    index.upsert,
                    [replace(doc, vector=list(vec)) for doc, vec in zip(documents, vectors, strict=True)],
                )
            else:
                await asyncio.to_thread(index.upsert, documents)
            # 清单确认：digest 与冻结 expected 集一致才允许（并发 revision 漂移 → 拒绝）
            await self._knowledge.acknowledge_channel(
                manifest.id,
                channel=channel,
                acknowledged_count=len(expected),
                ids_digest=digest,
            )
            written.append(channel)

        info = await self._knowledge.latest_manifest(version_id)
        return PublishOutcome(
            manifest_id=str(manifest.id),
            state=info.state if info else "unknown",
            channels_written=written,
            indexed_count=len(documents),
        )

    # ------------------------------------------------------------------
    # 单块重索引（reembed / 编辑后手动重建）
    # ------------------------------------------------------------------

    async def reindex_chunk(self, *, tenant_id: uuid.UUID, chunk_id: uuid.UUID) -> dict[str, Any]:
        """按 PG 当前 revision 重建单块索引；旧 revision 条目由权威校验自然失效。

        注意：这里不做 delete（避免误删新代）；检索侧以 PG 复核 revision，
        旧条目不会进入结果，清理由下一代发布或运维任务完成。
        """
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(
                        ChunkRow.id,
                        ChunkRow.current_revision,
                        ChunkRow.dataset_id,
                        ChunkRow.doc_id,
                        ChunkRow.kind,
                        ChunkRow.acl_ref,
                        ChunkRevisionRow.text,
                        ChunkRow.version_id,
                        ChunkRow.enabled,
                    )
                    .join(
                        ChunkRevisionRow,
                        (ChunkRevisionRow.chunk_id == ChunkRow.id)
                        & (ChunkRevisionRow.revision == ChunkRow.current_revision),
                    )
                    .where(ChunkRow.id == chunk_id, ChunkRow.tenant_id == tenant_id)
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError("分块不存在")
        if not row[8]:
            raise ConflictError("分块已停用，禁止重建索引")
        version_id = row[7]
        manifest = await self._knowledge.latest_manifest(version_id)
        generation = manifest.index_generation if manifest else 1
        channels = await self.required_channels(tenant_id)
        document = IndexDocument(
            id=f"{tenant_id}:{version_id}:{row[0]}:{row[1]}:{generation}",
            vector=None,
            text=row[6],
            tenant_id=str(tenant_id),
            dataset_id=str(row[2]),
            version_id=str(version_id),
            chunk_id=str(row[0]),
            chunk_revision=row[1],
            index_generation=generation,
            acl_ref=row[5],
            payload={"document_id": str(row[3]), "kind": row[4]},
        )
        for channel in channels:
            if self._dense is None or self._lexical is None:  # required_channels 已校验的运行时防御
                raise CapabilityNotReadyError(f"{channel} 索引写入器未装配")
            if channel == "dense":
                provider = await self._resolve_provider(tenant_id)
                if provider is None:
                    raise CapabilityNotReadyError("dense 通道缺少 embedding 提供者")
                vector = list((await provider([document.text]))[0])
                await asyncio.to_thread(self._dense.upsert, [replace(document, vector=vector)])
            else:
                await asyncio.to_thread(self._lexical.upsert, [document])
        # 重建成功后：同 chunk 的 pending 行动全部完成（自动/手动路径共用）
        resolved = await self._knowledge.resolve_index_actions_for_chunk(
            tenant_id=tenant_id, chunk_id=chunk_id
        )
        return {
            "chunk_id": str(row[0]),
            "revision": row[1],
            "index_generation": generation,
            "channels": channels,
            "resolved_actions": resolved,
        }

    # ------------------------------------------------------------------
    # 行动消费（worker 入口）
    # ------------------------------------------------------------------

    async def apply_action(self, action_id: uuid.UUID) -> str:
        """消费一条编辑派生的重索引行动（幂等：仅 pending 状态执行）。

        返回 done / skipped / pending（重派）/ failed。失败未超限时重新登记
        outbox 事件形成重试环；attempts 超限后置 failed（可手动 reembed）。
        """
        action = await self._knowledge.get_index_action(action_id)
        if action is None or action.state != "pending":
            return "skipped"
        try:
            await self.reindex_chunk(tenant_id=action.tenant_id, chunk_id=action.chunk_id)
            return "done"
        except NotFoundError:
            # 分块已删除：无内容可索引，行动直接完成
            await self._knowledge.complete_index_action(action_id)
            return "done"
        except ConflictError:
            # 分块已停用：权威层已生效，无需重建索引
            await self._knowledge.complete_index_action(action_id)
            return "skipped"
        except Exception as exc:
            info = await self._knowledge.fail_index_action(
                action_id, error=exc.__class__.__name__, max_attempts=_MAX_ACTION_ATTEMPTS
            )
            if info.state == "pending" and self._outbox is not None:
                await self._outbox.record(
                    aggregate_type="index_action",
                    aggregate_id=str(action_id),
                    event_type="index.apply",
                    payload={"action_id": str(action_id)},
                )
            return info.state
