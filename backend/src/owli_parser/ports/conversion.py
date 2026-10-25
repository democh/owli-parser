"""文档转换端口：Office 等格式 → PDF（预览与定位映射的前置）。

- 转换能力必须如实声明：二进制缺失/版本不符时 available()=False，
  convert 抛 ConversionUnavailable（不假装转换成功）；
- 转换产物 page_count 允许为 None（解析库缺失时不伪造数字）；
- 转换在隔离的临时目录中执行，不污染源文件目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ConversionResult:
    source_path: Path
    pdf_path: Path
    page_count: int | None  # None = 未能读取（不伪造）
    engine: str  # 例如 "libreoffice"
    engine_version: str | None


class ConversionError(Exception):
    """转换失败（含超时/产物缺失）。"""


class ConversionUnavailable(ConversionError):
    """转换器不可用（二进制缺失等）；调用方应回退为无转换路径。"""


class UnsupportedConversion(Exception):
    """源格式不在该转换器支持范围内。"""


class DocumentConverter(Protocol):
    extensions: frozenset[str]

    def available(self) -> bool: ...

    def engine_info(self) -> tuple[str, str | None]:
        """(engine 名称, 版本或 None)。"""
        ...

    def convert(
        self, source_path: Path, out_dir: Path, *, timeout_seconds: float = 120.0
    ) -> ConversionResult: ...
