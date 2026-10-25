"""实时通道：WS 票据与任务事件推送（/v1/auth/ws-ticket、/v1/ws/jobs/{id}）。

- 浏览器事件连接使用租户绑定的一次性 ticket；长期凭据不进入 URL。
- 快照与事件分离：连接先发快照与 latest_seq，再按 seq 增量推送；
  历史过期或断线重连由 HTTP `/events?after_seq=` 补发（同一事件表）。
- 权限变更/登录过期后主动断开：每轮推送前复核 membership。
- 外部应用首期使用事件 HTTP 查询，不签 WS ticket。
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict

from owli_parser.bootstrap.dependencies import ActorState, get_container, resolve_actor
from owli_parser.core.errors import AppError, InvalidInputError
from owli_parser.modules.jobs.service import JobEventInfo, JobInfo

router = APIRouter(tags=["realtime"])

_POLL_SECONDS = 1.0
_HEARTBEAT_EVERY_TICKS = 15
_SNAPSHOT_EVENT_LIMIT = 500


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WsTicketRequest(_Strict):
    tenant_id: uuid.UUID


class WsTicketResponse(_Strict):
    ticket: str
    expires_in_seconds: int


@router.post("/auth/ws-ticket")
async def issue_ws_ticket(
    payload: WsTicketRequest, state: ActorState = Depends(resolve_actor)
) -> WsTicketResponse:
    if state.is_application:
        raise InvalidInputError("应用 Token 首期请使用事件 HTTP 查询，不签 WS ticket")
    container = get_container(state.request)
    # 必须是该租户的有效成员（与 X-Tenant-ID 同等校验）
    await container.identity.resolve_tenant_membership(state.actor.subject_id, payload.tenant_id)
    session_id = state.actor.credential_id
    if session_id is None:
        raise InvalidInputError("缺少会话凭据")
    ticket, ttl = container.identity.issue_ws_ticket(state.actor.subject_id, payload.tenant_id, session_id)
    return WsTicketResponse(ticket=ticket, expires_in_seconds=ttl)


@router.websocket("/ws/jobs/{job_id}")
async def ws_job_events(websocket: WebSocket, job_id: uuid.UUID) -> None:
    container = websocket.app.state.container
    ticket = websocket.query_params.get("ticket") or ""
    try:
        claims = container.identity.verify_ws_ticket(ticket)
        # 成员关系与租户状态复核（撤权后新连接立即拒绝）
        await container.identity.resolve_tenant_membership(claims.user_id, claims.tenant_id)
        job = await container.jobs.get_job(claims.tenant_id, job_id)
    except AppError as exc:
        # 握手拒绝：使用 4401/4404 应用层关闭码，不泄露细节
        auth_codes = {"not_authenticated", "session_expired", "invalid_credentials"}
        code = 4401 if str(exc.code) in auth_codes else 4404
        await websocket.close(code=code)
        return

    await websocket.accept()
    try:
        # 快照 + 起点 seq：快照包含截至 latest_seq 的历史事件，之后只推增量
        # （客户端按 seq 去重）。只给 latest_seq 会让终态任务的历史时间线永远为空。
        events, latest_seq = await container.jobs.list_events(
            claims.tenant_id, job_id, after_seq=0, limit=_SNAPSHOT_EVENT_LIMIT
        )
        await websocket.send_json(
            {
                "type": "snapshot",
                "job": _job_payload(job),
                "latest_seq": latest_seq,
                "events": [_event_payload(event) for event in events],
                "truncated": len(events) >= _SNAPSHOT_EVENT_LIMIT,
            }
        )
        after_seq = latest_seq
        ticks = 0
        while True:
            await asyncio.sleep(_POLL_SECONDS)
            # 每轮复核授权：失效即以 4403 关闭（撤权语义）
            try:
                await container.identity.resolve_tenant_membership(claims.user_id, claims.tenant_id)
            except AppError:
                await websocket.close(code=4403)
                return
            events, latest_seq = await container.jobs.list_events(
                claims.tenant_id, job_id, after_seq=after_seq
            )
            for event in events:
                await websocket.send_json({"type": "event", "event": _event_payload(event)})
            if events:
                after_seq = events[-1].seq
            ticks += 1
            if ticks % _HEARTBEAT_EVERY_TICKS == 0:
                await websocket.send_json({"type": "heartbeat"})
            job = await container.jobs.get_job(claims.tenant_id, job_id)
            if job.state in {"DONE", "PARTIAL_DONE", "FAILED", "DEAD_LETTER", "CANCELLED"}:
                pending_events, _ = await container.jobs.list_events(
                    claims.tenant_id, job_id, after_seq=after_seq
                )
                if not pending_events:
                    await websocket.send_json({"type": "closed", "state": job.state})
                    await websocket.close(code=1000)
                    return
    except WebSocketDisconnect:
        return


def _event_payload(event: JobEventInfo) -> dict[str, object]:
    return {
        "seq": event.seq,
        "type": event.event_type,
        "attempt": event.attempt,
        "from_state": event.from_state,
        "to_state": event.to_state,
        "ts": event.ts.isoformat(),
        "detail": event.detail,
    }


def _job_payload(job: JobInfo) -> dict[str, object]:
    return {
        "id": str(job.id),
        "document_id": str(job.document_id),
        "version_id": str(job.version_id),
        "dataset_id": str(job.dataset_id),
        "state": job.state,
        "stage": job.stage,
        "attempt": job.attempt,
        "progress": job.progress,
        "total_units": job.total_units,
        "completed_units": job.completed_units,
        "coverage": job.coverage,
        "error": job.error,
        "retrieval_ready": job.retrieval_ready,
    }
