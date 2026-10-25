"""M5-3：ZIP 安全解包适配器（文本条目抽取、嵌套、路径/压缩比/条目数限制）。

- 直测适配器：抽取文本条目 + 嵌套 ZIP；二进制条目 warning；不安全路径/压缩比/条目数
  逐项记 unit_error；无文本条目 → archive_no_text_entries 失败；
- 端到端：经默认 pipeline 解析 ZIP，IR 保留 archive_path 与 text_span part 锚点。
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
from owli_parser.modules.parsing.adapters_base import (
    ExecutionContext,
    ParseFailedError,
    SourceHandle,
)
from owli_parser.modules.parsing.builtin_adapters import ZipLocalAdapter
from tests.conftest import create_user_with_tenant


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _handle(tmp_path, data: bytes, filename: str = "sample.zip") -> SourceHandle:
    path = tmp_path / filename
    path.write_bytes(data)
    return SourceHandle(path=path, mime=None, filename=filename, content_hash="", artifact_id="artifact-1")


def _with_encrypted_flag(data: bytes) -> bytes:
    """给所有中央目录条目置 encrypted 位（zipfile 写入会重置该位，故后置补丁）。"""
    patched = bytearray(data)
    marker = b"PK\x01\x02"
    start = 0
    while True:
        index = patched.find(marker, start)
        if index < 0:
            break
        flag_offset = index + 8
        flags = int.from_bytes(patched[flag_offset : flag_offset + 2], "little")
        patched[flag_offset : flag_offset + 2] = (flags | 0x1).to_bytes(2, "little")
        start = index + 4
    return bytes(patched)


def _execution() -> ExecutionContext:
    return ExecutionContext(deadline_epoch=time.time() + 60, cancellation=lambda: False)


def test_zip_extracts_text_entries_and_nested_archives(tmp_path) -> None:
    nested = _zip_bytes({"inner.txt": "嵌套内容".encode()})
    data = _zip_bytes(
        {
            "docs/readme.md": "# 标题\n正文".encode(),
            "data.csv": b"a,b\n1,2",
            "image.bin": b"\x00\x01\x02",
            "nested.zip": nested,
        }
    )
    outcome = ZipLocalAdapter().parse(_handle(tmp_path, data), {}, _execution())

    texts = {block.text for block in outcome.blocks}
    assert "# 标题\n正文" in texts
    assert "a,b\n1,2" in texts
    assert "嵌套内容" in texts
    parts = {block.source_ref["part"] for block in outcome.blocks}
    assert "zip:docs/readme.md" in parts
    assert "zip:nested.zip!/inner.txt" in parts
    assert any("image.bin" in warning for warning in outcome.warnings)
    assert outcome.engine == "python-zipfile"
    assert outcome.total_units == 3
    assert outcome.succeeded_units == 3
    for block in outcome.blocks:
        assert block.source_ref["kind"] == "text_span"
        assert block.source_ref["end"] == len(block.text)


def test_zip_rejects_unsafe_paths_and_encrypted_entries(tmp_path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../evil.txt", b"evil")
        archive.writestr("ok.txt", b"ok")
    outcome = ZipLocalAdapter().parse(_handle(tmp_path, buffer.getvalue()), {}, _execution())

    codes = {error.code for error in outcome.unit_errors}
    assert "archive_unsafe_path" in codes
    assert [block.text for block in outcome.blocks] == ["ok"]
    assert outcome.total_units == 2
    assert outcome.succeeded_units == 1

    # 加密条目：补丁中央目录 flag 位（不解压内容）
    encrypted = io.BytesIO()
    with zipfile.ZipFile(encrypted, "w") as archive:
        archive.writestr("secret.txt", b"secret")
    encrypted_outcome = ZipLocalAdapter().parse(
        _handle(tmp_path, _with_encrypted_flag(encrypted.getvalue()), "encrypted.zip"),
        {},
        _execution(),
    )
    assert "archive_encrypted_entry" in {error.code for error in encrypted_outcome.unit_errors}


def test_zip_enforces_ratio_entries_and_depth(tmp_path) -> None:
    bomb = _zip_bytes({"bomb.txt": b"a" * 20000})
    ratio_outcome = ZipLocalAdapter().parse(
        _handle(tmp_path, bomb, "bomb.zip"),
        {"archive_max_ratio": 10},
        _execution(),
    )
    assert "archive_ratio_exceeded" in {error.code for error in ratio_outcome.unit_errors}

    many = _zip_bytes({f"f{index}.txt": b"x" for index in range(3)})
    entries_outcome = ZipLocalAdapter().parse(
        _handle(tmp_path, many, "many.zip"),
        {"archive_max_entries": 2},
        _execution(),
    )
    assert "archive_too_many_entries" in {error.code for error in entries_outcome.unit_errors}

    nested = _zip_bytes({"inner.txt": b"deep"})
    depth_outcome = ZipLocalAdapter().parse(
        _handle(tmp_path, _zip_bytes({"outer.txt": b"outer", "nested.zip": nested}), "depth.zip"),
        {"archive_max_depth": 0},
        _execution(),
    )
    assert [block.text for block in depth_outcome.blocks] == ["outer"]
    assert any("深度上限" in warning for warning in depth_outcome.warnings)


def test_zip_without_text_entries_fails_explicitly(tmp_path) -> None:
    data = _zip_bytes({"image.bin": b"\x00\x01"})
    with pytest.raises(ParseFailedError) as excinfo:
        ZipLocalAdapter().parse(_handle(tmp_path, data), {}, _execution())
    assert excinfo.value.code == "archive_no_text_entries"


async def _run_zip_job(container, username: str, data: bytes):
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
        scope, dataset_id=dataset.id, filename="bundle.zip", declared_total_bytes=len(data)
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
        filename="bundle.zip",
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


async def test_zip_end_to_end_pipeline_keeps_archive_anchors(container) -> None:
    data = _zip_bytes(
        {
            "docs/readme.md": "知识库条目正文".encode(),
            "nested.zip": _zip_bytes({"inner.txt": "嵌套条目正文".encode()}),
            "image.bin": b"\x00\x01",
        }
    )
    result = await _run_zip_job(container, "zip-e2e", data)
    ir = await _load_ir(container, result)

    blocks = [block for block in ir["blocks"] if block.get("text")]
    texts = {block["text"] for block in blocks}
    assert "知识库条目正文" in texts
    assert "嵌套条目正文" in texts

    readme = next(block for block in blocks if block["text"] == "知识库条目正文")
    assert readme["source_refs"][0]["kind"] == "text_span"
    assert readme["source_refs"][0]["part"] == "zip:docs/readme.md"
    assert readme["source_refs"][0]["path"] == "docs/readme.md"
    assert readme["source_refs"][0]["start"] == 0
    assert readme["source_refs"][0]["end"] == len("知识库条目正文")

    coverage = ir.get("coverage") or {}
    assert coverage.get("total_units") == 2
    assert coverage.get("succeeded_units") == 2
