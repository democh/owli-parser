"""Outbox 端口：事务内登记异步动作，派发器补偿消息丢失。"""

from __future__ import annotations

from typing import Any, Protocol


class OutboxRecorder(Protocol):
    """在调用方 PG 事务内登记 outbox 事件（同一 session/事务）。"""

    async def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        revision: int = 1,
        session: Any = None,
    ) -> None: ...
