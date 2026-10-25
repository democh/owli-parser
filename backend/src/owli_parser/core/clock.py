"""可注入时钟，统一 UTC aware 时间。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """生产时钟：始终返回 UTC aware datetime。"""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """测试时钟，可手动推进。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FixedClock 只接受 aware datetime")
        self._now = moment


def ensure_utc(moment: datetime) -> datetime:
    """将 naive datetime 视为 UTC 并补齐时区。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
