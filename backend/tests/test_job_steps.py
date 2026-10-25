"""L-4：job steps 生产端 + 查询 API。

- 解析完成后按页（PDF）或文档（text）写入 job_steps（每 attempt 一套）；
- GET /v1/parsing/jobs/{id}/steps 返回单位级状态/checkpoint，支持 stage 过滤与有界 limit；
- 跨租户 404；应用主体只能看自己的任务（规则与 get job 一致）。
"""

from __future__ import annotations

import io
import uuid

from httpx import ASGITransport, AsyncClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant

_PASSWORD = "correct-horse-battery"


def _pdf_bytes() -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setFont("Helvetica", 12)
    pdf.drawString(72, 720, "PAGE-ONE job steps sample content")
    pdf.showPage()
    pdf.drawString(72, 720, "PAGE-TWO job steps sample content")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _scope(user_id: uuid.UUID, tenant_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


async def _run_job(container, scope: CatalogScope, filename: str, data: bytes):
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-{uuid.uuid4().hex[:8]}", description=None, visibility="tenant"
    )
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename=filename, declared_total_bytes=len(data)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([data]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref=None,
        document_id=None,
        expected_revision=None,
        filename=filename,
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
    job = await container.jobs.get_job(scope.tenant_id, result.job_id)
    assert job.state == "DONE", job.error
    return result.job_id


async def _login(client: AsyncClient, username: str, tenant_id: uuid.UUID) -> dict[str, str]:
    login = await client.post("/v1/auth/login", json={"username": username, "password": _PASSWORD})
    return {"X-OWLI-CSRF": login.json()["csrf_token"], "X-Tenant-ID": str(tenant_id)}


async def test_job_steps_recorded_per_page_and_queryable(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="steps-pdf")
    job_id = await _run_job(container, _scope(user_id, tenant_id), "steps.pdf", _pdf_bytes())
    app = create_app(container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, "steps-pdf", tenant_id)
        response = await client.get(f"/v1/parsing/jobs/{job_id}/steps", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        assert body["limit"] == 1000
        items = body["items"]
        assert [item["unit_id"] for item in items] == ["page:0", "page:1"]
        for item in items:
            assert item["stage"] == "PARSING"
            assert item["state"] == "succeeded"
            assert item["attempt"] == 1
            assert item["checkpoint_ref"]["engine"] == "pypdf"
            assert item["error_code"] is None
            assert item["finished_at"] is not None

        filtered = await client.get(f"/v1/parsing/jobs/{job_id}/steps?stage=CHUNKING", headers=headers)
        assert filtered.status_code == 200
        assert filtered.json() == {"items": [], "total": 0, "limit": 1000}

        limited = await client.get(f"/v1/parsing/jobs/{job_id}/steps?limit=1", headers=headers)
        assert limited.status_code == 200
        assert limited.json()["total"] == 2
        assert len(limited.json()["items"]) == 1

    # 跨租户 404（不泄露任务存在性）
    await create_user_with_tenant(container, username="steps-other")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        login = await other.post("/v1/auth/login", json={"username": "steps-other", "password": _PASSWORD})
        tenants = (await other.get("/v1/auth/tenants")).json()
        other_headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": tenants[0]["tenant_id"],
        }
        denied = await other.get(f"/v1/parsing/jobs/{job_id}/steps", headers=other_headers)
        assert denied.status_code == 404


async def test_job_steps_document_unit_for_text(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="steps-text")
    job_id = await _run_job(container, _scope(user_id, tenant_id), "steps.txt", "第一段\n\n第二段".encode())
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, "steps-text", tenant_id)
        response = await client.get(f"/v1/parsing/jobs/{job_id}/steps", headers=headers)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["unit_id"] for item in items] == ["document"]
        assert items[0]["checkpoint_ref"]["engine"] == "builtin-text"
