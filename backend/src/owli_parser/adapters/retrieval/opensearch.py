"""OpenSearch BM25 词法索引适配器（REST API，同步调用由检索服务放入线程池）。

- index 首次写入时创建 keyword/integer/text 映射；正文只用于索引，查询响应不返回正文；
- 写入使用 ``_bulk`` 并检查逐项错误；``refresh_on_write`` 默认 true，发布后即可搜；
- tenant/dataset/ACL/generation 全部进入 bool.filter，不由应用层事后裁剪。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from urllib.parse import quote

import httpx

from owli_parser.adapters.retrieval.in_memory import hit_payload
from owli_parser.ports.retrieval import (
    IndexDocument,
    IndexFilter,
    IndexHit,
    IndexWriteReceipt,
    RetrievalIndexError,
)

_MAPPING = {
    "mappings": {
        "properties": {
            "tenant_id": {"type": "keyword"},
            "dataset_id": {"type": "keyword"},
            "version_id": {"type": "keyword"},
            "chunk_id": {"type": "keyword"},
            "chunk_revision": {"type": "integer"},
            "index_generation": {"type": "integer"},
            "acl_ref": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "kind": {"type": "keyword"},
            "text": {"type": "text"},
        }
    }
}


def _term(field: str, value: object) -> dict[str, object]:
    return {"term": {field: value}}


def _terms(field: str, values: Sequence[str]) -> dict[str, object]:
    return {"terms": {field: list(values)}}


def _filter_conditions(filter_: IndexFilter) -> list[dict[str, object]]:
    conditions: list[dict[str, object]] = [_term("tenant_id", filter_.tenant_id)]
    if filter_.dataset_ids:
        conditions.append(_terms("dataset_id", sorted(filter_.dataset_ids)))
    if filter_.acl_refs:
        conditions.append(_terms("acl_ref", sorted(filter_.acl_refs)))
    if filter_.index_generation is not None:
        conditions.append(_term("index_generation", filter_.index_generation))
    return conditions


class OpenSearchLexicalIndex:
    """LexicalIndex 的 OpenSearch 实现；client 可注入 MockTransport。"""

    def __init__(
        self,
        *,
        url: str,
        index: str,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 10,
        verify_tls: bool = True,
        refresh_on_write: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self._index = index
        self._index_path = f"/{quote(index, safe='')}"
        headers: dict[str, str] = {}
        if api_key:
            headers["authorization"] = f"ApiKey {api_key}"
        auth = (username, password) if username and password else None
        self._client = client or httpx.Client(
            base_url=url.rstrip("/"),
            headers=headers,
            auth=auth,
            timeout=timeout_seconds,
            verify=verify_tls,
        )
        self._refresh = "true" if refresh_on_write else "false"
        self._ready = False
        self._ensure_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def check_health(self) -> bool:
        """集群 health 为 green/yellow 即可用；red 或网络失败返回 False。"""
        try:
            response = self._send("GET", "/_cluster/health", params={"timeout": "2s"})
            payload = self._json(response)
        except RetrievalIndexError:
            return False
        return payload.get("status") in ("green", "yellow")

    def _send(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise RetrievalIndexError(f"OpenSearch 连接失败: {exc.__class__.__name__}") from exc

    @staticmethod
    def _error(response: httpx.Response) -> RetrievalIndexError:
        return RetrievalIndexError(f"OpenSearch HTTP {response.status_code}: {response.text[:300]}")

    def _json(self, response: httpx.Response) -> dict[str, object]:
        if response.status_code >= 400:
            raise self._error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RetrievalIndexError("OpenSearch 返回非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise RetrievalIndexError("OpenSearch 响应结构不合法")
        return payload

    def _ensure_index(self) -> None:
        if self._ready:
            return
        with self._ensure_lock:
            # 第一层检查之后，其他线程可能已完成初始化。
            if self._ready:
                return  # type: ignore[unreachable]
            response = self._send("HEAD", self._index_path)
            if response.status_code == 404:
                created = self._send("PUT", self._index_path, json=_MAPPING)
                if created.status_code >= 400 and "resource_already_exists" not in created.text:
                    raise self._error(created)
            elif response.status_code >= 400:
                raise self._error(response)
            self._ready = True

    def upsert(self, documents: Sequence[IndexDocument]) -> IndexWriteReceipt:
        if not documents:
            return IndexWriteReceipt(upserted=0, deleted=0)
        self._ensure_index()
        lines: list[str] = []
        for doc in documents:
            lines.append(json.dumps({"index": {"_id": doc.id}}, separators=(",", ":")))
            lines.append(
                json.dumps(
                    {"text": doc.text, **hit_payload(doc)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        response = self._send(
            "POST",
            "/_bulk",
            params={"refresh": self._refresh},
            content=("\n".join(lines) + "\n").encode("utf-8"),
            headers={"content-type": "application/x-ndjson"},
        )
        payload = self._json(response)
        if payload.get("errors"):
            items = payload.get("items")
            first_error: object = None
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        action = item.get("index")
                        if isinstance(action, dict) and action.get("error"):
                            first_error = action["error"]
                            break
            raise RetrievalIndexError(f"OpenSearch bulk 写入失败: {first_error}")
        return IndexWriteReceipt(upserted=len(documents), deleted=0)

    def search(self, query: str, *, limit: int, filter_: IndexFilter) -> list[IndexHit]:
        clean = query.strip()
        if not clean:
            return []
        self._ensure_index()
        response = self._send(
            "POST",
            f"{self._index_path}/_search",
            json={
                "size": limit,
                "_source": [
                    "chunk_id",
                    "chunk_revision",
                    "version_id",
                    "dataset_id",
                    "index_generation",
                    "document_id",
                    "kind",
                ],
                "query": {
                    "bool": {
                        "must": [{"match": {"text": {"query": clean}}}],
                        "filter": _filter_conditions(filter_),
                    }
                },
            },
        )
        payload = self._json(response)
        hits_block = payload.get("hits")
        rows = hits_block.get("hits") if isinstance(hits_block, dict) else None
        if not isinstance(rows, list):
            raise RetrievalIndexError("OpenSearch 搜索响应缺少 hits")
        hits: list[IndexHit] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            hit_id = row.get("_id")
            score = row.get("_score")
            source = row.get("_source")
            if isinstance(hit_id, str) and isinstance(score, (int, float)):
                hits.append(
                    IndexHit(
                        id=hit_id,
                        raw_score=float(score),
                        payload=source if isinstance(source, dict) else {},
                    )
                )
        return hits

    def delete_by_generation(self, *, tenant_id: str, version_id: str, before_generation: int) -> int:
        self._ensure_index()
        response = self._send(
            "POST",
            f"{self._index_path}/_delete_by_query",
            params={"refresh": self._refresh, "conflicts": "proceed"},
            json={
                "query": {
                    "bool": {
                        "filter": [
                            _term("tenant_id", tenant_id),
                            _term("version_id", version_id),
                            {"range": {"index_generation": {"lt": before_generation}}},
                        ]
                    }
                }
            },
        )
        payload = self._json(response)
        deleted = payload.get("deleted")
        return int(deleted) if isinstance(deleted, int) else 0
