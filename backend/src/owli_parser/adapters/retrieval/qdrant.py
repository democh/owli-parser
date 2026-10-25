"""Qdrant dense 索引适配器（REST API，同步调用由检索服务放入线程池）。

- collection 首次写入时按向量维度创建；已有 collection 维度不一致直接拒绝；
- point id 使用 ``uuid5(NAMESPACE_URL, 逻辑ID)``，逻辑 ID 保存在 payload，
  避免 Qdrant 对 "tenant:version:chunk:revision:generation" 的 ID 类型限制；
- tenant/dataset/ACL/generation 全部作为前置过滤条件，不依赖候选返回后再删；
- 删除旧代前先 exact count，保证端口返回真实回收数量。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Sequence
from urllib.parse import quote

import httpx

from owli_parser.adapters.retrieval.in_memory import hit_payload
from owli_parser.ports.retrieval import (
    DimensionMismatchError,
    IndexDocument,
    IndexFilter,
    IndexHit,
    IndexWriteReceipt,
    RetrievalIndexError,
)

_ID_FIELD = "_owli_id"
_UUID_NAMESPACE = uuid.UUID("8e4f3e94-5f9d-4f76-8e2a-08f0fa1f7f4e")


def _logical_id(document_id: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, document_id))


def _match(key: str, values: Sequence[str]) -> dict[str, object]:
    return {"key": key, "match": {"any": list(values)}}


def _filter_conditions(filter_: IndexFilter) -> list[dict[str, object]]:
    conditions: list[dict[str, object]] = [_match("tenant_id", [filter_.tenant_id])]
    if filter_.dataset_ids:
        conditions.append(_match("dataset_id", sorted(filter_.dataset_ids)))
    if filter_.acl_refs:
        conditions.append(_match("acl_ref", sorted(filter_.acl_refs)))
    if filter_.index_generation is not None:
        conditions.append({"key": "index_generation", "match": {"value": filter_.index_generation}})
    return conditions


class QdrantDenseIndex:
    """DenseIndex 的 Qdrant 实现；client 可注入 MockTransport 做契约测试。"""

    def __init__(
        self,
        *,
        url: str,
        collection: str,
        api_key: str | None = None,
        timeout_seconds: float = 10,
        verify_tls: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self._collection = collection
        self._collection_path = f"/collections/{quote(collection, safe='')}"
        self._client = client or httpx.Client(
            base_url=url.rstrip("/"),
            headers={"api-key": api_key} if api_key else {},
            timeout=timeout_seconds,
            verify=verify_tls,
        )
        self._dimension: int | None = None
        self._ensure_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def check_health(self) -> bool:
        """轻量健康探针；不创建 collection，避免只读实例改变集群状态。"""
        try:
            response = self._send("GET", "/healthz")
        except RetrievalIndexError:
            return False
        return 200 <= response.status_code < 300

    def _send(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise RetrievalIndexError(f"Qdrant 连接失败: {exc.__class__.__name__}") from exc

    @staticmethod
    def _error(response: httpx.Response) -> RetrievalIndexError:
        body = response.text[:300]
        if "dimension" in body.lower():
            return DimensionMismatchError(f"Qdrant 向量维度不匹配: {body}")
        return RetrievalIndexError(f"Qdrant HTTP {response.status_code}: {body}")

    def _json(self, response: httpx.Response) -> dict[str, object]:
        if response.status_code >= 400:
            raise self._error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RetrievalIndexError("Qdrant 返回非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise RetrievalIndexError("Qdrant 响应结构不合法")
        return payload

    @staticmethod
    def _dimension_from_collection(payload: dict[str, object]) -> int:
        result = payload.get("result")
        config = result.get("config") if isinstance(result, dict) else None
        params = config.get("params") if isinstance(config, dict) else None
        vectors = params.get("vectors") if isinstance(params, dict) else None
        if isinstance(vectors, dict):
            if isinstance(vectors.get("size"), int):
                return int(vectors["size"])
            first = next(iter(vectors.values()), None)
            if isinstance(first, dict) and isinstance(first.get("size"), int):
                return int(first["size"])
        raise RetrievalIndexError("Qdrant collection 缺少向量维度信息")

    def _create_collection(self, dimension: int) -> bool:
        response = self._send(
            "PUT",
            self._collection_path,
            json={"vectors": {"size": dimension, "distance": "Cosine"}},
        )
        if response.status_code >= 400:
            # 并发进程可能已创建；由后续 GET 校验维度，不把竞态当失败。
            return False
        for field, schema in (
            ("tenant_id", "keyword"),
            ("dataset_id", "keyword"),
            ("acl_ref", "keyword"),
            ("version_id", "keyword"),
            ("index_generation", "integer"),
        ):
            index_response = self._send(
                "PUT",
                f"{self._collection_path}/index",
                params={"wait": "true"},
                json={"field_name": field, "field_schema": schema},
            )
            if index_response.status_code not in (200, 201, 409):
                raise self._error(index_response)
        return True

    def _collection_dimension(self, *, create_for: int | None = None) -> int | None:
        if self._dimension is not None:
            return self._dimension
        with self._ensure_lock:
            # 第一层检查之后，其他线程可能已完成初始化。
            if self._dimension is not None:
                return self._dimension  # type: ignore[unreachable]
            response = self._send("GET", self._collection_path)
            if response.status_code == 404:
                if create_for is None:
                    return None
                if not self._create_collection(create_for):
                    response = self._send("GET", self._collection_path)
                    payload = self._json(response)
                else:
                    self._dimension = create_for
                    return create_for
            else:
                payload = self._json(response)
            dimension = self._dimension_from_collection(payload)
            self._dimension = dimension
            return dimension

    @staticmethod
    def _validate_documents(documents: Sequence[IndexDocument]) -> int:
        dimensions = {len(doc.vector or []) for doc in documents}
        if 0 in dimensions:
            raise DimensionMismatchError("dense 写入缺少向量")
        if len(dimensions) != 1:
            raise DimensionMismatchError("同一批次向量维度不一致")
        return dimensions.pop()

    def upsert(self, documents: Sequence[IndexDocument]) -> IndexWriteReceipt:
        if not documents:
            return IndexWriteReceipt(upserted=0, deleted=0)
        dimension = self._validate_documents(documents)
        existing = self._collection_dimension(create_for=dimension)
        if existing != dimension:
            raise DimensionMismatchError(f"向量维度不一致（期望 {existing}，收到 {dimension}）")
        points: list[dict[str, object]] = []
        for doc in documents:
            payload: dict[str, object] = hit_payload(doc)
            payload[_ID_FIELD] = doc.id
            points.append(
                {
                    "id": _logical_id(doc.id),
                    "vector": [float(value) for value in doc.vector or []],
                    "payload": payload,
                }
            )
        response = self._send(
            "PUT",
            f"{self._collection_path}/points",
            params={"wait": "true"},
            json={"points": points},
        )
        self._json(response)
        return IndexWriteReceipt(upserted=len(points), deleted=0)

    def search(self, query_vector: Sequence[float], *, limit: int, filter_: IndexFilter) -> list[IndexHit]:
        vector = [float(value) for value in query_vector]
        if not vector:
            return []
        dimension = self._collection_dimension()
        if dimension is None:
            return []
        if dimension != len(vector):
            raise DimensionMismatchError("查询向量维度与 Qdrant collection 不一致")
        response = self._send(
            "POST",
            f"{self._collection_path}/points/query",
            json={
                "query": vector,
                "filter": {"must": _filter_conditions(filter_)},
                "limit": limit,
                "with_payload": True,
            },
        )
        payload = self._json(response)
        result = payload.get("result")
        points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(points, list):
            raise RetrievalIndexError("Qdrant 查询响应缺少 points")
        hits: list[IndexHit] = []
        for point in points:
            if not isinstance(point, dict):
                continue
            point_payload = point.get("payload")
            metadata = dict(point_payload) if isinstance(point_payload, dict) else {}
            raw_id = metadata.pop(_ID_FIELD, None)
            hit_id = raw_id if isinstance(raw_id, str) else str(point.get("id", ""))
            score = point.get("score")
            if hit_id and isinstance(score, (int, float)):
                hits.append(IndexHit(id=hit_id, raw_score=float(score), payload=metadata))
        return hits

    def delete_by_generation(self, *, tenant_id: str, version_id: str, before_generation: int) -> int:
        if self._collection_dimension() is None:
            return 0
        filter_body = {
            "must": [
                _match("tenant_id", [tenant_id]),
                _match("version_id", [version_id]),
                {"key": "index_generation", "range": {"lt": before_generation}},
            ]
        }
        count_response = self._send(
            "POST",
            f"{self._collection_path}/points/count",
            json={"filter": filter_body, "exact": True},
        )
        count_payload = self._json(count_response)
        count_result = count_payload.get("result")
        count = count_result.get("count") if isinstance(count_result, dict) else 0
        deleted = int(count) if isinstance(count, int) else 0
        if deleted:
            response = self._send(
                "POST",
                f"{self._collection_path}/points/delete",
                params={"wait": "true"},
                json={"filter": filter_body},
            )
            self._json(response)
        return deleted
