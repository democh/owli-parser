"""M5-3：Jupyter Notebook 适配器（单元格抽取、标题层级、代码语言、失败语义）。

- 直测：markdown 单行标题进入 heading_path；code 带 kernelspec language；raw 按 text；
  无效 JSON → unit_error；无有效单元格 → empty_notebook 失败；
- 端到端：经默认 pipeline 解析 .ipynb，IR 类型/provenance/覆盖率正确。
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import select

from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.models import DocumentRow, DocVersionRow
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.parsing.adapters_base import ExecutionContext, ParseFailedError, SourceHandle
from owli_parser.modules.parsing.builtin_adapters import JupyterLocalAdapter
from tests.conftest import create_user_with_tenant


def _notebook(cells: list[dict]) -> bytes:
    return json.dumps(
        {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "language": "python"}},
            "cells": cells,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _handle(tmp_path, data: bytes, filename: str = "sample.ipynb") -> SourceHandle:
    path = tmp_path / filename
    path.write_bytes(data)
    return SourceHandle(path=path, mime=None, filename=filename, content_hash="", artifact_id="artifact-1")


def _execution() -> ExecutionContext:
    return ExecutionContext(deadline_epoch=time.time() + 60, cancellation=lambda: False)


def test_jupyter_extracts_cells_with_heading_and_language(tmp_path) -> None:
    data = _notebook(
        [
            {"cell_type": "markdown", "source": ["# 标题"]},
            {"cell_type": "markdown", "source": ["说明文字"]},
            {"cell_type": "code", "source": ["print(1)\n", "print(2)"]},
            {"cell_type": "raw", "source": ["原始文本"]},
            {"cell_type": "code", "source": ["   "]},
        ]
    )
    outcome = JupyterLocalAdapter().parse(_handle(tmp_path, data), {}, _execution())

    blocks = outcome.blocks
    assert [block.type for block in blocks] == ["heading", "text", "code", "text"]
    assert blocks[0].text == "标题"
    assert blocks[0].payload["level"] == 1
    assert blocks[1].payload["heading_path"] == ["标题"]
    assert blocks[2].text == "print(1)\nprint(2)"
    assert blocks[2].payload["code_language"] == "python"
    assert blocks[3].text == "原始文本"
    assert [block.source_ref["part"] for block in blocks] == [
        "cells[0]",
        "cells[1]",
        "cells[2]",
        "cells[3]",
    ]
    for block in blocks:
        assert block.source_ref["end"] == len(block.text)
    assert outcome.total_units == 1
    assert outcome.succeeded_units == 1


def test_jupyter_invalid_and_empty_notebook_fail_explicitly(tmp_path) -> None:
    invalid = JupyterLocalAdapter().parse(_handle(tmp_path, b"{not json", "invalid.ipynb"), {}, _execution())
    assert "invalid_notebook" in {error.code for error in invalid.unit_errors}
    assert invalid.succeeded_units == 0

    with pytest.raises(ParseFailedError) as excinfo:
        JupyterLocalAdapter().parse(
            _handle(tmp_path, _notebook([{"cell_type": "code", "source": [""]}]), "empty.ipynb"),
            {},
            _execution(),
        )
    assert excinfo.value.code == "empty_notebook"


async def _run_notebook_job(container, username: str, data: bytes):
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
        scope, dataset_id=dataset.id, filename="analysis.ipynb", declared_total_bytes=len(data)
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
        filename="analysis.ipynb",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
    job = await container.jobs.get_job(tenant_id, result.job_id)
    assert job.state == "DONE", job.error
    return result


async def test_jupyter_end_to_end_pipeline(container) -> None:
    data = _notebook(
        [
            {"cell_type": "markdown", "source": ["# 分析报告"]},
            {"cell_type": "code", "source": ["result = 1 + 1\n", "result"]},
        ]
    )
    result = await _run_notebook_job(container, "jupyter-e2e", data)
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
    types = [block["type"] for block in blocks]
    assert "heading" in types
    assert "code" in types
    assert blocks[0]["provenance"]["adapter_name"] == "jupyter-local"
    code = next(block for block in blocks if block["type"] == "code")
    assert code["payload"]["code_language"] == "python"
    assert code["source_refs"][0]["part"] == "cells[1]"
    assert (ir.get("coverage") or {}).get("succeeded_units") == 1
