"""问答路由：SSE 流式问答、运行查询与事件重放（断流恢复）。

- POST /v1/chat/runs：创建运行并返回 SSE 事件流（data: json；结尾 [DONE]）；
- GET /v1/chat/runs/{run_id}：运行状态；
- GET /v1/chat/runs/{run_id}/events?after_seq=：事件重放（断流后从序号续读）；
- 外部应用须持有 chat:invoke scope（dependencies 规则表集中强制，fail-closed）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import Field

from owli_parser.bootstrap.dependencies import ActorState, get_container, resolve_actor, resolve_scope
from owli_parser.bootstrap.routes_catalog import _Strict
from owli_parser.modules.catalog.service import CatalogScope

router = APIRouter()


class ChatRunCreate(_Strict):
    question: str = Field(min_length=1, max_length=4000)
    dataset_ids: list[uuid.UUID] = Field(default_factory=list)
    parent_run_id: uuid.UUID | None = None


def _run_payload(info: Any) -> dict[str, Any]:
    return {
        "id": str(info.id),
        "state": info.state,
        "dataset_ids": info.dataset_ids,
        "request_snapshot": info.request_snapshot,
        "error": info.error,
        "parent_run_id": str(info.parent_run_id) if info.parent_run_id else None,
    }


@router.post("/chat/runs")
async def create_chat_run(
    payload: ChatRunCreate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> StreamingResponse:
    container = get_container(state.request)
    run = await container.chat.create_run(
        scope,
        question=payload.question,
        dataset_ids=[str(value) for value in payload.dataset_ids],
        parent_run_id=payload.parent_run_id,
    )

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in container.chat.execute(run.id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/chat/runs/{run_id}")
async def get_chat_run(
    run_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    info = await container.chat.get_run(scope, run_id)
    return _run_payload(info)


@router.post("/chat/runs/{run_id}/cancel")
async def cancel_chat_run(
    run_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    """幂等取消：非终态置 cancelled，生成循环在下一检查点结束（已送达增量不撤回）。"""
    container = get_container(state.request)
    info = await container.chat.request_cancel(scope, run_id)
    return _run_payload(info)


@router.get("/chat/runs/{run_id}/events")
async def list_chat_events(
    run_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    after_seq: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    events = await container.chat.list_events(scope, run_id, after_seq=after_seq)
    return [{"seq": event.seq, "type": event.event_type, "payload": event.payload} for event in events]
