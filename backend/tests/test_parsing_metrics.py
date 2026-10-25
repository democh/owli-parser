"""解析质量指标测试：CER/WER、单元格结构 F1、阅读顺序、bbox IoU 与边界。"""

from __future__ import annotations

import pytest

from owli_parser.modules.parsing.compare import compare_ir_blocks
from owli_parser.modules.parsing.metrics import (
    BBox,
    bbox_iou,
    cell_structure_f1,
    character_error_rate,
    levenshtein,
    reading_order_score,
    word_error_rate,
)


def test_levenshtein_basic_operations() -> None:
    assert levenshtein(list("abc"), list("abc")) == 0
    assert levenshtein(list("abc"), list("abd")) == 1  # 替换
    assert levenshtein(list("abc"), list("abxc")) == 1  # 插入
    assert levenshtein(list("abc"), list("ac")) == 1  # 删除
    assert levenshtein([], list("ab")) == 2
    assert levenshtein(list("ab"), []) == 2
    with pytest.raises(ValueError):
        levenshtein(["x"] * 20_001, ["y"])


def test_character_and_word_error_rate() -> None:
    assert character_error_rate("abc", "abc") == 0.0
    assert character_error_rate("abd", "abc") == pytest.approx(1 / 3)
    # 边界：参考空 + 假设非空 → 1.0（全错）；双空 → 0.0
    assert character_error_rate("x", "") == 1.0
    assert character_error_rate("", "") == 0.0

    assert word_error_rate("the quick fox", "the quick brown fox") == pytest.approx(0.25)
    assert word_error_rate("a b", "a b") == 0.0
    assert word_error_rate("extra", "") == 1.0


def test_cell_structure_f1() -> None:
    ref = [["姓名", "部门"], ["张三", "研发"]]
    perfect = cell_structure_f1(ref, ref)
    assert perfect.f1 == 1.0
    assert perfect.exact_text_ratio == 1.0
    assert perfect.ref_cells == 4

    # 一格文本错误：结构匹配 3/4（F1 = 2*3/4*1 / (3/4+1)）
    typo = [["姓名", "部门"], ["张三", "市场"]]
    report = cell_structure_f1(typo, ref)
    assert report.precision == pytest.approx(0.75)
    assert report.recall == pytest.approx(0.75)
    assert report.f1 == pytest.approx(0.75)
    # 位置匹配中 3/4 文本一致
    assert report.exact_text_ratio == pytest.approx(0.75)

    # 缺列：precision 1（假设全对）、recall 0.5
    missing = [["姓名"], ["张三"]]
    partial = cell_structure_f1(missing, ref)
    assert partial.precision == 1.0
    assert partial.recall == pytest.approx(0.5)

    # 边界：双空 → 全 1；单侧空 → 0
    assert cell_structure_f1([], []).f1 == 1.0
    assert cell_structure_f1([["a"]], []).f1 == 0.0
    assert cell_structure_f1([], [["a"]]).f1 == 0.0


def test_reading_order_score() -> None:
    ref = ["标题", "第一段", "第二段", "结尾"]
    assert reading_order_score(ref, ref) == 1.0
    # 完全倒序：LCS = 1（任一单块）
    assert reading_order_score(list(reversed(ref)), ref) == pytest.approx(0.25)
    # 中间缺失两块：LCS = 2
    assert reading_order_score(["标题", "结尾"], ref) == pytest.approx(0.5)
    # 空白折叠后比较
    assert reading_order_score([" 标题 ", "第一段", "第二段", "结尾"], ref) == 1.0
    # 假设为空、参考非空 → 0；无参考 → 1（无顺序可考）
    assert reading_order_score([], ref) == 0.0
    assert reading_order_score(["任意"], []) == 1.0


def test_bbox_iou() -> None:
    base = BBox(0.0, 0.0, 1.0, 1.0)
    assert bbox_iou(base, BBox(0.0, 0.0, 1.0, 1.0)) == 1.0
    assert bbox_iou(base, BBox(1.0, 1.0, 2.0, 2.0)) == 0.0  # 仅边界接触
    assert bbox_iou(base, BBox(0.5, 0.5, 1.5, 1.5)) == pytest.approx(0.25 / 1.75, abs=1e-6)
    # 零面积与负尺寸防御
    assert bbox_iou(BBox(0.2, 0.2, 0.2, 0.2), base) == 0.0


# ---------------------------------------------------------------------------
# 解析质量对比（候选 IR vs 基准 IR）
# ---------------------------------------------------------------------------


