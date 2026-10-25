"""检索索引端口：Dense / Lexical /（后续）Sparse 通道。

- 所有通道先按 tenant/dataset/ACL 过滤再检索（文档 §9.1：所有召回通道前置过滤）。
- 返回 ID 与原始得分（raw_score 量纲因通道而异，禁止跨通道相加）；
  服务端在返回内容前再次校验 PG 权威版本、revision、enabled、删除与权限。
- point/document ID 必须包含 tenant/version/chunk/revision/generation 维度，
  以支持"过期动作不能覆盖新代"的幂等 upsert。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class IndexDocument:
    """索引写入单元：ID 携带版本与 revision 维度，payload 至少含权限过滤字段。"""

    id: str  # 例如 f"{tenant}:{version}:{chunk}:{revision}:{generation}"
    vector: list[float] | None  # dense 通道使用
    text: str  # lexical/sparse 通道使用
    tenant_id: str
    dataset_id: str
    version_id: str
    chunk_id: str
    chunk_revision: int
    index_generation: int
    acl_ref: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IndexFilter:
    """前置过滤条件：通道在检索阶段即应用，避免无权候选进入后续环节。"""

    tenant_id: str
    dataset_ids: frozenset[str] | None = None  # None 表示不限（内部调用已授权范围）
    acl_refs: frozenset[str] | None = None
    index_generation: int | None = None


@dataclass(frozen=True, slots=True)
class IndexHit:
    """通道命中：id + 原始得分（保持通道自身量纲）。"""

    id: str
    raw_score: float
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IndexWriteReceipt:
    upserted: int
    deleted: int


class DenseIndex(Protocol):
    def upsert(self, documents: Sequence[IndexDocument]) -> IndexWriteReceipt: ...

    def search(
        self, query_vector: Sequence[float], *, limit: int, filter_: IndexFilter
    ) -> list[IndexHit]: ...

    def delete_by_generation(self, *, tenant_id: str, version_id: str, before_generation: int) -> int: ...


class LexicalIndex(Protocol):
    def upsert(self, documents: Sequence[IndexDocument]) -> IndexWriteReceipt: ...

    def search(self, query: str, *, limit: int, filter_: IndexFilter) -> list[IndexHit]: ...

    def delete_by_generation(self, *, tenant_id: str, version_id: str, before_generation: int) -> int: ...


class RetrievalIndexError(Exception):
    """索引层错误（不可用时检索应返回索引暂不可查询，而不是旧结果凑数）。"""


class DimensionMismatchError(RetrievalIndexError):
    """向量维度不一致：维度/模型变更必须新建集合或新 index_generation，禁止混写。"""
