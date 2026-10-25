"""测试基础设施：隔离的 Container、内存盘存储与固定时钟。

- 数据库使用临时 sqlite 文件（schema 由 metadata.create_all 建立；
  生产使用 Alembic 迁移链）。
- 密钥使用测试环境变量；不出现在任何断言输出中。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

# 测试密钥在 import 前设置（Container 构造时解析）
os.environ.setdefault("OWLI_SESSION_PEPPER", "test-session-pepper-0123456789abcdef")
os.environ.setdefault("OWLI_TOKEN_PEPPER", "test-token-pepper-0123456789abcdef")
os.environ.setdefault("OWLI_SOURCE_URL_KEY", "dGVzdC1zb3VyY2UtdXJsLWtleS0wMTIzNDU2Nzg5YWJjZGVm")

from owli_parser.bootstrap.container import Container
from owli_parser.bootstrap.schema import get_metadata
from owli_parser.bootstrap.settings import load_boot_settings
from owli_parser.core.clock import FixedClock


@pytest.fixture()
def fixed_clock() -> FixedClock:
    return FixedClock()


@pytest_asyncio.fixture()
async def container(tmp_path: Path, fixed_clock: FixedClock) -> Container:
    settings = load_boot_settings(
        database={"url": f"sqlite+aiosqlite:///{tmp_path}/test.db"},
        storage={
            "default_profile_ref": "local-primary",
            "local": {"roots": [{"id": "local-primary-root", "path": str(tmp_path / "artifacts")}]},
        },
        network={"mode": "offline", "internal_allowlist": ["files.internal.test:443"]},
        app={"environment": "development"},
        # 测试走 http://test：关闭 Secure 以便 httpx cookie jar 保存会话；生产默认 true
        auth={"session": {"cookie_secure": False}},
    )
    c = Container(settings=settings, clock=fixed_clock)
    # 内存 broker 替身：记录派发而不真正投递
    from owli_parser.ports.queue import TaskDispatcher

    class RecordingDispatcher(TaskDispatcher):
        def __init__(self) -> None:
            self.routes: list[str] = []
            self.runs: list[tuple[str, str]] = []
            self.imports: list[str] = []
            self.index_actions: list[str] = []

        def enqueue_route(self, job_id: str) -> None:
            self.routes.append(job_id)

        def enqueue_run(self, job_id: str, pool: str) -> None:
            self.runs.append((job_id, pool))

        def enqueue_post(self, job_id: str) -> None:
            self.runs.append((job_id, "post"))

        def enqueue_import(self, import_id: str) -> None:
            self.imports.append(import_id)

        def enqueue_index_action(self, action_id: str) -> None:
            self.index_actions.append(action_id)

    c.dispatcher = RecordingDispatcher()  # type: ignore[attr-defined]
    async with c.engine.begin() as conn:
        await conn.run_sync(get_metadata().create_all)
    yield c
    await c.dispose()


async def create_user_with_tenant(
    container: Container,
    *,
    username: str | None = None,
    role: str = "admin",
    tenant_name: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """创建 user/tenant/membership，返回 (user_id, tenant_id, membership_id)。"""
    username = username or f"user-{uuid.uuid4().hex[:8]}"

    user = await container.identity.create_user(
        username=username, password="correct-horse-battery", display_name=username
    )
    tenant_name = tenant_name or f"tenant-{uuid.uuid4().hex[:8]}"
    tenant_id = await container.identity.create_tenant(tenant_name)
    info = await container.identity.add_member(tenant_id, username, role)
    return user.id, tenant_id, info.membership_id


@pytest_asyncio.fixture()
async def seeded(container: Container):
    """两个租户、两个用户的种子数据（多租户隔离测试基础）。"""
    admin_a = await create_user_with_tenant(container, tenant_name="tenant-a")
    admin_b = await create_user_with_tenant(container, tenant_name="tenant-b")
    return {"a": admin_a, "b": admin_b}
