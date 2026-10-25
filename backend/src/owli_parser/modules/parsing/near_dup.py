"""近重复提示（后处理第 8 步）：相似但不完全相同的块配对报告。

- 只报告不删除：近重复文本可能是有意内容（合同条款重复等），
  交由人工/复核判断（文档用词"提示"）；
- 算法：空白折叠规范化 → 字符级 shingle（n-gram）集合 → Jaccard 相似度；
  长度比预筛 + 阈值过滤，结果按相似度降序截断；
- 性能护栏：块数超上限时跳过扫描并由调用方记录 warning（诚实降级，
  不假装已扫描）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 字符级 shingle 长度与相似度阈值
# （bigram 对中文短句的一字差异仍保持高 Jaccard；5-gram 对短句过于敏感）
_SHINGLE_SIZE = 2
_DEFAULT_THRESHOLD = 0.7
# 长度比低于该值的配对直接跳过（Jaccard 上界低于阈值）
_MIN_LENGTH_RATIO = 0.6


@dataclass(frozen=True, slots=True)
class NearDuplicatePair:
    left_index: int
    right_index: int
    similarity: float


def _shingles(text: str) -> set[str]:
    normalized = " ".join(text.split())
    if len(normalized) < _SHINGLE_SIZE:
        return set()
    return {normalized[i : i + _SHINGLE_SIZE] for i in range(len(normalized) - _SHINGLE_SIZE + 1)}


def find_near_duplicates(
    texts: list[str],
    *,
    threshold: float = _DEFAULT_THRESHOLD,
    max_pairs: int = 20,
) -> list[NearDuplicatePair]:
    """返回相似度 ≥ threshold 的块配对（按相似度降序，截断 max_pairs）。

    仅比较 index 更小者与更大者（i<j）；短于 shingle 长度的文本不参与。
    """
    shingle_sets: list[tuple[int, int, set[str]]] = []
    for index, text in enumerate(texts):
        shingles = _shingles(text)
        if shingles:
            shingle_sets.append((index, len(" ".join(text.split())), shingles))

    pairs: list[NearDuplicatePair] = []
    for position, (left_index, left_len, left_set) in enumerate(shingle_sets):
        for right_index, right_len, right_set in shingle_sets[position + 1 :]:
            longer, shorter = max(left_len, right_len), min(left_len, right_len)
            if shorter / longer < _MIN_LENGTH_RATIO:
                continue
            intersection = len(left_set & right_set)
            if intersection == 0:
                continue
            similarity = intersection / len(left_set | right_set)
            if similarity >= threshold:
                pairs.append(
                    NearDuplicatePair(
                        left_index=left_index, right_index=right_index, similarity=round(similarity, 4)
                    )
                )
    pairs.sort(key=lambda pair: (-pair.similarity, pair.left_index, pair.right_index))
    return pairs[:max_pairs]
