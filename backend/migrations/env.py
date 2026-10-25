"""Alembic 迁移环境。

- 数据库 URL 从应用集中配置读取（load_boot_settings），不写死在 alembic.ini。
- 迁移使用同步引擎执行（asyncpg 连接串转换为 psycopg 不必要；直接用
  run_sync 方式在同步上下文执行）。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from owli_parser.bootstrap.schema import get_metadata

config = context.config

if config.config_file_name is not None:
    # 迁移可能在同一进程/测试进程内执行，不能顺带禁用应用已有 logger。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = get_metadata()


def _database_url() -> str:
    import os
    import sys

    # 允许通过环境变量直接覆盖（CI/测试）；否则读取应用配置。
    url = os.environ.get("OWLI_DATABASE__URL")
    if url:
        return url
    backend_dir = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(backend_dir / "src"))
    from owli_parser.bootstrap.settings import load_boot_settings

    return load_boot_settings().database.url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
