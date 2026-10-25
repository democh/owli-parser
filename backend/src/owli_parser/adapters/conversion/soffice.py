"""LibreOffice（soffice）转换适配器：Office → PDF。

- 二进制发现：显式路径或 PATH 探测；缺失时 available()=False，
  convert 抛 ConversionUnavailable（不假装成功，调用方回退 L4 兜底）；
- 命令在隔离 profile 下执行（-env:UserInstallation），避免并发锁冲突；
- runner 可注入（contract suite 用 fake 验证命令构造、超时与非零退出映射）；
- page_count 用 pypdf 读取；pypdf 缺失或读取失败时为 None（不伪造）。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from owli_parser.ports.conversion import (
    ConversionError,
    ConversionResult,
    ConversionUnavailable,
    UnsupportedConversion,
)

# runner 签名：argv + timeout → 类 CompletedProcess（returncode/stdout/stderr）
ConversionRunner = Callable[[list[str], float], Any]


def _default_runner(argv: list[str], timeout: float) -> Any:
    return subprocess.run(  # noqa: S603 —— argv 由本适配器构造，不经 shell
        argv,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


class SofficeConverter:
    """DocumentConverter 实现（LibreOffice headless）。"""

    extensions = frozenset({".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"})

    def __init__(
        self,
        *,
        binary: str | None = None,
        runner: ConversionRunner | None = None,
    ) -> None:
        self._binary = binary or shutil.which("soffice") or shutil.which("libreoffice")
        self._runner = runner or _default_runner

    def available(self) -> bool:
        return self._binary is not None

    def engine_info(self) -> tuple[str, str | None]:
        if self._binary is None:
            return ("libreoffice", None)
        try:
            proc = self._runner([self._binary, "--version"], 15.0)
            text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
            version = text.splitlines()[0] if text else None
            return ("libreoffice", version)
        except Exception:
            return ("libreoffice", None)

    def convert(
        self, source_path: Path, out_dir: Path, *, timeout_seconds: float = 120.0
    ) -> ConversionResult:
        if self._binary is None:
            raise ConversionUnavailable("LibreOffice 未安装（soffice 不在 PATH）")
        if source_path.suffix.lower() not in self.extensions:
            raise UnsupportedConversion(f"不支持的转换格式：{source_path.suffix}")
        if not source_path.is_file():
            raise ConversionError("源文件不存在")

        out_dir.mkdir(parents=True, exist_ok=True)
        profile_dir = out_dir / ".lo-profile"
        argv = [
            self._binary,
            "--headless",
            "--norestore",
            f"-env:UserInstallation=file://{profile_dir}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(source_path),
        ]
        try:
            proc = self._runner(argv, timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(f"转换超时（>{timeout_seconds:.0f}s）") from exc

        if proc.returncode != 0:
            # 错误信息截断（不含源文件绝对路径之外的隐私内容）
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")[:400]
            raise ConversionError(f"转换失败（exit={proc.returncode}）：{stderr.strip()}")

        pdf_path = out_dir / f"{source_path.stem}.pdf"
        if not pdf_path.is_file():
            raise ConversionError("转换产物缺失（未生成 PDF）")

        return ConversionResult(
            source_path=source_path,
            pdf_path=pdf_path,
            page_count=_read_page_count(pdf_path),
            engine="libreoffice",
            engine_version=self.engine_info()[1],
        )


def _read_page_count(pdf_path: Path) -> int | None:
    """读取页数；pypdf 缺失/损坏文件时为 None（不伪造数字）。"""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return None
