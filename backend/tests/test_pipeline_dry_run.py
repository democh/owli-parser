"""M5-1：pipeline 草稿校验与 dry-run 路由解释（不执行 adapter、不计模型费用）。

覆盖：
- validate：合法草稿 ok + digest；未知 adapter / name 不一致 → ok=false 且 error 可读；
- dry-run：artifact 样本（text）与已入库版本（pdf）两条来源，返回 format/features/route；
- 无格式分支 → 200 + ok=false + 逐候选原因（不抛 422）；
- 来源参数二选一、artifact 需 filename、跨租户版本 404。
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

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
    pdf.setFont("Helvetica", 14)
    pdf.drawString(72, 720, "PAGE-ONE dry-run sample content for routing")
    pdf.showPage()
    pdf.drawString(72, 720, "PAGE-TWO dry-run sample content for routing")
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


@asynccontextmanager
async def _admin_client(
    container: Any, username: str
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, AsyncClient, dict[str, str]]]:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/v1/auth/login", json={"username": username, "password": _PASSWORD})
        headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": str(tenant_id),
        }
        yield user_id, tenant_id, client, headers


async def _upload(container: Any, scope: CatalogScope, filename: str, data: bytes):
    dataset = await container.catalog.create_dataset(
        scope,
        name=f"ds-{uuid.uuid4().hex[:8]}",
        description=None,
        visibility="tenant",
    )
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename=filename, declared_total_bytes=len(data)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([data]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    return dataset, artifact_id


async def _submit_version(container: Any, scope: CatalogScope, filename: str, data: bytes) -> uuid.UUID:
    dataset, artifact_id = await _upload(container, scope, filename, data)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref=None,
        document_id=None,
        expected_revision=None,
        filename=filename,
    )
    job = await container.jobs.get_job(scope.tenant_id, result.job_id)
    return job.version_id


async def test_validate_pipeline_draft(container) -> None:
    async with _admin_client(container, "dryrun-validate") as (_, _, client, headers):
        default_yaml = container.submit_parsing.compile_default_pipeline().yaml_text
        ok = await client.post(
            "/v1/pipelines/default/validate", json={"yaml_text": default_yaml}, headers=headers
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["ok"] is True and body["errors"] == []
        assert body["digest"]
        assert body["execution_plan"]["pipeline_name"] == "default"

        unknown = default_yaml.replace("pdf-text-local", "missing-adapter")
        failed = await client.post(
            "/v1/pipelines/default/validate", json={"yaml_text": unknown}, headers=headers
        )
        assert failed.status_code == 200
        assert failed.json()["ok"] is False
        assert any("未注册 adapter" in item for item in failed.json()["errors"])

        renamed = default_yaml.replace("name: default", "name: renamed", 1)
        mismatch = await client.post(
            "/v1/pipelines/default/validate", json={"yaml_text": renamed}, headers=headers
        )
        assert mismatch.json()["ok"] is False
        assert any("不一致" in item for item in mismatch.json()["errors"])


async def test_dry_run_text_artifact_with_draft(container) -> None:
    async with _admin_client(container, "dryrun-text") as (user_id, tenant_id, client, headers):
        scope = _scope(user_id, tenant_id)
        default_yaml = container.submit_parsing.compile_default_pipeline().yaml_text
        _, artifact_id = await _upload(container, scope, "sample.txt", "第一段\n\n第二段".encode())

        response = await client.post(
            "/v1/pipelines/default/dry-run",
            json={
                "yaml_text": default_yaml,
                "artifact_id": str(artifact_id),
                "filename": "sample.txt",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["format"] == "text"
        assert body["route"]["adapter"] == "text-local"
        assert body["route"]["skipped"] == []
        assert body["pipeline"]["source"] == "draft"
        assert body["features"]["extension"] == "txt"
        # dry-run 不落盘：草稿不会成为 tenant active 配置
        assert await container.configuration.get_active_pipeline(tenant_id, "default") is None


async def test_dry_run_pdf_version(container) -> None:
    async with _admin_client(container, "dryrun-pdf") as (user_id, tenant_id, client, headers):
        scope = _scope(user_id, tenant_id)
        version_id = await _submit_version(container, scope, "sample.pdf", _pdf_bytes())

        response = await client.post(
            "/v1/pipelines/default/dry-run",
            json={"version_id": str(version_id)},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["format"] == "pdf"
        assert body["route"]["adapter"] == "pdf-text-local"
        assert body["route"]["reason"]
        assert body["pipeline"]["source"] == "builtin"
        assert body["features"]["page_count"] == 2
        assert body["features"]["text_layer_ratio"] == 1.0
        assert body["warnings"] == []


async def test_dry_run_unknown_format_returns_explanation(container) -> None:
    async with _admin_client(container, "dryrun-unknown") as (user_id, tenant_id, client, headers):
        scope = _scope(user_id, tenant_id)
        _, artifact_id = await _upload(container, scope, "sample.bin", b"\x00\x01\x02\x03")

        response = await client.post(
            "/v1/pipelines/default/dry-run",
            json={"artifact_id": str(artifact_id), "filename": "sample.bin"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        assert body["route"]["adapter"] is None
        assert "没有针对格式" in body["route"]["reason"]
        assert any("未定义格式" in warning for warning in body["warnings"])


async def test_dry_run_source_argument_validation(container) -> None:
    async with _admin_client(container, "dryrun-args") as (user_id, tenant_id, client, headers):
        scope = _scope(user_id, tenant_id)
        _, artifact_id = await _upload(container, scope, "sample.txt", b"dry-run")

        neither = await client.post("/v1/pipelines/default/dry-run", json={}, headers=headers)
        assert neither.status_code == 422

        both = await client.post(
            "/v1/pipelines/default/dry-run",
            json={
                "version_id": str(uuid.uuid4()),
                "artifact_id": str(artifact_id),
                "filename": "sample.txt",
            },
            headers=headers,
        )
        assert both.status_code == 422

        missing_filename = await client.post(
            "/v1/pipelines/default/dry-run",
            json={"artifact_id": str(artifact_id)},
            headers=headers,
        )
        assert missing_filename.status_code == 422
        assert missing_filename.json()["code"] == "validation_failed"


async def test_dry_run_version_is_tenant_scoped(container) -> None:
    async with _admin_client(container, "dryrun-owner") as (owner_id, owner_tenant, _, _):
        version_id = await _submit_version(
            container, _scope(owner_id, owner_tenant), "scoped.pdf", _pdf_bytes()
        )
    async with _admin_client(container, "dryrun-other") as (_, _, client, headers):
        response = await client.post(
            "/v1/pipelines/default/dry-run",
            json={"version_id": str(version_id)},
            headers=headers,
        )
        assert response.status_code == 404
