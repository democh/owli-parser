"""检索领域逻辑：RRF 融合（严格按文档 §9.1 语义）。

RRF(d) = Σ w_i / (rrf_k + rank_i(d))
- rank 从 1 开始；未命中通道贡献为 0（不上榜即不贡献）。
- 不同量纲的 raw_score 不相加，只保留供展示与排序诊断；
  融合仅使用各通道的排名与配置权重。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RankedHit:
    """通道内命中：rank 由通道返回顺序决定（从 1 开始）。"""

    id: str
    raw_score: float
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FusedHit:
    id: str
    rrf_score: float
    channel_ranks: dict[str, int] = field(default_factory=dict)
    channel_raw_scores: dict[str, float] = field(default_factory=dict)
    channel_contributions: dict[str, float] = field(default_factory=dict)
    payload: dict[str, object] = field(default_factory=dict)


def rrf_fuse(
    channel_results: dict[str, list[RankedHit]],
    *,
    rrf_k: int,
    weights: dict[str, float],
    candidate_limit: int,
) -> list[FusedHit]:
    """融合各通道结果，返回按 RRF 分数降序的候选（含逐通道贡献）。

    参数约束（协议不变量）：
    - rrf_k ≥ 1；rank 从 1 开始；
    - 未配置权重的通道按 1.0 计；权重 ≤ 0 表示该通道不参与融合；
    - candidate_limit ≥ 1，超额候选被裁剪（融合后再截断）。
    """
    if rrf_k < 1:
        raise ValueError("rrf_k 必须 ≥ 1")
    if candidate_limit < 1:
        raise ValueError("candidate_limit 必须 ≥ 1")

    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    raws: dict[str, dict[str, float]] = {}
    contributions: dict[str, dict[str, float]] = {}
    payloads: dict[str, dict[str, object]] = {}

    for channel, hits in channel_results.items():
        weight = weights.get(channel, 1.0)
        if weight <= 0:
            continue
        for rank, hit in enumerate(hits, start=1):
            contribution = weight / (rrf_k + rank)
            scores[hit.id] = scores.get(hit.id, 0.0) + contribution
            ranks.setdefault(hit.id, {})[channel] = rank
            raws.setdefault(hit.id, {})[channel] = hit.raw_score
            contributions.setdefault(hit.id, {})[channel] = contribution
            payloads.setdefault(hit.id, hit.payload)

    # 稳定排序：分数降序，分数相同按 id 升序（确定性输出，便于回归对比）
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    fused: list[FusedHit] = []
    for doc_id, score in ordered[:candidate_limit]:
        fused.append(
            FusedHit(
                id=doc_id,
                rrf_score=round(score, 10),
                channel_ranks=dict(ranks.get(doc_id, {})),
                channel_raw_scores=dict(raws.get(doc_id, {})),
                channel_contributions=dict(contributions.get(doc_id, {})),
                payload=payloads.get(doc_id, {}),
            )
        )
    return fused


def is_low_relevance(
    channel_results: dict[str, list[RankedHit]],
    *,
    threshold: float,
) -> bool:
    """低相关判定：所有启用通道的最高原始分都低于阈值，或全部空召回。

    阈值来自 retrieval 配置并由验证集标定；原始分仅在通道内可比。
    """
    best: float | None = None
    for hits in channel_results.values():
        if hits:
            top = hits[0].raw_score
            best = top if best is None else max(best, top)
    if best is None:
        return True
    return best < threshold
