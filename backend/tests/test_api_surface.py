"""API surface 测试：真实 ASGI 请求（httpx ASGITransport，与 fixture 同一事件循环）。

覆盖：multipart 直传、metrics、应用 Token 端到端（scope/grant/撤销/调用记录）、
ws-ticket 签发与拒绝、复核队列处置、能力未就绪的明确响应。
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant


async def _client(container) -> AsyncClient:
    app = create_app(container)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"X-OWLI-CSRF": body["csrf_token"]}


async def test_direct_upload_creates_artifact(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    user = await container.identity.get_user(user_id)
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name="ds-direct", description=None, visibility="tenant"
    )
    async with await _client(container) as client:
        headers = await _login(client, user.username)
        headers["X-Tenant-ID"] = str(tenant_id)
        response = await client.post(
            "/v1/uploads/direct",
            data={"dataset_id": str(dataset.id)},
            files={"file": ("note.txt", b"direct upload body", "text/plain")},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        artifact_id = uuid.UUID(response.json()["artifact_id"])
        # 直传与分片路径产出同一类 artifact（SHA-256 服务端计算）
        artifact = await container.artifacts.get_artifact(tenant_id, artifact_id)
        assert artifact.kind == "source"
        assert artifact.size_bytes == len(b"direct upload body")

        # 超限：临时下调 direct 阈值，验证有界读取拒绝（不一次性读满内存）
        container.settings.upload.direct_max_bytes = 1024
        big = b"x" * 4096
        too_big = await client.post(
            "/v1/uploads/direct",
            data={"dataset_id": str(dataset.id)},
            files={"file": ("big.bin", big, "application/octet-stream")},
            headers=headers,
        )
        assert too_big.status_code == 413

        listing = await client.get("/v1/uploads", headers=headers)
        assert listing.status_code == 200
        assert "items" in listing.json()


async def test_metrics_endpoint(container) -> None:
    async with await _client(container) as client:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "owli_http_requests_total" in response.text


async def test_ws_ticket_issue_and_reject(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    user = await container.identity.get_user(user_id)
    async with await _client(container) as client:
        headers = await _login(client, user.username)
        headers["X-Tenant-ID"] = str(tenant_id)
        response = await client.post(
            "/v1/auth/ws-ticket", json={"tenant_id": str(tenant_id)}, headers=headers
        )
        assert response.status_code == 200, response.text
        ticket = response.json()["ticket"]
        # 票据一次性：业务层直接校验两次，第二次必须拒绝
        claims = container.identity.verify_ws_ticket(ticket)
        assert claims.tenant_id == tenant_id
        from owli_parser.core.errors import NotAuthenticatedError

        with pytest.raises(NotAuthenticatedError):
            container.identity.verify_ws_ticket(ticket)

        # 未加入的租户不允许签票
        other_user_id, other_tenant_id, _ = await create_user_with_tenant(container)
        forbidden = await client.post(
            "/v1/auth/ws-ticket",
            json={"tenant_id": str(other_tenant_id)},
            headers={"X-Tenant-ID": str(other_tenant_id), **headers},
        )
        assert forbidden.status_code == 403
        del other_user_id


async def test_application_token_end_to_end(container) -> None:
    """应用 Token：scope + grant 放行；撤销后立即 401；调用记录已脱敏落库。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    allowed_ds = await container.catalog.create_dataset(
        scope, name="ds-allowed", description=None, visibility="tenant"
    )
    hidden_ds = await container.catalog.create_dataset(
        scope, name="ds-hidden", description=None, visibility="tenant"
    )
    app_profile = await container.access.create_application(
        tenant_id,
        name="integration-app",
        description=None,
        allowed_scopes=["datasets:read", "documents:read"],
        created_by_id=user_id,
    )
    from owli_parser.modules.access.service import GrantEntry

    await container.access.replace_grants(
        tenant_id,
        app_profile.id,
        [GrantEntry(dataset_id=allowed_ds.id, actions=["read"])],
    )
    issued = await container.access.issue_token(
        tenant_id,
        app_profile.id,
        name="t1",
        scopes=["datasets:read"],
        dataset_ids=[str(allowed_ds.id)],
        expires_in_seconds=3600,
        created_by_id=user_id,
    )
    async with await _client(container) as client:
        headers = {"Authorization": f"Bearer {issued.raw_token}"}
        listing = await client.get("/v1/datasets", headers=headers)
        assert listing.status_code == 200, listing.text
        ids = {item["id"] for item in listing.json()["items"]}
        assert str(allowed_ds.id) in ids
        assert str(hidden_ds.id) not in ids  # 未授权数据集不可见

        # X-Tenant-ID 与 Token 绑定租户不一致 → 403
        mismatch = await client.get("/v1/datasets", headers={**headers, "X-Tenant-ID": str(uuid.uuid4())})
        assert mismatch.status_code == 403

        # 缺 scope：以相同 Token 调 tasks 列表需要 jobs:read
        jobs = await client.get("/v1/parsing/jobs", headers=headers)
        assert jobs.status_code == 403
        assert jobs.json()["code"] == "scope_required"

        # 撤销后立即拒绝（权威库生效，无需过期窗口）
        await container.access.revoke_token(tenant_id, app_profile.id, issued.token_id)
        revoked = await client.get("/v1/datasets", headers=headers)
        assert revoked.status_code == 401

        # 调用记录：脱敏聚合存在且不含 Authorization 内容
        usage = await client_get_usage(container, tenant_id, app_profile.id)
        assert usage, "应有调用记录"
        assert all("Bearer" not in json.dumps(row) for row in usage)


