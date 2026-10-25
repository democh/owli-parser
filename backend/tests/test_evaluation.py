"""检索评估指标纯函数测试：数学正确性与边界（空集不伪造 0 值仅限聚合语义）。"""

from __future__ import annotations

import pytest

from owli_parser.modules.retrieval.evaluation import (
    binary_gains,
    compute_case_metrics,
    mean_metrics,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


def test_recall_at_k() -> None:
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, {"b"}, 1) == 0.0
    assert recall_at_k(ranked, {"b"}, 2) == 1.0
    assert recall_at_k(ranked, {"b", "d"}, 3) == pytest.approx(0.5)
    assert recall_at_k(ranked, {"b", "d"}, 4) == 1.0
    assert recall_at_k(ranked, set(), 3) == 0.0
    assert recall_at_k(ranked, {"b"}, 0) == 0.0


def test_precision_at_k_fixed_denominator() -> None:
    ranked = ["a", "b"]
    # 标准 P@k：分母固定为 k，不足 k 条不虚增
    assert precision_at_k(ranked, {"a"}, 5) == pytest.approx(0.2)
    assert precision_at_k(ranked, {"a", "b"}, 2) == 1.0
    assert precision_at_k([], {"a"}, 3) == 0.0


def test_mrr() -> None:
    assert mrr(["a", "b", "c"], {"b"}) == pytest.approx(0.5)
    assert mrr(["a", "b"], {"a"}) == 1.0
    assert mrr(["a", "b"], {"z"}) == 0.0
    assert mrr([], {"a"}) == 0.0


def test_ndcg_binary_and_graded() -> None:
    relevant = {"a", "b"}
    gains = binary_gains(relevant)
    # 完美排序 → 1.0
    assert ndcg_at_k(["a", "b"], gains, 2) == pytest.approx(1.0)
    # 二元增益下相关项间换序不改变 nDCG；关键是不相关项前置压缩了增益
    assert ndcg_at_k(["b", "a"], gains, 2) == pytest.approx(1.0)
    expected = (1 / 1.5849625007211562 + 0.5) / (1 + 1 / 1.5849625007211562)
    assert ndcg_at_k(["x", "a", "b"], gains, 3) == pytest.approx(expected)
    # 分级增益：高相关被压后 → 低于 1
    graded = {"a": 3.0, "b": 1.0}
    assert ndcg_at_k(["a", "b"], graded, 2) == pytest.approx(1.0)
    assert ndcg_at_k(["b", "a"], graded, 2) < 1.0
    assert ndcg_at_k(["a"], {}, 2) == 0.0


def test_compute_case_metrics_keys() -> None:
    metrics = compute_case_metrics(["a", "b"], relevant={"a"}, k_values=[1, 3])
    assert set(metrics) == {"mrr", "recall@1", "precision@1", "ndcg@1", "recall@3", "precision@3", "ndcg@3"}
    assert metrics["mrr"] == 1.0
    assert metrics["recall@3"] == 1.0
    assert metrics["precision@3"] == pytest.approx(1 / 3)


def test_mean_metrics() -> None:
    assert mean_metrics([]) == {}
    averaged = mean_metrics([{"mrr": 1.0, "recall@3": 1.0}, {"mrr": 0.0, "recall@3": 0.5}])
    assert averaged == {"mrr": 0.5, "recall@3": 0.75}
