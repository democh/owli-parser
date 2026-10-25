"""工作单元（UnitOfWork）。

- 跨模块事务由 workflows 共享一个 UoW；网络 IO 在事务外。
- 长事务不包住下载/推理；成功副作用通过 outbox 登记。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class UnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        """打开一个事务性会话；异常回滚，正常退出提交。"""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def read_only(self) -> AsyncIterator[AsyncSession]:
        """只读会话（不显式提交）。"""
        async with self._session_factory() as session:
            yield session
