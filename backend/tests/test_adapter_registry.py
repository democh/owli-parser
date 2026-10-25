"""adapter 注册表接线：外部实例心跳/readiness 与健康聚合。

- 内置 adapter 由进程内注册表提供，恒 ready 且不可被同名外部注册遮蔽；
- 外部实例注册后按心跳时效判定：超时 → unavailable（读时判定，不回写行）；
- 写接口仅平台管理员（注册表为平台级资源，无租户维度）。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from owli_parser.core.errors import ConflictError, InvalidInputError, NotFoundError
from tests.conftest import create_user_with_tenant


async def _client(container) -> AsyncClient:
    app = create_app(container)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _login_platform_admin(container, client: AsyncClient) -> dict[str, str]:
    username = f"root-{uuid.uuid4().hex[:8]}"
    await container.identity.create_user(
        username=username,
        password="correct-horse-battery",
        display_name="root",
        is_platform_admin=True,
    )
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {"X-OWLI-CSRF": response.json()["csrf_token"]}


async def test_register_heartbeat_and_health_aggregation(container) -> None:
    async with await _client(container) as client:
        headers = await _login_platform_admin(container, client)

        listing = await client.get("/v1/adapters", headers=headers)
        assert listing.status_code == 200, listing.text
        builtins = {item["name"]: item for item in listing.json()}
        assert builtins["text-local"]["source"] == "builtin"
        assert builtins["text-local"]["health"] == "ready"
        assert builtins["text-local"]["stale"] is False

        created = await client.put(
            "/v1/adapters/compliance-ocr",
            json={
                "version": "1.2.0",
                "mode": "remote",
                "capabilities": {"formats": ["pdf"], "ocr": True},
                "config_ref": "vault:adapters/compliance-ocr",
                "health": "ready",
            },
            headers=headers,
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["source"] == "registered"
        assert body["reported_health"] == "ready"
        assert body["stale"] is False
        assert body["heartbeat_at"] is not None

        listing = await client.get("/v1/adapters", headers=headers)
        items = {item["name"]: item for item in listing.json()}
        assert items["compliance-ocr"]["capabilities"] == {"formats": ["pdf"], "ocr": True}
        # 清单按名称稳定排序
        assert [item["name"] for item in listing.json()] == sorted(items)

        beat = await client.post(
            "/v1/adapters/compliance-ocr/heartbeat",
            json={"health": "degraded", "version": "1.2.1"},
            headers=headers,
        )
        assert beat.status_code == 200, beat.text
        assert beat.json()["health"] == "degraded"
        assert beat.json()["version"] == "1.2.1"

        health = await client.get("/v1/adapters/health", headers=headers)
        assert health.status_code == 200
        summary = health.json()
        assert summary["status"] == "degraded"
        assert summary["counts"]["degraded"] == 1
        assert summary["counts"]["ready"] >= 1  # 内置实例恒 ready
        assert summary["stale"] == []


async def test_heartbeat_ttl_marks_stale_without_rewriting_row(container, fixed_clock) -> None:
    info = await container.configuration.register_adapter(
        name="legacy-mathtype",
        version="0.9.0",
        mode="remote",
        capabilities={"formats": ["doc"]},
        config_ref=None,
        health="ready",
    )
    assert info.health == "ready"
    assert info.stale is False

    fixed_clock.advance(91)  # 默认 TTL 90s
    rows = await container.configuration.list_adapters()
    stale = next(row for row in rows if row.name == "legacy-mathtype")
    assert stale.health == "unavailable"
    assert stale.stale is True
    # 最近一次上报值保留，便于排查
    assert stale.reported_health == "ready"

    async with await _client(container) as client:
        headers = await _login_platform_admin(container, client)
        summary = (await client.get("/v1/adapters/health", headers=headers)).json()
        assert summary["status"] == "unavailable"
        assert summary["stale"] == ["legacy-mathtype"]

        # 重新心跳恢复 ready
        beat = await client.post("/v1/adapters/legacy-mathtype/heartbeat", json={}, headers=headers)
        assert beat.status_code == 200, beat.text
        assert beat.json()["health"] == "ready"
        summary = (await client.get("/v1/adapters/health", headers=headers)).json()
        assert summary["status"] == "ok"


async def test_registry_rejects_builtin_shadow_and_unknown_heartbeat(container) -> None:
    with pytest.raises(ConflictError):
        await container.configuration.register_adapter(
            name="text-local",
            version="9.9.9",
            mode="remote",
            capabilities={},
            config_ref=None,
            health="ready",
        )
    with pytest.raises(NotFoundError):
        await container.configuration.heartbeat_adapter(name="ghost-adapter", health="ready")
    with pytest.raises(InvalidInputError):
        await container.configuration.register_adapter(
            name="Bad Name",
            version="1.0.0",
            mode="remote",
            capabilities={},
            config_ref=None,
            health="ready",
        )


async def test_write_endpoints_require_platform_admin(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    user = await container.identity.get_user(user_id)
    async with await _client(container) as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": user.username, "password": "correct-horse-battery"},
        )
        assert login.status_code == 200, login.text
        headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": str(tenant_id),
        }
        denied = await client.put("/v1/adapters/tenant-owned", json={"version": "1.0.0"}, headers=headers)
        assert denied.status_code == 403, denied.text

        # 平台管理员：未注册心跳 404；非法 mode 422
        root = await _login_platform_admin(container, client)
        missing = await client.post("/v1/adapters/unregistered/heartbeat", json={}, headers=root)
        assert missing.status_code == 404, missing.text
        bad_mode = await client.put(
            "/v1/adapters/bad-mode", json={"version": "1.0.0", "mode": "cloud"}, headers=root
        )
        assert bad_mode.status_code == 422, bad_mode.text
