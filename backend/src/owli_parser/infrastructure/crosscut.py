"""横切基础设施实现：outbox 记录器、审计记录器、幂等存储。

这些实现不依赖业务模块，通过端口注入使用；写操作优先加入调用方会话
（同一事务），未提供会话时自行开启短事务。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from owli_parser.core.clock import Clock
from owli_parser.core.context import ActorKind
from owli_parser.infrastructure.crosscut_models import (
    AuditLogRow,
    IdempotencyRecordRow,
    OutboxEventRow,
)
from owli_parser.ports.idempotency import IdempotencyConflict, IdempotencyRecord


class SqlOutboxRecorder:
    """outbox 记录器：优先写入调用方传入的 session（同一事务）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        revision: int = 1,
        session: AsyncSession | None = None,
    ) -> None:
        row = OutboxEventRow(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            revision=revision,
            event_type=event_type,
            payload=payload,
            delivery_state="pending",
        )
        if session is not None:
            session.add(row)
            return
        async with self._session_factory() as own_session:
            own_session.add(row)
            await own_session.commit()


class SqlAuditRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        action: str,
        actor_kind: ActorKind,
        actor_id: uuid.UUID | None,
        tenant_id: uuid.UUID | None,
        credential_id: uuid.UUID | None,
        resource_type: str,
        resource_id: str | None,
        trace_id: str,
        decision: str | None = None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        row = AuditLogRow(
            tenant_id=tenant_id,
            actor_kind=str(actor_kind),
            actor_id=actor_id,
            credential_id=credential_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=decision,
            reason=reason,
            trace_id=trace_id,
            detail=detail or {},
        )
        if session is not None:
            session.add(row)
            return
        async with self._session_factory() as own_session:
            own_session.add(row)
            await own_session.commit()


class SqlIdempotencyStore:
    """请求幂等存储。

    并发策略：先查（同 key 同摘要直接复用）；插入依赖唯一约束兜底；
    in_progress 记录表示首个请求尚未完成，重放方返回 409 冲突语义由调用方决定。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        retention_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._retention_seconds = retention_seconds

    async def lookup(
        self,
        *,
        tenant_id: uuid.UUID | None,
        actor_kind: str,
        actor_id: uuid.UUID,
        operation: str,
        key: str,
    ) -> IdempotencyRecord | None:
        async with self._session_factory() as session:
            row = await self._find(session, tenant_id, actor_kind, actor_id, operation, key)
            return self._to_record(row) if row is not None else None

    async def begin(
        self,
        *,
        tenant_id: uuid.UUID | None,
        actor_kind: str,
        actor_id: uuid.UUID,
        operation: str,
        key: str,
        request_hash: str,
        session: AsyncSession | None = None,
    ) -> IdempotencyRecord:
        if session is not None:
            return await self._begin(session, tenant_id, actor_kind, actor_id, operation, key, request_hash)
        async with self._session_factory() as own_session:
            record = await self._begin(
                own_session, tenant_id, actor_kind, actor_id, operation, key, request_hash
            )
            await own_session.commit()
            return record

    async def _begin(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID | None,
        actor_kind: str,
        actor_id: uuid.UUID,
        operation: str,
        key: str,
        request_hash: str,
    ) -> IdempotencyRecord:
        existing = await self._find(session, tenant_id, actor_kind, actor_id, operation, key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyConflict("同一幂等键对应了不同请求")
            return self._to_record(existing)
        now = self._clock.now()
        row = IdempotencyRecordRow(
            tenant_id=tenant_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            operation=operation,
            key=key,
            request_hash=request_hash,
            response_ref={},
            state="in_progress",
            expires_at=now + timedelta(seconds=self._retention_seconds),
        )
        session.add(row)
        await session.flush()
        return self._to_record(row)

    async def complete(
        self,
        *,
        record_id: uuid.UUID,
        response_ref: dict[str, Any],
        session: AsyncSession | None = None,
    ) -> None:
        stmt = (
            update(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.id == record_id)
            .values(response_ref=response_ref, state="completed")
        )
        if session is not None:
            await session.execute(stmt)
            return
        async with self._session_factory() as own_session:
            await own_session.execute(stmt)
            await own_session.commit()

    async def _find(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID | None,
        actor_kind: str,
        actor_id: uuid.UUID,
        operation: str,
        key: str,
    ) -> IdempotencyRecordRow | None:
        stmt = select(IdempotencyRecordRow).where(
            IdempotencyRecordRow.tenant_id.is_(None)
            if tenant_id is None
            else IdempotencyRecordRow.tenant_id == tenant_id,
            IdempotencyRecordRow.actor_kind == actor_kind,
            IdempotencyRecordRow.actor_id == actor_id,
            IdempotencyRecordRow.operation == operation,
            IdempotencyRecordRow.key == key,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _to_record(row: IdempotencyRecordRow) -> IdempotencyRecord:
        return IdempotencyRecord(
            id=row.id,
            tenant_id=row.tenant_id,
            actor_kind=row.actor_kind,
            actor_id=row.actor_id,
            operation=row.operation,
            key=row.key,
            request_hash=row.request_hash,
            response_ref=row.response_ref or {},
            state=row.state,
        )
