"""预览 descriptor 测试：kind 推断、受权资源路径、页几何（含不可用时的降级）。"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant


async def _client(container) -> AsyncClient:
    app = create_app(container)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _setup_document(container, filename: str, body: bytes):
    """上传 → 提交解析（不执行 worker）：返回 (scope, tenant_id, document_id, version_id)。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container)
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-preview-{filename}", description=None, visibility="tenant"
    )
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename=filename, declared_total_bytes=len(body)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([body]))
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
    return scope, tenant_id, result.document_id, result.version_id


async def _login_headers(container, client: AsyncClient, tenant_id) -> dict[str, str]:
    from sqlalchemy import select

    from owli_parser.modules.identity.models import MembershipRow

    async with container.uow.read_only() as session:
        membership = (
            (await session.execute(select(MembershipRow).where(MembershipRow.tenant_id == tenant_id)))
            .scalars()
            .first()
        )
    user = await container.identity.get_user(membership.user_id)
    response = await client.post(
        "/v1/auth/login", json={"username": user.username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {
        "X-OWLI-CSRF": response.json()["csrf_token"],
        "X-Tenant-ID": str(tenant_id),
    }


async def test_preview_text_descriptor(container) -> None:
    scope, tenant_id, document_id, version_id = await _setup_document(
        container, "readme.txt", b"hello preview world\n"
    )
    del scope
    async with await _client(container) as client:
        headers = await _login_headers(container, client, tenant_id)
        response = await client.get(f"/v1/documents/{document_id}/preview", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["kind"] == "text"
        assert payload["mime"] == "text/plain"
        assert payload["status"] == "ready"
        assert payload["version_id"] == str(version_id)
        # 推荐 page_region（PDF）不返回；文本无页几何
        assert payload["pages"] == []
        # content_path 为受权资源，必须携带文档/版本关联（非"知道 ID 即可下载"）
        assert "/v1/artifacts/" in payload["content_path"]
        assert f"document_id={document_id}" in payload["content_path"]
        assert f"version_id={version_id}" in payload["content_path"]

        # descriptor 指向的资源确实可按 content_path 流式读取
        content = await client.get(payload["content_path"], headers=headers)
        assert content.status_code == 200
        assert content.content == b"hello preview world\n"
        assert content.headers["cache-control"] == "private, no-store"


async def test_preview_image_kind(container) -> None:
    png_head = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    _, tenant_id, document_id, _ = await _setup_document(container, "scan.png", png_head)
    async with await _client(container) as client:
        headers = await _login_headers(container, client, tenant_id)
        response = await client.get(f"/v1/documents/{document_id}/preview", headers=headers)
        assert response.status_code == 200
        assert response.json()["kind"] == "image"
        assert response.json()["mime"] == "image/png"


async def test_preview_unsupported_and_pdf_degrade(container) -> None:
    # 无 magic、含 NUL：MIME 无法判定 → unsupported（前端走 L4 兜底信息卡）
    binary_body = b"\x00\x01\x02\x03" + bytes(range(64))
    _, tenant_id, document_id, version_id = await _setup_document(container, "blob.bin", binary_body)
    async with await _client(container) as client:
        headers = await _login_headers(container, client, tenant_id)
        response = await client.get(
            f"/v1/documents/{document_id}/preview?version_id={version_id}", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "unsupported"

    # 损坏 PDF：kind 仍为 pdf；页几何不可用时降级为空列表而不报错
    broken_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
    _, tenant_id2, document_id2, version_id2 = await _setup_document(container, "broken.pdf", broken_pdf)
    async with await _client(container) as client:
        headers = await _login_headers(container, client, tenant_id2)
        response = await client.get(
            f"/v1/documents/{document_id2}/preview?version_id={version_id2}", headers=headers
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "pdf"
        assert payload["pages"] == []


async def test_preview_requires_authorized_document(container) -> None:
    """跨租户读取预览 descriptor 一律 404（不泄露存在性）。"""
    _, tenant_id, document_id, _ = await _setup_document(container, "secret.txt", b"top secret\n")
    _, other_tenant, _ = await create_user_with_tenant(container)
    async with await _client(container) as client:
        headers = await _login_headers(container, client, other_tenant)
        response = await client.get(f"/v1/documents/{document_id}/preview", headers=headers)
        assert response.status_code == 404
    del tenant_id
