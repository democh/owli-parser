"""M2 后处理测试：文本语言检测、精确去重、乱码率（文档流水线第 8 步）。"""

from __future__ import annotations

import uuid

from owli_parser.modules.parsing.adapters_base import (
    ExtractedBlock,
    ExtractedPage,
    ParseOutcome,
)
from owli_parser.modules.parsing.langdetect import detect_language
from owli_parser.modules.parsing.near_dup import find_near_duplicates
from owli_parser.modules.parsing.normalize import build_document_ir

# ---------------------------------------------------------------------------
# 文本语言检测（脚本级；短文本/混合不强制归类）
# ---------------------------------------------------------------------------


def test_detect_language_scripts() -> None:
    assert detect_language("这是一段足够长的中文内容用于检测").lang == "zh"
    assert detect_language("これは日本語のテキストです、十分に長い").lang == "ja"
    assert detect_language("한국어로 작성된 충분히 긴 텍스트입니다").lang == "ko"
    assert detect_language("Это достаточно длинный текст на русском").lang == "ru"
    # 拉丁文本不猜具体语种（fastText 或准入替代方案负责细分）
    assert detect_language("This is a sufficiently long latin sentence").lang == "latin"


def test_detect_language_short_mixed_and_hint() -> None:
    # 短文本 → unknown（None）+ too-short
    short = detect_language("你好")
    assert short.lang is None and short.method == "too-short"
    assert detect_language("").lang is None
    # 混合语言（中英各半）→ mixed（不强制归类）
    mixed = detect_language("这是中文内容前段 mixed with english words 后续内容")
    assert mixed.lang == "mixed"
    # hint 优先
    hinted = detect_language("短", hint="zh")
    assert hinted.lang == "zh" and hinted.method == "hint"


# ---------------------------------------------------------------------------
# 后处理集成：精确去重 + 乱码率 + 语言回填（经 build_document_ir）
# ---------------------------------------------------------------------------


def _block(order: int, text: str, *, lang: str | None = None) -> ExtractedBlock:
    return ExtractedBlock(
        type="text",
        text=text,
        source_ref={"kind": "text_span", "part": order},
        lang=lang,
    )


def _build_ir(blocks: list[ExtractedBlock]):
    outcome = ParseOutcome(
        blocks=blocks,
        pages=[ExtractedPage(page_index=0, width=612, height=792)],
        engine="test",
    )
    ids = {name: uuid.uuid4() for name in ("doc", "ver", "ds", "tenant", "artifact")}
    return build_document_ir(
        parse_outcome=outcome,
        adapter_name="test-adapter",
        adapter_version="1",
        adapter_mode="local",
        doc_id=ids["doc"],
        version_id=ids["ver"],
        dataset_id=ids["ds"],
        tenant_id=ids["tenant"],
        content_hash="hash",
        effective_config_digest="digest",
        source_artifact_id=ids["artifact"],
    )


def test_exact_dedup_removes_repeated_blocks() -> None:
    ir = _build_ir(
        [
            _block(0, "页眉标题重复内容"),
            _block(1, "正文第一段内容正文第一段内容"),
            _block(2, "页眉标题重复内容"),  # 完全重复（空白折叠后）→ 去除
            _block(3, " 页眉标题重复内容 "),  # 空白差异仍视为重复 → 去除
            _block(4, "正文第二段内容正文第二段内容"),
            _block(5, "12"),  # 过短重复不去重
            _block(6, "12"),
        ]
    )
    texts = [block.text for block in ir.blocks]
    assert texts.count("页眉标题重复内容") == 1
    assert texts.count("12") == 2  # 短文本不误删
    assert len(ir.blocks) == 5
    # 质量报告如实记录去重比例（2/7）
    assert ir.quality.metrics["duplicate_block_ratio"] == round(2 / 7, 6)
    # 块 ID 连续重排（order 与 id 一致，无空洞）
    assert [block.id for block in ir.blocks] == [f"b-{i:04d}" for i in range(5)]
    assert [block.order for block in ir.blocks] == list(range(5))


