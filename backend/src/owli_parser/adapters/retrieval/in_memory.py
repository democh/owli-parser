"""内存索引实现：dense（余弦相似度）与 lexical（BM25）。

用途：开发环境、单元/契约测试、M3 真实引擎接入前的语义锁定。
特性对齐文档要求：
- 前置过滤（tenant/dataset/acl/generation）在检索阶段应用；
- 向量维度固定：维度不一致拒绝写入（必须新建集合/新 index_generation）；
- 幂等 upsert（同 ID 覆盖）；delete_by_generation 支持"旧代不覆盖新代"的清理。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

from owli_parser.ports.retrieval import (
    DimensionMismatchError,
    IndexDocument,
    IndexFilter,
    IndexHit,
    IndexWriteReceipt,
)

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """确定性分词：ASCII 词（小写）+ 中文字（逐字）。仅用于基线与测试。"""
    return _TOKEN_RE.findall(text.lower())


def hit_payload(doc: IndexDocument) -> dict[str, object]:
    """命中载荷：合并权威校验必需字段（chunk_id/revision/version/dataset）。"""
    return {
        "chunk_id": doc.chunk_id,
        "chunk_revision": doc.chunk_revision,
        "version_id": doc.version_id,
        "dataset_id": doc.dataset_id,
        "tenant_id": doc.tenant_id,
        "acl_ref": doc.acl_ref,
        "index_generation": doc.index_generation,
        **doc.payload,
    }


def _matches_filter(doc: IndexDocument, filter_: IndexFilter) -> bool:
    return (
        doc.tenant_id == filter_.tenant_id
        and (filter_.dataset_ids is None or doc.dataset_id in filter_.dataset_ids)
        and (filter_.acl_refs is None or doc.acl_ref in filter_.acl_refs)
        and (filter_.index_generation is None or doc.index_generation == filter_.index_generation)
    )


class InMemoryDenseIndex:
    """余弦相似度 dense 索引。"""

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = {}
        self._docs: dict[str, IndexDocument] = {}
        self._dimension: int | None = None

    @property
    def dimension(self) -> int | None:
        return self._dimension

    def upsert(self, documents: Sequence[IndexDocument]) -> IndexWriteReceipt:
        upserted = 0
        for doc in documents:
            if doc.vector is None or len(doc.vector) == 0:
                raise DimensionMismatchError(f"dense 写入缺少向量: {doc.id}")
            if self._dimension is None:
                self._dimension = len(doc.vector)
            elif len(doc.vector) != self._dimension:
                # 维度改变必须新建集合或新 index_generation，禁止混写
                raise DimensionMismatchError(
                    f"向量维度不一致（期望 {self._dimension}，收到 {len(doc.vector)}）"
                )
            self._vectors[doc.id] = [float(x) for x in doc.vector]
            self._docs[doc.id] = doc
            upserted += 1
        return IndexWriteReceipt(upserted=upserted, deleted=0)

    def search(self, query_vector: Sequence[float], *, limit: int, filter_: IndexFilter) -> list[IndexHit]:
        if self._dimension is not None and len(query_vector) != self._dimension:
            raise DimensionMismatchError("查询向量维度与索引不一致")
        query = [float(x) for x in query_vector]
        query_norm = math.sqrt(sum(x * x for x in query))
        if query_norm == 0:
            return []
        scored: list[tuple[float, str]] = []
        for doc_id, vector in self._vectors.items():
            doc = self._docs[doc_id]
            if doc is None or not _matches_filter(doc, filter_):
                continue
            dot = sum(a * b for a, b in zip(query, vector, strict=True))
            norm = math.sqrt(sum(x * x for x in vector))
            if norm == 0:
                continue
            scored.append((dot / (query_norm * norm), doc_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            IndexHit(id=doc_id, raw_score=round(score, 10), payload=hit_payload(self._docs[doc_id]))
            for score, doc_id in scored[:limit]
        ]

    def delete_by_generation(self, *, tenant_id: str, version_id: str, before_generation: int) -> int:
        stale = [
            doc_id
            for doc_id, doc in self._docs.items()
            if doc.tenant_id == tenant_id
            and doc.version_id == version_id
            and doc.index_generation < before_generation
        ]
        for doc_id in stale:
            self._docs.pop(doc_id, None)
            self._vectors.pop(doc_id, None)
        return len(stale)


class InMemoryLexicalIndex:
    """BM25 词法索引（k1=1.2, b=0.75，标准公式）。"""

    K1 = 1.2
    B = 0.75

    def __init__(self) -> None:
        self._tokens: dict[str, list[str]] = {}
        self._docs: dict[str, IndexDocument] = {}
        self._df: Counter[str] = Counter()
        self._total_length = 0

    def _remove(self, doc_id: str) -> None:
        old_tokens = self._tokens.pop(doc_id, None)
        if old_tokens is not None:
            for token in set(old_tokens):
                self._df[token] -= 1
                if self._df[token] <= 0:
                    del self._df[token]
            self._total_length -= len(old_tokens)
        self._docs.pop(doc_id, None)

    def upsert(self, documents: Sequence[IndexDocument]) -> IndexWriteReceipt:
        upserted = 0
        for doc in documents:
            if not doc.text:
                raise ValueError(f"lexical 写入缺少文本: {doc.id}")
            self._remove(doc.id)
            tokens = tokenize(doc.text)
            self._tokens[doc.id] = tokens
            self._docs[doc.id] = doc
            for token in set(tokens):
                self._df[token] += 1
            self._total_length += len(tokens)
            upserted += 1
        return IndexWriteReceipt(upserted=upserted, deleted=0)

    def search(self, query: str, *, limit: int, filter_: IndexFilter) -> list[IndexHit]:
        query_tokens = tokenize(query)
        if not query_tokens or not self._docs:
            return []
        total_docs = len(self._docs)
        avg_length = self._total_length / total_docs if total_docs else 0.0
        scored: list[tuple[float, str]] = []
        for doc_id, tokens in self._tokens.items():
            doc = self._docs[doc_id]
            if not _matches_filter(doc, filter_):
                continue
            length = len(tokens)
            frequencies = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                tf = frequencies.get(token, 0)
                if tf == 0:
                    continue
                df = self._df.get(token, 0)
                if df == 0:
                    continue
                idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
                denominator = tf + self.K1 * (1 - self.B + self.B * length / (avg_length or 1))
                score += idf * (tf * (self.K1 + 1)) / denominator
            if score > 0:
                scored.append((score, doc_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            IndexHit(id=doc_id, raw_score=round(score, 10), payload=hit_payload(self._docs[doc_id]))
            for score, doc_id in scored[:limit]
        ]

    def delete_by_generation(self, *, tenant_id: str, version_id: str, before_generation: int) -> int:
        stale = [
            doc_id
            for doc_id, doc in self._docs.items()
            if doc.tenant_id == tenant_id
            and doc.version_id == version_id
            and doc.index_generation < before_generation
        ]
        for doc_id in stale:
            self._remove(doc_id)
        return len(stale)
