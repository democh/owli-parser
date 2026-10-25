"""M5-3：EPUB 适配器（OPF spine 顺序、章节标题层级、结构失败语义）。

- 直测：按 spine 抽取 XHTML 标题/段落；inline 标签不切块；来源锚点 part=epub:<entry>；
- 容错：缺 OPF → invalid_epub unit_error；空 spine → invalid_epub 失败；
- 端到端：经默认 pipeline 解析 .epub，IR 顺序/provenance/覆盖率正确。
"""

from __future__ import annotations

import io
import json
import time
import uuid
import zipfile

import pytest
from sqlalchemy import select

from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.models import DocumentRow, DocVersionRow
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.parsing.adapters_base import ExecutionContext, ParseFailedError, SourceHandle
from owli_parser.modules.parsing.builtin_adapters import EpubLocalAdapter
from tests.conftest import create_user_with_tenant

_CONTAINER = (
    '<?xml version="1.0"?>'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _opf(chapter_count: int, *, spine: bool = True) -> str:
    items = "".join(
        f'<item id="c{index}" href="chapter{index}.xhtml" media-type="application/xhtml+xml"/>'
        for index in range(chapter_count)
    )
    spine_xml = "".join(f'<itemref idref="c{index}"/>' for index in range(chapter_count)) if spine else ""
    return (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f"<manifest>{items}</manifest><spine>{spine_xml}</spine></package>"
    )


def _chapter(title: str, body: str) -> str:
    return f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>{title}</h1><p>{body}</p></body></html>'


def _epub_bytes(chapters: list[tuple[str, str]], *, spine: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", _CONTAINER)
        archive.writestr("OEBPS/content.opf", _opf(len(chapters), spine=spine))
        for index, (title, body) in enumerate(chapters):
            archive.writestr(f"OEBPS/chapter{index}.xhtml", _chapter(title, body))
    return buffer.getvalue()


def _handle(tmp_path, data: bytes, filename: str = "book.epub") -> SourceHandle:
    path = tmp_path / filename
    path.write_bytes(data)
    return SourceHandle(path=path, mime=None, filename=filename, content_hash="", artifact_id="artifact-1")


def _execution() -> ExecutionContext:
    return ExecutionContext(deadline_epoch=time.time() + 60, cancellation=lambda: False)


def test_epub_extracts_spine_chapters_in_order(tmp_path) -> None:
    data = _epub_bytes(
        [
            ("第一章", "第一章正文"),
            ("第二章", "带 <em>强调</em> 的第二章正文"),
        ]
    )
    outcome = EpubLocalAdapter().parse(_handle(tmp_path, data), {}, _execution())

    blocks = outcome.blocks
    assert [block.type for block in blocks] == ["heading", "text", "heading", "text"]
    assert [block.text for block in blocks] == [
        "第一章",
        "第一章正文",
        "第二章",
        "带 强调 的第二章正文",
    ]
    assert blocks[3].payload["heading_path"] == ["第二章"]
    assert [block.source_ref["part"] for block in blocks] == [
        "epub:OEBPS/chapter0.xhtml",
        "epub:OEBPS/chapter0.xhtml",
        "epub:OEBPS/chapter1.xhtml",
        "epub:OEBPS/chapter1.xhtml",
    ]
    assert blocks[0].source_ref["path"] == "OEBPS/chapter0.xhtml"
    assert outcome.total_units == 2
    assert outcome.succeeded_units == 2


def test_epub_invalid_structure_fails_explicitly(tmp_path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
    missing = EpubLocalAdapter().parse(_handle(tmp_path, buffer.getvalue(), "broken.epub"), {}, _execution())
    assert "invalid_epub" in {error.code for error in missing.unit_errors}
    assert missing.succeeded_units == 0

    with pytest.raises(ParseFailedError) as excinfo:
        EpubLocalAdapter().parse(
            _handle(tmp_path, _epub_bytes([("章节", "正文")], spine=False), "nospine.epub"),
            {},
            _execution(),
        )
    assert excinfo.value.code == "invalid_epub"


async def _run_epub_job(container, username: str, data: bytes):
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
        scope, dataset_id=dataset.id, filename="book.epub", declared_total_bytes=len(data)
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
        filename="book.epub",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error
    return result


async def test_epub_end_to_end_pipeline(container) -> None:
    data = _epub_bytes([("第一章", "第一章正文"), ("第二章", "第二章正文")])
    result = await _run_epub_job(container, "epub-e2e", data)
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
    assert [block["type"] for block in blocks] == ["heading", "text", "heading", "text"]
    assert blocks[0]["text"] == "第一章"
    assert blocks[0]["provenance"]["adapter_name"] == "epub-local"
    assert blocks[1]["source_refs"][0]["part"] == "epub:OEBPS/chapter0.xhtml"
    coverage = ir.get("coverage") or {}
    assert coverage.get("total_units") == 2
    assert coverage.get("succeeded_units") == 2
