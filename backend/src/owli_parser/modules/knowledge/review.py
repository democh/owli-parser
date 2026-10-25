"""复核目标收集：低置信块 + 缺失单位（纯函数，供解析工作流调用）。

- 未评估置信度（confidence=None）不算低置信——"缺失不是 1.0"，也不误报；
- 阈值比较为严格小于（raw_score == threshold 不登记）；
- 缺失单位去重排序，保证登记顺序稳定。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


class _HasConfidence(Protocol):
    """最小鸭子类型：只依赖 id 与 confidence.raw_score（Block 结构子集）。"""

    @property
    def id(self) -> str: ...

    @property
    def confidence(self) -> object | None: ...


@dataclass(frozen=True, slots=True)
class ReviewTargets:
    low_confidence_block_ids: list[str] = field(default_factory=list)
    missing_units: list[str] = field(default_factory=list)


def collect_review_targets(
    *,
    blocks: Sequence[_HasConfidence],
    missing_units: Sequence[str],
    low_confidence_threshold: float,
) -> ReviewTargets:
    low_conf: list[str] = []
    for block in blocks:
        confidence = block.confidence
        if confidence is None:
            continue
        raw_score = getattr(confidence, "raw_score", None)
        if raw_score is not None and raw_score < low_confidence_threshold:
            low_conf.append(block.id)
    return ReviewTargets(
        low_confidence_block_ids=low_conf,
        missing_units=sorted(set(missing_units)),
    )