async def client_get_usage(container, tenant_id, application_id) -> list[dict]:
    from sqlalchemy import select

    from owli_parser.modules.access.models import ApplicationUsageRow

    async with container.uow.read_only() as session:
        rows = (
            (
                await session.execute(
                    select(ApplicationUsageRow).where(
                        ApplicationUsageRow.tenant_id == tenant_id,
                        ApplicationUsageRow.application_id == application_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "operation": r.operation,
                "method": r.method,
                "path": r.path,
                "status_code": r.status_code,
                "decision": r.decision,
            }
            for r in rows
        ]


async def test_review_queue_resolve(container) -> None:
    from owli_parser.modules.knowledge.models import ReviewItemRow

    user_id, tenant_id, _ = await create_user_with_tenant(container)
    version_id = uuid.uuid4()
    async with container.uow.begin() as session:
        session.add(
            ReviewItemRow(
                tenant_id=tenant_id,
                version_id=version_id,
                job_id=None,
                block_id="b-0001",
                reason="unit_error",
                status="open",
            )
        )
    user = await container.identity.get_user(user_id)
    async with await _client(container) as client:
        headers = await _login(client, user.username)
        headers["X-Tenant-ID"] = str(tenant_id)
        queue = await client.get("/v1/review/queue?status=open", headers=headers)
        assert queue.status_code == 200, queue.text
        items = queue.json()["items"]
        assert len(items) == 1
        item_id = items[0]["id"]
        assert items[0]["reason"] == "unit_error"

        resolved = await client.post(
            f"/v1/review/queue/{item_id}/resolve",
            json={"action": "accept", "note": "已人工确认"},
            headers=headers,
        )
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "resolved"

        # 重复处置 → 409（不静默覆盖他人结论）
        again = await client.post(
            f"/v1/review/queue/{item_id}/resolve",
            json={"action": "dismiss"},
            headers=headers,
        )
        assert again.status_code == 409


async def test_capability_not_ready_responses(container) -> None:
    """检索/索引能力未启用时返回明确 503 capability_not_ready，不伪造成功。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    user = await container.identity.get_user(user_id)
    async with await _client(container) as client:
        headers = await _login(client, user.username)
        headers["X-Tenant-ID"] = str(tenant_id)
        retrieval = await client.post(
            "/v1/retrieval/test",
            json={"query": "hello", "dataset_ids": [str(uuid.uuid4())]},
            headers=headers,
        )
        assert retrieval.status_code == 503
        assert retrieval.json()["code"] == "capability_not_ready"

        reembed = await client.post(f"/v1/chunks/{uuid.uuid4()}/reembed", headers=headers)
        assert reembed.status_code == 503

        capabilities = await client.get("/v1/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.json()["retrieval_enabled"] is False
