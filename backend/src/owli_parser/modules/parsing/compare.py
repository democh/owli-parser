"""解析质量对比（候选 IR vs 基准 IR）：文本/阅读顺序/表格结构/来源定位。

- 输入为 IR JSON 的 blocks 列表（dict），可从版本 IR artifact 直接读取；
- 缺失维度不伪造：无表格对或定位对时对应指标为 None（展示为 —）；
- 表格/定位按双方出现顺序一一配对（最小长度），不做跨块模糊匹配；
- 指标口径见 modules/parsing/metrics.py（边界语义以那里为准）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from owli_parser.modules.parsing.metrics import (
    BBox,
    bbox_iou,
    cell_structure_f1,
    character_error_rate,
    reading_order_score,
)


@dataclass(frozen=True, slots=True)
class ParsingComparison:
    text_cer: float
    reading_order: float
    table_f1: float | None
    table_pairs: int
    location_iou: float | None
    location_pairs: int
    reference_blocks: int
    hypothesis_blocks: int

    def as_metrics(self) -> dict[str, float]:
        """扁平指标字典（与 golden runner 的 metrics 记法一致；缺失不填 0）。"""
        metrics = {
            "text_cer": self.text_cer,
            "reading_order": self.reading_order,
            "reference_blocks": float(self.reference_blocks),
            "hypothesis_blocks": float(self.hypothesis_blocks),
        }
        if self.table_f1 is not None:
            metrics["table_structure_f1"] = self.table_f1
            metrics["table_pairs"] = float(self.table_pairs)
        if self.location_iou is not None:
            metrics["location_iou"] = self.location_iou
            metrics["location_pairs"] = float(self.location_pairs)
        return metrics


def _block_text(block: dict[str, Any]) -> str:
    return str(block.get("text") or "")


def _table_grid(block: dict[str, Any]) -> list[list[str]]:
    payload = block.get("payload") or {}
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    grid: list[list[str]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        grid.append([str(cell.get("text") or "") for cell in row if isinstance(cell, dict)])
    return grid


def _page_regions(block: dict[str, Any]) -> list[BBox]:
    refs = block.get("source_refs") or []
    boxes: list[BBox] = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("kind") != "page_region":
            continue
        bbox = ref.get("bbox")
        if isinstance(bbox, dict):
            boxes.append(
                BBox(
                    x0=float(bbox.get("x0", 0.0)),
                    y0=float(bbox.get("y0", 0.0)),
                    x1=float(bbox.get("x1", 0.0)),
                    y1=float(bbox.get("y1", 0.0)),
                )
            )
    return boxes


def compare_ir_blocks(reference: list[dict[str, Any]], hypothesis: list[dict[str, Any]]) -> ParsingComparison:
    """对比两组 IR 块（基准 vs 候选）；轮换顺序为字面意义（reference 在前）。"""
    ref_texts = [_block_text(block) for block in reference]
    hyp_texts = [_block_text(block) for block in hypothesis]
    text_cer = character_error_rate("\n".join(hyp_texts), "\n".join(ref_texts))
    reading = reading_order_score(hyp_texts, ref_texts)

    ref_grids = [_table_grid(block) for block in reference if block.get("type") == "table"]
    hyp_grids = [_table_grid(block) for block in hypothesis if block.get("type") == "table"]
    table_pairs = min(len(ref_grids), len(hyp_grids))
    table_f1: float | None = None
    if table_pairs > 0:
        scores = [cell_structure_f1(hyp_grids[index], ref_grids[index]).f1 for index in range(table_pairs)]
        table_f1 = round(sum(scores) / len(scores), 6)

    ref_boxes = [box for block in reference for box in _page_regions(block)]
    hyp_boxes = [box for block in hypothesis for box in _page_regions(block)]
    location_pairs = min(len(ref_boxes), len(hyp_boxes))
    location_iou: float | None = None
    if location_pairs > 0:
        scores = [bbox_iou(hyp_boxes[index], ref_boxes[index]) for index in range(location_pairs)]
        location_iou = round(sum(scores) / len(scores), 6)

    return ParsingComparison(
        text_cer=round(text_cer, 6),
        reading_order=reading,
        table_f1=table_f1,
        table_pairs=table_pairs,
        location_iou=location_iou,
        location_pairs=location_pairs,
        reference_blocks=len(reference),
        hypothesis_blocks=len(hypothesis),
    )
