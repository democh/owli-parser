"""黄金集检索评估 runner（文档 §9：固定案例、adapter/配置与版本比较检索指标）。

- 标注相关性为不可变 chunk 身份；检索侧经权威校验后的顺序参与评分；
- 指标按 case 计算 → 宏平均聚合 → 按 case.groups 维度分层（缺失组不填充 0）；
- expectations.min_metrics 给定基线阈值：任一未达 → state=failed 并列出违规项；
- 每次运行固化 adapter_ref 与 config_digest（对比可复现性，索引代变化时需重跑）。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from owli_parser.core.clock import Clock
from owli_parser.modules.knowledge.service import GoldenSetInfo, KnowledgeService
from owli_parser.modules.retrieval.evaluation import compute_case_metrics, mean_metrics

# 搜索函数：接收 case（含 query/dataset_ids 及调用方附加上下文），返回权威过滤后的有序 chunk id
SearchFn = Callable[[dict[str, Any]], Awaitable[list[str]]]


@dataclass(frozen=True, slots=True)
class GoldenCaseResult:
    query: str
    groups: dict[str, str]
    ranked_ids: list[str]
    metrics: dict[str, float]


@dataclass(frozen=True, slots=True)
class GoldenRunResult:
    run_id: str
    golden_set_id: uuid.UUID
    passed: bool
    violations: list[str]
    aggregate: dict[str, float]
    by_group: dict[str, dict[str, float]]
    cases: list[GoldenCaseResult] = field(default_factory=list)


class GoldenRunner:
    def __init__(self, knowledge: KnowledgeService, clock: Clock) -> None:
        self._knowledge = knowledge
        self._clock = clock

    async def run(
        self,
        *,
        golden_set: GoldenSetInfo,
        search_fn: SearchFn,
        adapter_ref: str,
        config_digest: str | None = None,
    ) -> GoldenRunResult:
        k_values = [int(k) for k in (golden_set.expectations or {}).get("k_values", [5])]
        min_metrics: dict[str, float] = {
            str(key): float(value)
            for key, value in (golden_set.expectations or {}).get("min_metrics", {}).items()
        }

        case_results: list[GoldenCaseResult] = []
        for case in golden_set.cases or []:
            query = str(case["query"])
            ranked = await search_fn(case)
            relevant = [str(item) for item in case.get("relevant", [])]
            gains_raw = case.get("gains")
            gains = {str(k): float(v) for k, v in gains_raw.items()} if gains_raw else None
            metrics = compute_case_metrics(ranked, relevant=relevant, k_values=k_values, gains=gains)
            case_results.append(
                GoldenCaseResult(
                    query=query,
                    groups={str(k): str(v) for k, v in (case.get("groups") or {}).items()},
                    ranked_ids=list(ranked),
                    metrics=metrics,
                )
            )

        aggregate = mean_metrics([case.metrics for case in case_results])
        by_group: dict[str, dict[str, float]] = {}
        group_keys = sorted({key for case in case_results for key in case.groups})
        for group_key in group_keys:
            members = [case.metrics for case in case_results if case.groups.get(group_key) is not None]
            by_group[group_key] = mean_metrics(members)

        violations = [
            f"{metric} 聚合 {aggregate.get(metric, 0.0):.4f} < 期望 {threshold:.4f}（阈值 {metric}）"
            for metric, threshold in sorted(min_metrics.items())
            if aggregate.get(metric, 0.0) < threshold
        ]
        passed = not violations

        metrics_out: dict[str, Any] = {
            "cases": [
                {
                    "query": case.query,
                    "groups": case.groups,
                    "ranked_ids": case.ranked_ids,
                    "metrics": case.metrics,
                }
                for case in case_results
            ],
            "aggregate": aggregate,
            "by_group": by_group,
            "expectations": {"k_values": k_values, "min_metrics": min_metrics},
            "passed": passed,
            "violations": violations,
        }
        run = await self._knowledge.create_golden_run(
            golden_set_id=golden_set.id,
            adapter_ref=adapter_ref,
            config_digest=config_digest,
            metrics=metrics_out,
            state="passed" if passed else "failed",
        )
        return GoldenRunResult(
            run_id=run.run_id,
            golden_set_id=golden_set.id,
            passed=passed,
            violations=violations,
            aggregate=aggregate,
            by_group=by_group,
            cases=case_results,
        )
