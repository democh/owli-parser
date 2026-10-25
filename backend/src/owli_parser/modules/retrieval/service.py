"""RetrievalService：混合检索管线（dense + BM25 → RRF → 权威校验 → TopK）。

流程（文档 §9.1）：
1. 通道前置过滤 tenant/dataset/ACL（IndexFilter），无权候选不进入后续环节；
2. 每通道 per_channel_limit 召回，保留 raw_score/rank（量纲不混合）；
3. RRF 融合（权重与 rrf_k 来自配置），候选裁剪至 candidate_limit；
4. **权威校验（必填回调）**：调用方按 PG 复核 chunk revision/enabled/版本/权限，
   剔除陈旧或无权候选；
5. 截取 top_k；低相关时返回警告而非伪造相关性结论。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from owli_parser.bootstrap.settings import RetrievalSection
from owli_parser.core.errors import CapabilityNotReadyError
from owli_parser.infrastructure.telemetry import METRICS, get_logger
from owli_parser.modules.retrieval.domain import FusedHit, RankedHit, is_low_relevance, rrf_fuse
from owli_parser.ports.retrieval import (
    DenseIndex,
    IndexFilter,
    LexicalIndex,
    RetrievalIndexError,
)


@dataclass(frozen=True, slots=True)
class RetrievalItem:
    """最终候选：保留逐通道原始分与 RRF 贡献供召回测试台展示。"""

    id: str
    rrf_score: float
    channel_ranks: dict[str, int] = field(default_factory=dict)
    channel_raw_scores: dict[str, float] = field(default_factory=dict)
    channel_contributions: dict[str, float] = field(default_factory=dict)
    rerank_score: float | None = None  # M3 rerank 接入后填充
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    items: list[RetrievalItem]
    channels_used: list[str]
    applied_filters: dict[str, object]
    low_relevance: bool
    warnings: list[str] = field(default_factory=list)
    applied_params: dict[str, object] = field(default_factory=dict)
    rerank_applied: bool = False


# 权威校验回调：接收融合候选，返回允许保留的 id 集合（PG 复核 revision/enabled/权限）
AuthoritativeFilter = Callable[[Sequence[FusedHit]], Awaitable[set[str]]]

# rerank 提供者：查询 + 文档列表 → 逐文档分数（顺序与输入一致）
RerankProvider = Callable[[str, Sequence[str]], Awaitable[Sequence[float]]]
RerankProviderResolver = Callable[[str], Awaitable[RerankProvider | None]]
# 正文加载器：id 列表 → id→文本（PG 权威回查；缺失键表示正文不可得）
TextLoader = Callable[[str, Sequence[str]], Awaitable[dict[str, str]]]

_logger = get_logger("owli.retrieval")


def _query_fingerprint(query: str) -> str:
    """查询指纹：sha256 前 12 位。审计日志不落原文，可凭指纹与测试台/工单关联。"""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]


class RetrievalService:
    def __init__(
        self,
        settings: RetrievalSection,
        *,
        dense_index: DenseIndex | None,
        lexical_index: LexicalIndex | None,
        rerank_provider_resolver: RerankProviderResolver | None = None,
        text_loader: TextLoader | None = None,
    ) -> None:
        self._settings = settings
        self._dense = dense_index
        self._lexical = lexical_index
        self._rerank_resolver = rerank_provider_resolver
        self._text_loader = text_loader

    async def _rerank(
        self,
        verified: list[FusedHit],
        *,
        tenant_id: str,
        query: str,
        warnings: list[str],
    ) -> tuple[list[FusedHit], dict[str, float], bool]:
        """rerank 重排（权威校验之后）；返回 (重排后候选, id→分数, 是否生效)。

        - rerank_model_ref 非空即启用；无 rerank 用途绑定 → 明确失败（不静默降级）；
        - 正文缺失的候选不参与评分，保持原相对顺序置后并记 warning（不伪造分数）；
        - 分数缺失（服务未返回该序号）→ 视为响应不完整（客户端契约已拒）。
        """
        if not self._settings.rerank_model_ref or not verified:
            return verified, {}, False
        if self._rerank_resolver is None:
            raise CapabilityNotReadyError("rerank 已启用（rerank_model_ref）但未装配解析器")
        provider = await self._rerank_resolver(tenant_id)
        if provider is None:
            raise CapabilityNotReadyError("rerank 已启用（rerank_model_ref）但缺少 rerank 用途绑定")
        texts: dict[str, str] = {}
        if self._text_loader is not None:
            texts = await self._text_loader(tenant_id, [hit.id for hit in verified])
        present = [(index, texts.get(hit.id, "")) for index, hit in enumerate(verified)]
        scorable = [(index, text) for index, text in present if text]
        scores_map: dict[int, float] = {}
        if scorable:
            scores = await provider(query, [text for _, text in scorable])
            scores_map = {index: float(score) for (index, _), score in zip(scorable, scores, strict=True)}
        missing = len(verified) - len(scorable)
        if missing:
            warnings.append(f"{missing} 个候选缺少正文，未参与 rerank（保持原相对顺序置后）")
        # 稳定排序：有分按分降序；无分保持原序号顺序置于其后
        order = sorted(
            range(len(verified)),
            key=lambda index: (0, -scores_map[index]) if index in scores_map else (1, index),
        )
        reranked = [verified[index] for index in order]
        score_by_id = {verified[index].id: score for index, score in scores_map.items()}
        return reranked, score_by_id, bool(scores_map)

    async def search(
        self,
        *,
        tenant_id: str,
        query: str,
        query_vector: Sequence[float] | None,
        dataset_ids: Sequence[str] | None,
        acl_refs: Sequence[str] | None,
        authoritative_filter: AuthoritativeFilter,
        top_k: int | None = None,
        per_channel_limit: int | None = None,
        rrf_k: int | None = None,
        channel_weights: dict[str, float] | None = None,
        min_relevance_threshold: float | None = None,
    ) -> RetrievalResult:
        """召回测试台可覆盖 rrf_k/权重/阈值/每通道上限（TopK/阈值/权重可调）；
        缺省一律取配置，candidate_limit 不可覆盖（防止绕过候选上限）。"""
        if not self._settings.enabled:
            raise CapabilityNotReadyError("检索通道未启用（retrieval.enabled=false）")
        started = time.perf_counter()
        effective_per_channel = per_channel_limit or self._settings.per_channel_limit
        effective_top_k = min(top_k or self._settings.top_k, self._settings.candidate_limit)
        effective_rrf_k = rrf_k or self._settings.rrf_k
        effective_weights = (
            dict(channel_weights) if channel_weights is not None else dict(self._settings.channel_weights)
        )
        effective_threshold = (
            min_relevance_threshold
            if min_relevance_threshold is not None
            else self._settings.min_relevance_threshold
        )
        index_filter = IndexFilter(
            tenant_id=tenant_id,
            dataset_ids=frozenset(dataset_ids) if dataset_ids else None,
            acl_refs=frozenset(acl_refs) if acl_refs else None,
        )
        channel_results: dict[str, list[RankedHit]] = {}
        warnings: list[str] = []

        # 通道召回（同步适配器经线程池执行，避免阻塞事件循环）
        if self._dense is not None and query_vector is not None:
            try:
                dense_hits = await asyncio.to_thread(
                    self._dense.search,
                    list(query_vector),
                    limit=effective_per_channel,
                    filter_=index_filter,
                )
                channel_results["dense"] = [
                    RankedHit(id=hit.id, raw_score=hit.raw_score, payload=hit.payload) for hit in dense_hits
                ]
            except RetrievalIndexError:
                warnings.append("dense 通道暂不可用，已按可用通道降级")
        if self._lexical is not None:
            try:
                lexical_hits = await asyncio.to_thread(
                    self._lexical.search,
                    query,
                    limit=effective_per_channel,
                    filter_=index_filter,
                )
                channel_results["lexical"] = [
                    RankedHit(id=hit.id, raw_score=hit.raw_score, payload=hit.payload) for hit in lexical_hits
                ]
            except RetrievalIndexError:
                warnings.append("lexical 通道暂不可用，已按可用通道降级")

        low = is_low_relevance(channel_results, threshold=effective_threshold)
        if low:
            warnings.append("所有召回低于相关性阈值，结果可能不相关")

        fused = rrf_fuse(
            channel_results,
            rrf_k=effective_rrf_k,
            weights=effective_weights,
            candidate_limit=self._settings.candidate_limit,
        )
        # 权威校验：无权/陈旧候选不得进入结果、reranker 或日志
        allowed = await authoritative_filter(fused)
        verified = [hit for hit in fused if hit.id in allowed]
        dropped = len(fused) - len(verified)
        if dropped:
            warnings.append(f"{dropped} 个候选未通过权威校验（revision/权限/版本）")

        # rerank：仅作用于权威之后的候选（不得进入无权威校验的候选）
        verified, rerank_scores, rerank_applied = await self._rerank(
            verified, tenant_id=str(tenant_id), query=query, warnings=warnings
        )

        items = [
            RetrievalItem(
                id=hit.id,
                rrf_score=hit.rrf_score,
                channel_ranks=hit.channel_ranks,
                channel_raw_scores=hit.channel_raw_scores,
                channel_contributions=hit.channel_contributions,
                rerank_score=rerank_scores.get(hit.id),
                payload=hit.payload,
            )
            for hit in verified[:effective_top_k]
        ]
        applied_filters: dict[str, object] = {
            "tenant_id": tenant_id,
            "dataset_ids": sorted(dataset_ids) if dataset_ids else None,
            "acl_refs": sorted(acl_refs) if acl_refs else None,
        }
        applied_params: dict[str, object] = {
            "top_k": effective_top_k,
            "per_channel_limit": effective_per_channel,
            "candidate_limit": self._settings.candidate_limit,
            "rrf_k": effective_rrf_k,
            "channel_weights": effective_weights,
            "min_relevance_threshold": effective_threshold,
            "rerank_applied": rerank_applied,
        }
        # 审计与观测：结构化日志不落查询原文（仅指纹/长度），指标标签有界；
        # 高频读操作不写 audit_logs 表，避免污染操作审计与写放大。
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        channels_used = sorted(channel_results.keys())
        _logger.info(
            "retrieval_query",
            extra={
                "tenant_id": tenant_id,
                "query_fingerprint": _query_fingerprint(query),
                "query_length": len(query),
                "channels": channels_used,
                "channel_hits": {ch: len(hits) for ch, hits in channel_results.items()},
                "fused_candidates": len(fused),
                "dropped_by_authority": dropped,
                "returned": len(items),
                "low_relevance": low,
                "duration_ms": duration_ms,
                "rrf_k": effective_rrf_k,
                "per_channel_limit": effective_per_channel,
                "rerank_applied": rerank_applied,
            },
        )
        METRICS.inc("owli_retrieval_queries_total", {"low_relevance": "true" if low else "false"})
        for channel in channels_used:
            METRICS.inc("owli_retrieval_channel_queries_total", {"channel": channel})
        if dropped:
            METRICS.inc("owli_retrieval_dropped_total", amount=float(dropped))
        return RetrievalResult(
            items=items,
            channels_used=channels_used,
            applied_filters=applied_filters,
            rerank_applied=rerank_applied,
            low_relevance=low,
            warnings=warnings,
            applied_params=applied_params,
        )
