"""模型网关适配器：OpenAI 兼容 /embeddings 客户端。

- httpx.AsyncClient 注入（测试用 MockTransport；生产按连接 base_url 构造）；
- 响应守卫：条数一致、**每向量长度必须等于声明维度**（不截断不补齐——
  维度不符意味着索引集合契约被破坏，必须显式失败）；
- 错误映射：HTTP 非 2xx → ExternalServiceUnavailableError（含状态码），
  超时 → UpstreamTimeoutError（两者均有界、可重试语义由上层决定）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable

import httpx

from owli_parser.core.errors import ExternalServiceUnavailableError, UpstreamTimeoutError


class EmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        dimensions: int,
        api_key: str | None,
        client: httpx.AsyncClient,
        timeout_seconds: float = 30.0,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions 必须为正（来自 embedding 档案的声明）")
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._dimensions = dimensions
        self._api_key = api_key
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入；空批直接返回（不调用上游）。"""
        if not texts:
            return []
        payload: dict[str, object] = {"model": self._model_name, "input": list(texts)}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                json=payload,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"embedding 调用超时（{self._timeout_seconds}s）") from exc
        except httpx.HTTPError as exc:
            raise ExternalServiceUnavailableError(f"embedding 调用失败：{exc}") from exc

        if response.status_code >= 400:
            raise ExternalServiceUnavailableError(
                f"embedding 服务返回 {response.status_code}",
                detail={"status": response.status_code},
            )
        try:
            body = response.json()
            entries = body["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ExternalServiceUnavailableError("embedding 响应缺少 data 字段") from exc
        if not isinstance(entries, list) or len(entries) != len(texts):
            raise ExternalServiceUnavailableError(
                f"embedding 响应条数不符：期望 {len(texts)} 实际 "
                f"{len(entries) if isinstance(entries, list) else '非列表'}"
            )
        vectors: list[list[float]] = []
        for index, entry in enumerate(entries):
            vector = entry.get("embedding") if isinstance(entry, dict) else None
            if not isinstance(vector, list) or len(vector) != self._dimensions:
                actual = len(vector) if isinstance(vector, list) else "非法"
                raise ExternalServiceUnavailableError(
                    f"第 {index} 条向量维度不符：声明 {self._dimensions} 实际 {actual}"
                    "（维度不符不可截断/补齐，需新建档案与索引集合）"
                )
            vectors.append([float(value) for value in vector])
        return vectors


class RerankClient:
    """OpenAI/Cohere 兼容 /rerank 客户端（httpx.AsyncClient 注入）。

    - 请求：{"model", "query", "documents"}；
    - 响应：{"results": [{"index", "relevance_score"}, ...]}——index 必须覆盖
      [0, n) 恰好一次（缺项/越界/重复均为响应不完整，显式失败不猜测）；
    - 返回按输入顺序归位的分数列表。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        api_key: str | None,
        client: httpx.AsyncClient,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._api_key = api_key
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._client.post(
                f"{self._base_url}/rerank",
                json={"model": self._model_name, "query": query, "documents": list(documents)},
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"rerank 调用超时（{self._timeout_seconds}s）") from exc
        except httpx.HTTPError as exc:
            raise ExternalServiceUnavailableError(f"rerank 调用失败：{exc}") from exc
        if response.status_code >= 400:
            raise ExternalServiceUnavailableError(
                f"rerank 服务返回 {response.status_code}",
                detail={"status": response.status_code},
            )
        try:
            body = response.json()
            results = body["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ExternalServiceUnavailableError("rerank 响应缺少 results 字段") from exc
        if not isinstance(results, list):
            raise ExternalServiceUnavailableError("rerank 响应 results 非列表")
        scores: list[float | None] = [None] * len(documents)
        for entry in results:
            index = entry.get("index") if isinstance(entry, dict) else None
            score = entry.get("relevance_score") if isinstance(entry, dict) else None
            if not isinstance(index, int) or not 0 <= index < len(documents):
                raise ExternalServiceUnavailableError(f"rerank 响应含越界 index：{index!r}")
            if scores[index] is not None:
                raise ExternalServiceUnavailableError(f"rerank 响应 index {index} 重复")
            if not isinstance(score, (int, float)):
                raise ExternalServiceUnavailableError(f"rerank 响应 index {index} 分数非法")
            scores[index] = float(score)
        missing = [i for i, value in enumerate(scores) if value is None]
        if missing:
            raise ExternalServiceUnavailableError(
                f"rerank 响应不完整，缺少 index：{missing}（不猜测缺失分数）"
            )
        return [value for value in scores if value is not None]


class GenerationClient:
    """OpenAI 兼容 /chat/completions 流式客户端（httpx.AsyncClient 注入）。

    - 请求：{"model", "messages", "stream": true}；
    - 响应：SSE 行 `data: {json}`，取 choices[0].delta.content 增量；`data: [DONE]` 结束；
    - 非 data 行与无法解析的 data 行跳过（不猜测内容）；HTTP ≥ 400 与超时显式失败。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        api_key: str | None,
        client: httpx.AsyncClient,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._api_key = api_key
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> AsyncGenerator[str, None]:
        """逐 token 产出正文增量（AsyncGenerator[str, None]）。

        on_usage 不为空时请求 stream_options.include_usage，并在最终 usage 行回调
        token 数（供应商不支持该选项时可能不返回 usage，调用方按字符估算兜底）。
        """
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, object] = {
            "model": self._model_name,
            "messages": messages,
            "stream": True,
        }
        if on_usage is not None:
            body["stream_options"] = {"include_usage": True}
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=self._timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    raise ExternalServiceUnavailableError(
                        f"generation 服务返回 {response.status_code}",
                        detail={"status": response.status_code},
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        parsed = json.loads(data)
                    except ValueError:
                        continue
                    usage = parsed.get("usage") if isinstance(parsed, dict) else None
                    if on_usage is not None and isinstance(usage, dict):
                        on_usage(
                            {
                                key: int(value)
                                for key, value in usage.items()
                                if isinstance(value, (int, float))
                            }
                        )
                    try:
                        delta = parsed["choices"][0]["delta"].get("content")
                    except (KeyError, IndexError, TypeError, AttributeError):
                        continue  # 非增量结构行（usage/ping）跳过
                    if isinstance(delta, str) and delta:
                        yield delta
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"generation 调用超时（{self._timeout_seconds}s）") from exc
        except httpx.HTTPError as exc:
            raise ExternalServiceUnavailableError(f"generation 调用失败：{exc}") from exc
