"""SQLAlchemy 2 基础设施：Base、engine、session factory。

- 权威存储为 PostgreSQL；本地开发允许 sqlite+aiosqlite（非生产）。
- ORM 模型按模块定义，共用本模块的 Base。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类。"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def create_async_engine_from_url(
    url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 20,
    pool_timeout_seconds: int = 30,
    pool_recycle_seconds: int = 1800,
) -> AsyncEngine:
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "echo": False}
    if url.startswith("sqlite"):
        kwargs.pop("pool_pre_ping", None)
    else:
        kwargs.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout_seconds,
            pool_recycle=pool_recycle_seconds,
        )
    engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_fk(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