def test_replacement_char_ratio_and_lang_backfill() -> None:
    ir = _build_ir(
        [
            _block(0, "中文内容第一段用于语言回填\ufffd\ufffd"),
            _block(1, "English content spanning enough characters"),
            _block(2, "短"),  # 检测为 unknown → lang 保持 None（不写 unknown 字符串）
        ]
    )
    total_chars = sum(len(block.text) for block in ir.blocks)
    assert ir.quality.metrics["replacement_char_ratio"] == round(2 / total_chars, 6)
    by_order = {block.order: block for block in ir.blocks}
    assert by_order[0].lang == "zh"  # 检测回填（adapter 未提供 lang）
    assert by_order[1].lang == "latin"
    assert by_order[2].lang is None
    assert ir.quality.algorithm_revision == "quality-v3"


def test_adapter_lang_takes_precedence_over_detection() -> None:
    ir = _build_ir([_block(0, "本行文本长度足够触发检测但以 adapter 标注为准", lang="zh-Hans")])
    assert ir.blocks[0].lang == "zh-Hans"


# ---------------------------------------------------------------------------
# 近重复提示（只报告不删除）
# ---------------------------------------------------------------------------


def test_find_near_duplicates_detects_and_ranks() -> None:
    base = "第三章 交付验收条款如下所述双方应共同遵守并执行"
    near = "第三章 交付验收条款如下所述双方应共同遵照并执行"  # 仅 1 字差异
    unrelated = "完全无关的另一段内容用于验证不会误报近重复"
    pairs = find_near_duplicates([base, near, unrelated])
    assert len(pairs) == 1
    pair = pairs[0]
    assert (pair.left_index, pair.right_index) == (0, 1)
    assert pair.similarity >= 0.8
    # 高相似但未达阈值 → 不报
    assert find_near_duplicates([base, near], threshold=0.99) == []


def test_find_near_duplicates_length_guard_and_limits() -> None:
    short = "短短短"
    long_text = "这是一段很长很长的内容" * 5
    assert find_near_duplicates([short, long_text]) == []  # 短文本不参与
    # 长度差过大预筛（相似度上界不足）
    a = "内容内容内容内容内容内容内容内容内容内容"
    b = "内容内容内容内容内容内容内容内容内容内容" + "不同后缀" * 20
    assert find_near_duplicates([a, b], threshold=0.5) == []

    # max_pairs 截断按相似度降序
    texts = [f"共同前缀足够长的文本{i}号补丁甲" for i in range(4)]
    texts[1] = "共同前缀足够长的文本X号补丁甲"
    texts[2] = "共同前缀足够长的文本Y号补丁甲"
    capped = find_near_duplicates(texts, threshold=0.5, max_pairs=1)
    assert len(capped) == 1


def test_ir_near_duplicate_reporting() -> None:
    ir = _build_ir(
        [
            _block(0, "第三章 交付验收条款如下所述双方应共同遵守并执行"),
            _block(1, "第三章 交付验收条款如下所述双方应共同遵照并执行"),
            _block(2, "完全无关的另一段内容用于保证不误报"),
        ]
    )
    assert ir.quality.metrics["near_duplicate_pair_count"] == 1.0
    assert ir.quality.notes is not None
    assert "near_duplicate b-0000~b-0001" in ir.quality.notes
    # 近重复只报告不删除：三个块都在
    assert len(ir.blocks) == 3


def test_ir_near_duplicate_scan_skipped_over_limit() -> None:
    blocks = [_block(i, f"第{i}段内容，长度足够不触发精确去重规则") for i in range(801)]
    ir = _build_ir(blocks)
    assert ir.quality.metrics["near_duplicate_pair_count"] == 0.0
    assert ir.quality.notes is not None
    assert "near_duplicate_scan_skipped" in ir.quality.notes
