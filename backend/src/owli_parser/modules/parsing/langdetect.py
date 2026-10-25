"""文本语言检测（后处理第 8 步；轻量脚本级替代方案）。

- 输出为脚本/粗语言标签，短文本与多脚本混合返回 unknown/mixed——
  "不强制归类"（文档 §456）；细粒度语言（拉丁语系）需 fastText 或
  已准入替代方案，本检测器不冒充；
- hint（用户/前置提示）优先于扫描结果（method="hint"）；
- 纯函数、无依赖、确定性：同一文本永远同一结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 字符类别（脚本）判定
_HAN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_HIRAGANA = re.compile(r"[\u3040-\u309f]")
_KATAKANA = re.compile(r"[\u30a0-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_ARABIC = re.compile(r"[\u0600-\u06ff]")
_LATIN = re.compile(r"[A-Za-z]")

# 有效字符下限：低于此长度不判定（返回 unknown）
_MIN_EFFECTIVE_CHARS = 8
# 主脚本占比阈值与"显著次脚本"阈值（mixed 判定）
_DOMINANT_RATIO = 0.8
_SIGNIFICANT_RATIO = 0.2


@dataclass(frozen=True, slots=True)
class LangDetection:
    lang: str | None  # zh/ja/ko/ru/ar/latin/mixed/unknown（None 等价 unknown 的 IR 表述）
    method: str  # hint / script-scan / too-short
    dominant_script: str | None


def _script_counts(text: str) -> dict[str, int]:
    return {
        "han": len(_HAN.findall(text)),
        "hiragana": len(_HIRAGANA.findall(text)),
        "katakana": len(_KATAKANA.findall(text)),
        "hangul": len(_HANGUL.findall(text)),
        "cyrillic": len(_CYRILLIC.findall(text)),
        "arabic": len(_ARABIC.findall(text)),
        "latin": len(_LATIN.findall(text)),
    }


def detect_language(text: str, *, hint: str | None = None) -> LangDetection:
    """检测文本语言（脚本级）。

    hint 为 BCP-47 式标签或粗语言名（zh/ja/ko/ru/ar/en…），直接采用；
    短于下限的有效文本返回 unknown（method=too-short）。
    """
    if hint:
        normalized = hint.strip().lower()
        if normalized:
            # hint 为 en/fr/es 等拉丁语系时归入 latin 标签（不冒充细粒度判定）
            return LangDetection(lang=normalized, method="hint", dominant_script=None)

    counts = _script_counts(text)
    total = sum(counts.values())
    if total < _MIN_EFFECTIVE_CHARS:
        return LangDetection(lang=None, method="too-short", dominant_script=None)

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    dominant_script, dominant_count = ordered[0]
    if dominant_count == 0:
        return LangDetection(lang=None, method="script-scan", dominant_script=None)
    dominant_ratio = dominant_count / total

    # 日文：假名（平/片）出现即判 ja（汉字为主但有假名也是日文）
    kana = counts["hiragana"] + counts["katakana"]
    if kana > 0 and (kana + counts["han"]) / total >= _DOMINANT_RATIO:
        return LangDetection(lang="ja", method="script-scan", dominant_script="han-kana")

    if dominant_ratio < _DOMINANT_RATIO:
        # 主脚本占比不足：mixed（不强制归类，不冒充细粒度判定）
        return LangDetection(lang="mixed", method="script-scan", dominant_script=dominant_script)

    mapping = {
        "han": "zh",
        "hangul": "ko",
        "cyrillic": "ru",
        "arabic": "ar",
        # 拉丁文本不猜具体语种：fastText 或准入替代方案负责细分
        "latin": "latin",
    }
    lang = mapping.get(dominant_script)
    return LangDetection(lang=lang, method="script-scan", dominant_script=dominant_script)
