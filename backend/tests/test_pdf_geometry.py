"""来源矩阵：旋转/裁剪 PDF 的精确锚点 fixture（reportlab 生成真实文字层）。

验证矩阵要求"旋转/裁剪 PDF 不引用错误页"。用真实 PDF（reportlab 写文字层 +
pypdf 做旋转/裁剪变换）走完整解析管线，断言：
- 文本不丢失、不串页（page_index 归属正确）；
- 页几何（rotation）正确进入 IR（PageGeometry.rotation）；
- 裁剪页文本提取与页归属不受裁剪影响；
- 全程无 unit_error、覆盖率完备。
"""

from __future__ import annotations

import io

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant


def _make_two_page_pdf() -> bytes:
    """生成两页 PDF：每页含可识别文本（真实文字层）。"""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setFont("Helvetica", 14)
    pdf.drawString(72, 720, "PAGE-ONE-ALPHA section one content")
    pdf.showPage()
    pdf.drawString(72, 720, "PAGE-TWO-BETA section two content")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _make_cross_page_table_pdf() -> bytes:
    """跨页表格：页 1 表头 + 两行，页 2 续行（无表头重复）。"""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setFont("Helvetica", 10)
    rows_p1 = [
        "TABLE-HEADER Region Revenue",
        "ROW-P1-1 North 100",
        "ROW-P1-2 East 120",
    ]
    rows_p2 = [
        "ROW-P2-1 South 140",
        "ROW-P2-2 West 160",
    ]
    for index, line in enumerate(rows_p1):
        pdf.drawString(72, 720 - index * 16, line)
    pdf.showPage()
    for index, line in enumerate(rows_p2):
        pdf.drawString(72, 720 - index * 16, line)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _rotate_second_page(pdf_bytes: bytes, angle: int = 90) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index == 1:
            page.rotate(angle)
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _crop_first_page(pdf_bytes: bytes) -> bytes:
    """把第一页 CropBox 缩到右下四分之一（文本仍应被提取，页归属不变）。"""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import RectangleObject

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index == 0:
            media = page.mediabox
            page.cropbox = RectangleObject(
                [
                    float(media.width) / 2,
                    float(media.height) / 2,
                    float(media.width),
                    float(media.height),
                ]
            )
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


async def _parse_pdf(container, username: str, pdf_bytes: bytes):
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-{username}", description=None, visibility="tenant"
    )
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="geo.pdf", declared_total_bytes=len(pdf_bytes)
    )
    await container.uploads.put_part(scope, upload.id, 1, iter([pdf_bytes]))
    artifact_id = await container.uploads.complete_upload(scope, upload.id)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=artifact_id,
        dataset_id=dataset.id,
        pipeline_ref=None,
        document_id=None,
        expected_revision=None,
        filename="geo.pdf",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error
    return result


async def _load_ir(container, tenant_id, version_id) -> dict:
    import json

    from sqlalchemy import select

    from owli_parser.modules.catalog.models import DocVersionRow

    async with container.uow.read_only() as session:
        row = (
            await session.execute(select(DocVersionRow.ir_artifact_id).where(DocVersionRow.id == version_id))
        ).scalar_one()
    location, profile = await container.artifacts.resolve_location(tenant_id, row)
    reader = container.artifacts.open_reader(location, profile)
    try:
        return json.loads(reader.read())
    finally:
        reader.close()


def _blocks_with(ir: dict, needle: str) -> list[dict]:
    return [block for block in ir["blocks"] if needle in (block.get("text") or "")]


@pytest.mark.parametrize("variant", ["baseline", "rotated", "cropped"])
async def test_rotated_and_cropped_pdf_anchor_integrity(container, variant: str) -> None:
    base = _make_two_page_pdf()
    if variant == "baseline":
        pdf_bytes = base
    elif variant == "rotated":
        pdf_bytes = _rotate_second_page(base, 90)
    else:
        pdf_bytes = _crop_first_page(base)

    result = await _parse_pdf(container, f"geo-{variant}", pdf_bytes)
    job_ir = await _load_ir_via_result(container, result)

    alpha_blocks = _blocks_with(job_ir, "ALPHA")
    beta_blocks = _blocks_with(job_ir, "BETA")
    assert alpha_blocks, f"[{variant}] 第一页文本不应丢失"
    assert beta_blocks, f"[{variant}] 第二页文本不应丢失"

    # 锚点归属：ALPHA 只能在第 0 页、BETA 只能在第 1 页（不串页）
    for block in alpha_blocks:
        pages = {ref["page_index"] for ref in block["source_refs"] if ref["kind"] == "page_region"}
        assert pages == {0}, f"[{variant}] ALPHA 页归属错误: {pages}"
    for block in beta_blocks:
        pages = {ref["page_index"] for ref in block["source_refs"] if ref["kind"] == "page_region"}
        assert pages == {1}, f"[{variant}] BETA 页归属错误: {pages}"

    # 页几何：旋转变体第二页 rotation=90 如实进入 IR
    pages = {page["page_index"]: page for page in job_ir["pages"]}
    assert pages[0]["rotation"] == 0
    if variant == "rotated":
        assert pages[1]["rotation"] == 90, "旋转页几何未正确记录"
    else:
        assert pages[1]["rotation"] == 0

    # 覆盖完备：两页均成功，无单位错误
    coverage = job_ir.get("coverage") or {}
    assert coverage.get("total_units") == 2
    assert coverage.get("succeeded_units") == 2


async def test_cross_page_table_stays_page_scoped(container) -> None:
    """跨页表格不被合并：行文本只出现在所属页的块，续页不回贴表头。"""
    result = await _parse_pdf(container, "geo-table", _make_cross_page_table_pdf())
    job_ir = await _load_ir_via_result(container, result)

    def page_refs(block: dict) -> set[int]:
        return {ref["page_index"] for ref in block["source_refs"] if ref["kind"] == "page_region"}

    header = _blocks_with(job_ir, "TABLE-HEADER")
    page_one_rows = _blocks_with(job_ir, "ROW-P1")
    page_two_rows = _blocks_with(job_ir, "ROW-P2")
    assert header and page_one_rows and page_two_rows
    for block in header + page_one_rows:
        assert page_refs(block) == {0}, "页 1 表格内容串页"
    for block in page_two_rows:
        assert page_refs(block) == {1}, "页 2 续行串页"

    # 不跨页合并：任一文本块不得同时包含两页的行；表头不重复到页 2
    for block in job_ir["blocks"]:
        text = block.get("text") or ""
        if "ROW-P1" in text:
            assert "ROW-P2" not in text
        if "ROW-P2" in text:
            assert "ROW-P1" not in text
            assert "TABLE-HEADER" not in text

    coverage = job_ir.get("coverage") or {}
    assert coverage.get("total_units") == 2
    assert coverage.get("succeeded_units") == 2


async def _load_ir_via_result(container, result) -> dict:
    """经版本行取 tenant/IR（避免测试内重复构造 scope）。"""
    from sqlalchemy import select

    from owli_parser.modules.catalog.models import DocumentRow, DocVersionRow

    async with container.uow.read_only() as session:
        row = (
            await session.execute(
                select(DocVersionRow.ir_artifact_id, DocumentRow.tenant_id)
                .join(DocumentRow, DocumentRow.id == DocVersionRow.document_id)
                .where(DocVersionRow.id == result.version_id)
            )
        ).one()
    return await _load_ir(container, row[1], result.version_id)