def _region(page: int, bbox: tuple[float, float, float, float]) -> dict:
    x0, y0, x1, y1 = bbox
    return {
        "kind": "page_region",
        "page_index": page,
        "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
    }


def _text_block(text: str, *, page: int = 0, bbox: tuple[float, float, float, float] | None = None):
    source_refs = [_region(page, bbox)] if bbox else [{"kind": "text_span", "part": 0}]
    return {"type": "text", "text": text, "payload": {}, "source_refs": source_refs}


def _table_block(grid: list[list[str]], bbox: tuple[float, float, float, float]):
    return {
        "type": "table",
        "text": " | ".join(" ".join(row) for row in grid),
        "payload": {
            "payload_type": "table",
            "rows": [[{"text": cell} for cell in row] for row in grid],
        },
        "source_refs": [_region(0, bbox)],
    }


def test_compare_ir_blocks_perfect_self_comparison() -> None:
    blocks = [
        _text_block("标题", page=0, bbox=(0.1, 0.05, 0.9, 0.1)),
        _table_block([["姓名", "部门"], ["张三", "研发"]], (0.1, 0.2, 0.9, 0.5)),
        _text_block("结尾段落", page=0, bbox=(0.1, 0.6, 0.9, 0.7)),
    ]
    result = compare_ir_blocks(blocks, blocks)
    assert result.text_cer == 0.0
    assert result.reading_order == 1.0
    assert result.table_f1 == 1.0
    assert result.table_pairs == 1
    assert result.location_iou == 1.0
    assert result.location_pairs == 3
    metrics = result.as_metrics()
    assert metrics["table_structure_f1"] == 1.0
    assert metrics["location_iou"] == 1.0


def test_compare_ir_blocks_degrades_and_detects_differences() -> None:
    reference = [
        _text_block("第一段正确文本", page=0, bbox=(0.1, 0.1, 0.9, 0.2)),
        _text_block("第二段应当保留", page=0, bbox=(0.1, 0.3, 0.9, 0.4)),
    ]
    hypothesis = [
        _text_block("第一段错误文本", page=0, bbox=(0.1, 0.1, 0.9, 0.2)),  # 一字差异
    ]
    result = compare_ir_blocks(reference, hypothesis)
    assert result.text_cer > 0
    assert result.reading_order < 1.0  # 缺块导致顺序覆盖下降
    assert result.reference_blocks == 2 and result.hypothesis_blocks == 1
    # 缺失维度不伪造：无表格、无表格对
    assert result.table_f1 is None
    assert "table_structure_f1" not in result.as_metrics()
    assert result.location_pairs == 1  # 定位对按最小长度配对（2 参考 vs 1 候选）

    # 双方均无 page_region → 定位指标为 None
    no_loc = compare_ir_blocks([_text_block("a", bbox=None)], [_text_block("a", bbox=None)])
    assert no_loc.location_iou is None
    assert "location_iou" not in no_loc.as_metrics()


async def test_compare_real_ir_artifact_self_baseline(container) -> None:
    """真实链路：解析 TXT → 从 IR artifact 读取 blocks → 自对比为完美基线。"""
    import json

    from owli_parser.core.context import ActorKind
    from owli_parser.modules.catalog.domain import Role, UserAccessContext
    from owli_parser.modules.catalog.service import CatalogScope
    from tests.conftest import create_user_with_tenant

    user_id, tenant_id, _ = await create_user_with_tenant(container, username="cmp-ir")
    scope = CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )
    dataset = await container.catalog.create_dataset(
        scope, name="ds-compare", description=None, visibility="tenant"
    )
    body = "第一章 总则\n本章说明系统目标与交付边界。\n第二章 验收\n验收标准见附录。\n".encode()
    upload = await container.uploads.init_upload(
        scope, dataset_id=dataset.id, filename="doc.txt", declared_total_bytes=len(body)
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
        filename="doc.txt",
    )
    pipeline = container.submit_parsing.compile_default_pipeline()
    await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)

    version = await container.catalog.get_version(scope, result.document_id, result.version_id)
    assert version is not None and version.ir_artifact_id is not None
    location, profile = await container.artifacts.resolve_location(tenant_id, version.ir_artifact_id)
    reader = container.artifacts.open_reader(location, profile)
    try:
        ir_json = json.loads(reader.read())
    finally:
        reader.close()
    blocks = ir_json["blocks"]
    assert blocks, "真实 IR 应包含块"

    identical = compare_ir_blocks(blocks, blocks)
    assert identical.text_cer == 0.0
    assert identical.reading_order == 1.0
    assert identical.reference_blocks == identical.hypothesis_blocks
