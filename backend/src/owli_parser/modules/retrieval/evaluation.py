"""检索质量评估指标（文档 §9：Recall@K / nDCG@K 为 M3 留出集基线）。

- rank 从 1 开始；相关性标注是不变 chunk 身份；
- nDCG 支持分级相关（graded gains），二元标注退化为标准 nDCG；
- 指标在单 case 上计算，聚合与分层由 golden runner 完成（缺失组不填充 0）。
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence


def recall_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    relevant_set = set(relevant)
    if not relevant_set or k <= 0:
        return 0.0
    hit = set(ranked[:k]) & relevant_set
    return len(hit) / len(relevant_set)


def precision_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    """标准 P@k：分母固定为 k（不足 k 条时同样除以 k，不虚增）。"""
    if k <= 0:
        return 0.0
    hit = set(ranked[:k]) & set(relevant)
    return len(hit) / k


def mrr(ranked: Sequence[str], relevant: Collection[str]) -> float:
    """MRR：首个相关项排名的倒数；无相关命中为 0。"""
    relevant_set = set(relevant)
    for rank, doc_id in enumerate(ranked, start=1):
        if doc_id in relevant_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: Sequence[str], gains: dict[str, float], k: int) -> float:
    """nDCG@k：分级增益（缺省 0），IDCG 按全部增益降序截断 k。"""
    if k <= 0 or not gains:
        return 0.0
    dcg = sum(gains.get(doc_id, 0.0) / math.log2(rank + 1) for rank, doc_id in enumerate(ranked[:k], start=1))
    ideal = sorted(gains.values(), reverse=True)[:k]
    idcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(ideal, start=1))
    return dcg / idcg if idcg > 0 else 0.0


def binary_gains(relevant: Collection[str]) -> dict[str, float]:
    return dict.fromkeys(relevant, 1.0)


def compute_case_metrics(
    ranked: Sequence[str],
    *,
    relevant: Collection[str],
    k_values: Sequence[int],
    gains: dict[str, float] | None = None,
) -> dict[str, float]:
    """单 case 指标：recall@k / precision@k / ndcg@k（各 k）+ mrr。"""
    gains = gains if gains is not None else binary_gains(relevant)
    metrics: dict[str, float] = {"mrr": mrr(ranked, relevant)}
    for k in k_values:
        metrics[f"recall@{k}"] = recall_at_k(ranked, relevant, k)
        metrics[f"precision@{k}"] = precision_at_k(ranked, relevant, k)
        metrics[f"ndcg@{k}"] = ndcg_at_k(ranked, gains, k)
    return metrics


def mean_metrics(rows: Sequence[dict[str, float]]) -> dict[str, float]:
    """宏平均（每个 case 等权）；空集合返回空 dict（不伪造 0 值）。"""
    if not rows:
        return {}
    keys = sorted({key for row in rows for key in row})
    return {key: sum(row.get(key, 0.0) for row in rows) / len(rows) for key in keys}
