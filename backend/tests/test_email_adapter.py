"""M5-3：EML 邮件适配器（头信息/正文/附件 warning、HTML 备选、端到端）。

- 直测：主题→heading；发件人/收件人/日期→text；正文按 text/plain；附件只记 warning；
- HTML-only：用最小分块器提取标题/段落，heading_path 前缀邮件主题；
- 端到端：经默认 pipeline 解析 .eml，IR provenance/来源锚点/覆盖率正确。
"""

from __future__ import annotations

import json
import time
import uuid
from email.message import EmailMessage

from sqlalchemy import select

from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.models import DocumentRow, DocVersionRow
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.parsing.adapters_base import ExecutionContext, SourceHandle
from owli_parser.modules.parsing.builtin_adapters import EmailLocalAdapter
from tests.conftest import create_user_with_tenant


def _eml_bytes(*, html: bool = False) -> bytes:
    message = EmailMessage()
    message["Subject"] = "季度报告"
    message["From"] = "alice@example.com"
    message["To"] = "bob@example.com"
    message["Date"] = "Sat, 17 Oct 2026 10:00:00 +0800"
    if html:
        message.set_content("<html><body><h2>图表</h2><p>HTML 正文</p></body></html>", subtype="html")
    else:
        message.set_content("正文第一段\n\n正文第二段")
        message.add_attachment(
            b"\x00\x01", maintype="application", subtype="octet-stream", filename="data.bin"
        )
    return message.as_bytes()


def _handle(tmp_path, data: bytes, filename: str = "mail.eml") -> SourceHandle:
    path = tmp_path / filename
    path.write_bytes(data)
    return SourceHandle(path=path, mime=None, filename=filename, content_hash="", artifact_id="artifact-1")


def _execution() -> ExecutionContext:
    return ExecutionContext(deadline_epoch=time.time() + 60, cancellation=lambda: False)


def test_email_extracts_headers_body_and_attachment_warning(tmp_path) -> None:
    outcome = EmailLocalAdapter().parse(_handle(tmp_path, _eml_bytes()), {}, _execution())

    blocks = outcome.blocks
    assert blocks[0].type == "heading"
    assert blocks[0].text == "季度报告"
    assert blocks[0].source_ref["part"] == "email:headers"
    meta = next(block for block in blocks if block.text.startswith("发件人:"))
    assert "alice@example.com" in meta.text
    assert "bob@example.com" in meta.text
    body_texts = [block.text for block in blocks if block.source_ref["part"] == "email:body"]
    assert "正文第一段" in body_texts
    assert "正文第二段" in body_texts
    assert any("data.bin" in warning for warning in outcome.warnings)
    assert outcome.total_units == 1
    assert outcome.succeeded_units == 1


def test_email_html_only_body_prefixes_subject(tmp_path) -> None:
    outcome = EmailLocalAdapter().parse(
        _handle(tmp_path, _eml_bytes(html=True), "html.eml"), {}, _execution()
    )

    heading = next(block for block in outcome.blocks if block.text == "图表")
    assert heading.type == "heading"
    assert heading.payload["heading_path"] == ["季度报告", "图表"]
    body = next(block for block in outcome.blocks if block.text == "HTML 正文")
    assert body.payload["heading_path"] == ["季度报告", "图表"]
    assert body.source_ref["part"] == "email:body"


async def _run_email_job(container, username: str, data: bytes):
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-{uuid.uuid4().hex[:8]}", description=None, visibility="tenant"
    )
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="mail.eml", declared_total_bytes=len(data)
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
        filename="mail.eml",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error
    return result


async def test_email_end_to_end_pipeline(container) -> None:
    result = await _run_email_job(container, "email-e2e", _eml_bytes())
    async with container.uow.read_only() as session:
        row = (
            await session.execute(
                select(DocVersionRow.ir_artifact_id, DocumentRow.tenant_id)
                .join(DocumentRow, DocumentRow.id == DocVersionRow.document_id)
                .where(DocVersionRow.id == result.version_id)
            )
        ).one()
    location, profile = await container.artifacts.resolve_location(row[1], row[0])
    reader = container.artifacts.open_reader(location, profile)
    try:
        ir = json.loads(reader.read())
    finally:
        reader.close()

    blocks = [block for block in ir["blocks"] if block.get("text")]
    assert blocks[0]["type"] == "heading"
    assert blocks[0]["text"] == "季度报告"
    assert blocks[0]["provenance"]["adapter_name"] == "email-local"
    body = next(block for block in blocks if block["text"] == "正文第一段")
    assert body["source_refs"][0]["part"] == "email:body"
    coverage = ir.get("coverage") or {}
    assert coverage.get("total_units") == 1
    assert coverage.get("succeeded_units") == 1
