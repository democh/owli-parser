"""Qdrant / OpenSearch 适配器契约测试（MockTransport，不触网）。

真实引擎共享端口语义：前置过滤、幂等写入、维度保护、旧代清理。
这些测试锁定适配器发出的请求语义；真实集群验收仍需独立执行。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from owli_parser.adapters.retrieval.opensearch import OpenSearchLexicalIndex
from owli_parser.adapters.retrieval.qdrant import QdrantDenseIndex
from owli_parser.bootstrap.container import Container
from owli_parser.bootstrap.settings import load_boot_settings
from owli_parser.core.clock import FixedClock
from owli_parser.ports.retrieval import (
    DimensionMismatchError,
    IndexDocument,
    IndexFilter,
    RetrievalIndexError,
)


def _doc(
    doc_id: str,
    *,
    vector: list[float] | None = None,
    text: str = "",
    tenant: str = "t1",
    dataset: str = "ds1",
    generation: int = 1,
    acl: str = "dataset:ds1",
) -> IndexDocument:
    return IndexDocument(
        id=doc_id,
        vector=vector,
        text=text,
        tenant_id=tenant,
        dataset_id=dataset,
        version_id="v1",
        chunk_id=doc_id,
        chunk_revision=1,
        index_generation=generation,
        acl_ref=acl,
    )


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content) if request.content else {}


def _matches(source: dict[str, Any], clauses: Sequence[dict[str, Any]]) -> bool:
    for clause in clauses:
        if "term" in clause:
            field, expected = next(iter(clause["term"].items()))
            if source.get(field) != expected:
                return False
        elif "terms" in clause:
            field, expected = next(iter(clause["terms"].items()))
            if source.get(field) not in expected:
                return False
        elif "range" in clause:
            range_spec = clause["range"]
            if "key" in clause:
                field = clause["key"]
                comparator = range_spec
            else:
                field, comparator = next(iter(range_spec.items()))
            value = source.get(field)
            if not isinstance(value, (int, float)) or value >= comparator["lt"]:
                return False
        elif "match" in clause:
            field = clause["key"]
            match = clause["match"]
            if "any" in match:
                if source.get(field) not in match["any"]:
                    return False
            elif source.get(field) != match.get("value"):
                return False
    return True


class FakeQdrant:
    def __init__(self) -> None:
        self.dimension: int | None = None
        self.points: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        base = "/collections/owli_chunks"
        if path == "/healthz":
            return httpx.Response(200, text="healthz check passed")
        if path == base and request.method == "GET":
            if self.dimension is None:
                return httpx.Response(404, json={"status": {"error": "not found"}})
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {"params": {"vectors": {"size": self.dimension, "distance": "Cosine"}}}
                    }
                },
            )
        if path == base and request.method == "PUT":
            self.dimension = int(_body(request)["vectors"]["size"])
            return httpx.Response(200, json={"result": True})
        if path == f"{base}/index":
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if path == f"{base}/points" and request.method == "PUT":
            for point in _body(request)["points"]:
                self.points[point["id"]] = point
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if path == f"{base}/points/query":
            body = _body(request)
            query = [float(value) for value in body["query"]]
            rows = []
            for point in self.points.values():
                payload = point["payload"]
                if not _matches(payload, body.get("filter", {}).get("must", [])):
                    continue
                score = sum(a * b for a, b in zip(query, point["vector"], strict=True))
                rows.append({"id": point["id"], "score": score, "payload": payload})
            rows.sort(key=lambda row: (-row["score"], row["id"]))
            return httpx.Response(200, json={"result": {"points": rows[: body["limit"]]}})
        if path == f"{base}/points/count":
            clauses = _body(request).get("filter", {}).get("must", [])
            count = sum(1 for point in self.points.values() if _matches(point["payload"], clauses))
            return httpx.Response(200, json={"result": {"count": count}})
        if path == f"{base}/points/delete":
            clauses = _body(request).get("filter", {}).get("must", [])
            stale = [
                point_id for point_id, point in self.points.items() if _matches(point["payload"], clauses)
            ]
            for point_id in stale:
                self.points.pop(point_id, None)
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(500, json={"error": f"unexpected {request.method} {path}"})


def _qdrant_index(fake: FakeQdrant) -> QdrantDenseIndex:
    client = httpx.Client(
        base_url="http://qdrant.test",
        transport=httpx.MockTransport(fake),
    )
    return QdrantDenseIndex(url="http://qdrant.test", collection="owli_chunks", client=client)


def test_qdrant_dense_contract() -> None:
    fake = FakeQdrant()
    index = _qdrant_index(fake)
    try:
        assert index.check_health()
        index.upsert(
            [
                _doc("c1", vector=[1.0, 0.0]),
                _doc("c2", vector=[0.9, 0.1], dataset="ds2", acl="dataset:ds2"),
                _doc("c3", vector=[0.0, 1.0]),
            ]
        )
        hits = index.search([1.0, 0.0], limit=3, filter_=IndexFilter(tenant_id="t1"))
        assert [hit.id for hit in hits] == ["c1", "c2", "c3"]
        assert hits[0].raw_score == pytest.approx(1.0)
        assert hits[0].payload["chunk_id"] == "c1"

        filtered = index.search(
            [1.0, 0.0],
            limit=3,
            filter_=IndexFilter(tenant_id="t1", dataset_ids=frozenset({"ds2"})),
        )
        assert [hit.id for hit in filtered] == ["c2"]
        assert index.search([1.0, 0.0], limit=3, filter_=IndexFilter(tenant_id="other")) == []

        with pytest.raises(DimensionMismatchError):
            index.upsert([_doc("c4", vector=[1.0, 0.0, 0.0])])
        with pytest.raises(DimensionMismatchError):
            index.search([1.0], limit=1, filter_=IndexFilter(tenant_id="t1"))

        index.upsert([_doc("c3", vector=[0.0, 1.0])])
        assert index.search([0.0, 1.0], limit=1, filter_=IndexFilter(tenant_id="t1"))[0].id == "c3"

        index.upsert([_doc("c5", vector=[1.0, 0.0], generation=2)])
        assert index.delete_by_generation(tenant_id="t1", version_id="v1", before_generation=2) == 3
        remaining = index.search([1.0, 0.0], limit=10, filter_=IndexFilter(tenant_id="t1"))
        assert [hit.id for hit in remaining] == ["c5"]
    finally:
        index.close()


def test_qdrant_errors_are_mapped() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    index = QdrantDenseIndex(
        url="http://qdrant.test",
        collection="owli_chunks",
        client=httpx.Client(base_url="http://qdrant.test", transport=httpx.MockTransport(fail)),
    )
    try:
        with pytest.raises(RetrievalIndexError, match="Qdrant HTTP 503"):
            index.upsert([_doc("c1", vector=[1.0, 0.0])])
        assert index.check_health() is False
    finally:
        index.close()


class FakeOpenSearch:
    def __init__(self) -> None:
        self.created = False
        self.docs: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/_cluster/health":
            return httpx.Response(200, json={"status": "yellow"})
        if path == "/owli_chunks" and request.method == "HEAD":
            return httpx.Response(200 if self.created else 404)
        if path == "/owli_chunks" and request.method == "PUT":
            self.created = True
            return httpx.Response(200, json={"acknowledged": True})
        if path == "/_bulk":
            lines = request.content.decode("utf-8").splitlines()
            for action_line, source_line in zip(lines[::2], lines[1::2], strict=True):
                action = json.loads(action_line)
                source = json.loads(source_line)
                self.docs[action["index"]["_id"]] = source
            return httpx.Response(
                200,
                json={"errors": False, "items": [{"index": {"status": 201}}]},
            )
        if path == "/owli_chunks/_search":
            body = _body(request)
            clauses = body["query"]["bool"]["filter"]
            query = str(body["query"]["bool"]["must"][0]["match"]["text"]["query"])
            terms = query.split()
            rows = []
            for doc_id, source in self.docs.items():
                if not _matches(source, clauses):
                    continue
                score = sum(source["text"].count(term) for term in terms)
                if score:
                    rows.append({"_id": doc_id, "_score": float(score), "_source": source})
            rows.sort(key=lambda row: (-row["_score"], row["_id"]))
            return httpx.Response(200, json={"hits": {"hits": rows[: body["size"]]}})
        if path == "/owli_chunks/_delete_by_query":
            clauses = _body(request)["query"]["bool"]["filter"]
            stale = [doc_id for doc_id, source in self.docs.items() if _matches(source, clauses)]
            for doc_id in stale:
                self.docs.pop(doc_id, None)
            return httpx.Response(200, json={"deleted": len(stale)})
        return httpx.Response(500, json={"error": f"unexpected {request.method} {path}"})


async def test_container_wires_configured_retrieval_backends(tmp_path) -> None:
    settings = load_boot_settings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path}/wiring.db"},
        storage={
            "default_profile_ref": "local-primary",
            "local": {"roots": [{"id": "local-primary-root", "path": str(tmp_path / "objects")}]},
        },
        retrieval={
            "enabled": True,
            "dense_backend": "qdrant",
            "lexical_backend": "opensearch",
            "qdrant": {"url": "http://qdrant.test", "collection": "owli_chunks"},
            "opensearch": {"url": "http://opensearch.test", "index": "owli_chunks"},
        },
    )
    container = Container(settings=settings, clock=FixedClock())
    try:
        assert isinstance(container.dense_index, QdrantDenseIndex)
        assert isinstance(container.lexical_index, OpenSearchLexicalIndex)
    finally:
        await container.dispose()


def _opensearch_index(fake: FakeOpenSearch) -> OpenSearchLexicalIndex:
    client = httpx.Client(
        base_url="http://opensearch.test",
        transport=httpx.MockTransport(fake),
    )
    return OpenSearchLexicalIndex(url="http://opensearch.test", index="owli_chunks", client=client)


def test_opensearch_lexical_contract() -> None:
    fake = FakeOpenSearch()
    index = _opensearch_index(fake)
    try:
        assert index.check_health()
        index.upsert(
            [
                _doc("d1", text="检索增强生成系统设计"),
                _doc("d2", text="数据库索引优化", dataset="ds2", acl="dataset:ds2"),
                _doc("d3", text="retrieval augmented generation"),
            ]
        )
        hits = index.search("检索 生成", limit=5, filter_=IndexFilter(tenant_id="t1"))
        assert hits and hits[0].id == "d1"
        assert hits[0].raw_score > 0

        assert [hit.id for hit in index.search("retrieval", limit=5, filter_=IndexFilter("t1"))] == ["d3"]
        filtered = index.search(
            "检索 生成",
            limit=5,
            filter_=IndexFilter(tenant_id="t1", dataset_ids=frozenset({"ds2"})),
        )
        assert filtered == []
        assert index.search("检索", limit=5, filter_=IndexFilter(tenant_id="other")) == []
        assert index.search("   ", limit=5, filter_=IndexFilter(tenant_id="t1")) == []

        index.upsert([_doc("d3", text="完全不同的内容")])
        assert index.search("retrieval", limit=5, filter_=IndexFilter(tenant_id="t1")) == []
        assert [hit.id for hit in index.search("不同", limit=5, filter_=IndexFilter("t1"))] == ["d3"]

        index.upsert([_doc("d4", text="新一代文档", generation=2)])
        assert index.delete_by_generation(tenant_id="t1", version_id="v1", before_generation=2) == 3
        remaining = index.search("新一代", limit=10, filter_=IndexFilter(tenant_id="t1"))
        assert [hit.id for hit in remaining] == ["d4"]
    finally:
        index.close()
