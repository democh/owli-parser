"""L-5：回归运行详情（cases 明细）与租户隔离。

- 详情返回逐 case 指标（不含被检索正文），列表接口保持精简不变；
- 租户黄金集的运行跨租户 404；CLI 全局集（tenant_id=None）对所有租户可见；
- 未知 run_id 404。
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from tests.conftest import create_user_with_tenant

_PASSWORD = "correct-horse-battery"


async def _login(client: AsyncClient, username: str, tenant_id: uuid.UUID) -> dict[str, str]:
    login = await client.post("/v1/auth/login", json={"username": username, "password": _PASSWORD})
    return {"X-OWLI-CSRF": login.json()["csrf_token"], "X-Tenant-ID": str(tenant_id)}


async def test_regression_run_detail_returns_cases_and_scopes_tenant(container) -> None:
    _, tenant_id, _ = await create_user_with_tenant(container, username="quality-detail")
    golden = await container.knowledge.upsert_golden_set(
        name=f"gs-{uuid.uuid4().hex[:8]}",
        cases=[
            {
                "reference_version_id": str(uuid.uuid4()),
                "hypothesis_version_id": str(uuid.uuid4()),
                "groups": {"format": "pdf"},
            }
        ],
        expectations={"max_cer": 0.05},
        tenant_id=tenant_id,
    )
    case_result = {
        "reference_version_id": str(uuid.uuid4()),
        "hypothesis_version_id": str(uuid.uuid4()),
        "groups": {"format": "pdf"},
        "metrics": {"text_cer": 0.01, "reading_order": 0.99},
    }
    run = await container.knowledge.create_golden_run(
        golden_set_id=golden.id,
        adapter_ref="parsing-compare",
        config_digest="d" * 64,
        metrics={
            "passed": True,
            "violations": [],
            "aggregate": {"text_cer": 0.01, "reading_order": 0.99},
            "cases": [case_result],
        },
        state="passed",
    )
    app = create_app(container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, "quality-detail", tenant_id)
        response = await client.get(f"/v1/quality/regression-runs/{run.run_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["run_id"] == run.run_id
        assert body["golden_set_name"] == golden.name
        assert body["passed"] is True
        assert body["aggregate"] == {"text_cer": 0.01, "reading_order": 0.99}
        assert body["cases"] == [case_result]

        missing = await client.get(f"/v1/quality/regression-runs/{uuid.uuid4().hex}", headers=headers)
        assert missing.status_code == 404

    # 另一租户不能读取该运行（不泄露存在性）
    await create_user_with_tenant(container, username="quality-other")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        login = await other.post("/v1/auth/login", json={"username": "quality-other", "password": _PASSWORD})
        tenants = (await other.get("/v1/auth/tenants")).json()
        other_headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": tenants[0]["tenant_id"],
        }
        denied = await other.get(f"/v1/quality/regression-runs/{run.run_id}", headers=other_headers)
        assert denied.status_code == 404


async def test_global_cli_golden_run_is_visible_to_tenants(container) -> None:
    _, tenant_id, _ = await create_user_with_tenant(container, username="quality-global")
    global_set = await container.knowledge.upsert_golden_set(
        name=f"global-{uuid.uuid4().hex[:8]}", cases=[], expectations={}
    )
    run = await container.knowledge.create_golden_run(
        golden_set_id=global_set.id,
        adapter_ref="parsing-compare",
        config_digest=None,
        metrics={"passed": True, "violations": [], "aggregate": {}, "cases": []},
        state="passed",
    )
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, "quality-global", tenant_id)
        response = await client.get(f"/v1/quality/regression-runs/{run.run_id}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["cases"] == []
