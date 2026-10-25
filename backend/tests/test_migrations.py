"""迁移链可执行性：从空库执行到 head，并与当前 ORM 元数据对比表集合。

常规业务测试使用 metadata.create_all 加速，但不覆盖 Alembic 历史脚本；
本测试独立执行真实迁移链，拦截“模型已改、迁移漏写”以及迁移脚本损坏。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from owli_parser.bootstrap.schema import get_metadata

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_migration_upgrade_head_builds_current_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    env = os.environ.copy()
    env["OWLI_DATABASE__URL"] = f"sqlite+aiosqlite:///{database_path}"
    owli = Path(sys.executable).with_name("owli")
    assert owli.is_file()

    # 走真实 CLI 子进程：同时验证入口、迁移日志配置与资源生命周期，
    # 避免 Alembic 的 fileConfig/asyncio.run 污染当前测试进程。
    subprocess.run(
        [str(owli), "db", "upgrade"],
        cwd=_BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))

    with sqlite3.connect(database_path) as connection:
        actual_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_")
        }
        revision_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    expected_head = ScriptDirectory.from_config(config).get_current_head()
    assert revision_row == (expected_head,)
    assert actual_tables - {"alembic_version"} == set(get_metadata().tables)
