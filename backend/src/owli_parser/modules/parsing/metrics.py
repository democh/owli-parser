"""解析质量指标（离线评估用纯函数；文档"质量指标按任务分开"）。

- 文本：CER/WER（编辑距离归一化）；
- 表格：单元格结构 P/R/F1 + 单元格文本精确匹配率（TEDS 需树编辑距离，
  留待第三方实现或专门评估包，当前以结构 F1 为基线并如实标注）；
- 阅读顺序：块序列 LCS 覆盖率；
- 来源定位：bbox IoU（0-1 归一化坐标）。
所有指标定义明确、边界写死（空参考等），不做隐式兜底。
"""

from __future__ import annotations

from dataclasses import dataclass

# 编辑距离保护上限（评估文本；超出直接拒绝，防误用在大文本上）
_MAX_TOKENS = 20_000


def levenshtein(left: list[str], right: list[str]) -> int:
    """序列编辑距离（插入/删除/替换代价均为 1）；空间 O(min) 行优化。"""
    if len(left) > _MAX_TOKENS or len(right) > _MAX_TOKENS:
        raise ValueError(f"评估序列超长（>{_MAX_TOKENS}），请分段或使用专用评估包")
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, left_item in enumerate(left, start=1):
        current = [i] + [0] * len(right)
        for j, right_item in enumerate(right, start=1):
            cost = 0 if left_item == right_item else 1
            current[j] = min(
                previous[j] + 1,  # 删除
                current[j - 1] + 1,  # 插入
                previous[j - 1] + cost,  # 替换
            )
        previous = current
    return previous[-1]


def character_error_rate(hypothesis: str, reference: str) -> float:
    """CER = 字符编辑距离 / 参考长度。

    边界：参考为空而假设非空 → 1.0（全错）；两者皆空 → 0.0。
    """
    if not reference:
        return 0.0 if not hypothesis else 1.0
    return levenshtein(list(hypothesis), list(reference)) / len(reference)


def word_error_rate(hypothesis: str, reference: str) -> float:
    """WER = 词编辑距离 / 参考词数（按空白分词）。边界同 CER。"""
    ref_words = reference.split()
    hyp_words = hypothesis.split()
    if not ref_words:
        return 0.0 if not hyp_words else 1.0
    return levenshtein(hyp_words, ref_words) / len(ref_words)


# ---------------------------------------------------------------------------
# 表格：单元格结构指标（TEDS 基线替代，如实标注）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CellStructureReport:
    precision: float
    recall: float
    f1: float
    exact_text_ratio: float  # 匹配单元格中文本完全一致的比例
    ref_cells: int
    hyp_cells: int


def _cell_key(row: int, col: int, text: str) -> tuple[int, int, str]:
    return (row, col, " ".join(text.split()))


def cell_structure_f1(hypothesis: list[list[str]], reference: list[list[str]]) -> CellStructureReport:
    """单元格结构 P/R/F1：单元格 (row, col, 归一化文本) 为元素集合。

    - precision/recall 按集合匹配；f1 为调和平均；
    - exact_text_ratio：位置匹配（row,col 相同）中文本一致的比例；
    - 双方皆空 → 全 1（无表可考）；仅一方为空 → 0。
    """
    ref_keys = {
        _cell_key(row, col, text) for row, cells in enumerate(reference) for col, text in enumerate(cells)
    }
    hyp_keys = {
        _cell_key(row, col, text) for row, cells in enumerate(hypothesis) for col, text in enumerate(cells)
    }
    if not ref_keys and not hyp_keys:
        return CellStructureReport(1.0, 1.0, 1.0, 1.0, 0, 0)
    if not ref_keys or not hyp_keys:
        return CellStructureReport(0.0, 0.0, 0.0, 0.0, len(ref_keys), len(hyp_keys))

    matched = ref_keys & hyp_keys
    precision = len(matched) / len(hyp_keys)
    recall = len(matched) / len(ref_keys)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    ref_positions = {(row, col): text for row, col, text in ref_keys}
    hyp_positions = {(row, col): text for row, col, text in hyp_keys}
    shared_positions = ref_positions.keys() & hyp_positions.keys()
    exact_text = (
        sum(1 for pos in shared_positions if ref_positions[pos] == hyp_positions[pos]) / len(shared_positions)
        if shared_positions
        else 0.0
    )
    return CellStructureReport(
        precision=round(precision, 6),
        recall=round(recall, 6),
        f1=round(f1, 6),
        exact_text_ratio=round(exact_text, 6),
        ref_cells=len(ref_keys),
        hyp_cells=len(hyp_keys),
    )


# ---------------------------------------------------------------------------
# 阅读顺序与来源定位
# ---------------------------------------------------------------------------


def reading_order_score(hypothesis: list[str], reference: list[str]) -> float:
    """块序列顺序覆盖：LCS(归一化块文本) / 参考块数。

    边界：参考为空 → 1.0（无顺序可考）。块文本按空白折叠后比较。
    """
    if not reference:
        return 1.0
    left = [" ".join(block.split()) for block in hypothesis]
    right = [" ".join(block.split()) for block in reference]
    # LCS 动态规划（行优化）
    previous = [0] * (len(right) + 1)
    for left_item in left:
        current = [0] * (len(right) + 1)
        for j, right_item in enumerate(right, start=1):
            if left_item == right_item:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = max(previous[j], current[j - 1])
        previous = current
    return round(previous[-1] / len(right), 6)


@dataclass(frozen=True, slots=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float


def bbox_iou(left: BBox, right: BBox) -> float:
    """来源定位误差：bbox 交并比（0-1 归一化坐标；不重叠为 0）。"""
    inter_x0 = max(left.x0, right.x0)
    inter_y0 = max(left.y0, right.y0)
    inter_x1 = min(left.x1, right.x1)
    inter_y1 = min(left.y1, right.y1)
    inter_area = max(0.0, inter_x1 - inter_x0) * max(0.0, inter_y1 - inter_y0)
    if inter_area == 0:
        return 0.0
    left_area = max(0.0, left.x1 - left.x0) * max(0.0, left.y1 - left.y0)
    right_area = max(0.0, right.x1 - right.x0) * max(0.0, right.y1 - right.y0)
    union = left_area + right_area - inter_area
    return round(inter_area / union, 6) if union > 0 else 0.0
