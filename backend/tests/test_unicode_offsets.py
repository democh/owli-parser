"""来源矩阵：Unicode 文本偏移 fixture（锁定 block-local 码点语义）。

设计口径（normalize.py）：文本块偏移是"块内偏移"（normalization_revision=block-local-v1），
不伪装原件精确偏移。本 fixture 用中文、emoji、扩展汉字与组合字符验证：
- start=0 且 end=len(text)（Unicode 码点，不是 UTF-8 字节或 UTF-16 单元）；
- 超长段落切分后每块仍满足块内偏移，且按序拼接可还原原文（不丢字）；
- 全程无 unit_error、覆盖率完备。
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.models import DocumentRow, DocVersionRow
from owli_parser.modules.catalog.service import CatalogScope
from tests.conftest import create_user_with_tenant

_COMBINING = "e" + chr(0x301)  # e + combining acute（2 个码点，1 个字形）
_EMOJI_LINE = f"emoji：😀🚀 与扩展汉字 𠮷，组合字符 {_COMBINING}。"
_LONG_PARAGRAPH = "".join(f"第{index}句：内容😀。" for index in range(400))
_UNICODE_TEXT = f"第一段：中文（全角标点）。\n\n{_EMOJI_LINE}\n\n{_LONG_PARAGRAPH}\n"


async def _parse_text(container, username: str, text: str):
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
    data = text.encode("utf-8")
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="unicode.txt", declared_total_bytes=len(data)
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
        filename="unicode.txt",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error
    return result


async def _load_ir(container, result) -> dict:
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
        return json.loads(reader.read())
    finally:
        reader.close()


async def test_unicode_text_span_offsets_are_block_local_codepoints(container) -> None:
    result = await _parse_text(container, "unicode-offsets", _UNICODE_TEXT)
    ir = await _load_ir(container, result)

    blocks = [block for block in ir["blocks"] if block.get("text")]
    assert blocks, "文本块不应为空"

    # 核心不变量：块内偏移按 Unicode 码点计，不按字节/UTF-16 单元
    for block in blocks:
        refs = block["source_refs"]
        assert len(refs) == 1
        ref = refs[0]
        assert ref["kind"] == "text_span"
        assert ref["normalization_revision"] == "block-local-v1"
        assert ref["start"] == 0
        assert ref["end"] == len(block["text"])

    # emoji / 扩展汉字：单个字符计 1 个码点（UTF-8 字节数明显更大）
    emoji_block = next(block for block in blocks if "😀" in block["text"])
    assert len("😀") == 1
    assert len("𠮷") == 1
    assert emoji_block["source_refs"][0]["end"] == len(emoji_block["text"])
    assert emoji_block["source_refs"][0]["end"] < len(emoji_block["text"].encode("utf-8"))

    # 组合字符：e + combining acute 计 2 个码点，不应被折叠成 1
    combining_block = next(block for block in blocks if _COMBINING in block["text"])
    assert combining_block["source_refs"][0]["end"] == len(combining_block["text"])

    # 超长段落切分：每块 ≤ 2000 码点，按序拼接还原原文（不丢字）
    long_blocks = [block for block in blocks if "句：内容" in block["text"]]
    assert len(long_blocks) >= 2, "超长段落应被切分为多块"
    for block in long_blocks:
        assert 0 < len(block["text"]) <= 2000
        ref = block["source_refs"][0]
        assert ref["start"] == 0
        assert ref["end"] == len(block["text"])
    assert "".join(block["text"] for block in long_blocks) == _LONG_PARAGRAPH

    # 覆盖完备：单文档单元成功，无缺失单位
    coverage = ir.get("coverage") or {}
    assert coverage.get("total_units") == 1
    assert coverage.get("succeeded_units") == 1
    assert coverage.get("missing_units") == []
