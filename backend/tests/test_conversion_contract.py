"""Office 转换适配器 contract suite：命令构造、错误映射与产物校验语义。

以 fake runner 注入锁定行为（不触真实 LibreOffice/不联网）：
- 二进制度缺失 → available()=False 且 convert 抛 ConversionUnavailable（不假装成功）；
- 成功路径校验 argv（headless/convert-to pdf/隔离 profile/outdir）与产物读取；
- 非零退出、超时、产物缺失、不支持格式、源文件不存在各自映射到明确错误；
- page_count 读取失败时为 None（不伪造数字）。
真实转换链路（二进制/字体/宏）需安装 LibreOffice 的环境独立验收。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from owli_parser.adapters.conversion.soffice import SofficeConverter
from owli_parser.ports.conversion import (
    ConversionError,
    ConversionUnavailable,
    UnsupportedConversion,
)


class _Proc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _write_valid_pdf(path: Path, pages: int = 2) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)


class FakeRunner:
    """记录 argv；version 调用返回版本串；转换调用按脚本回调产出/失败。"""

    def __init__(self, *, on_convert=None, version: bytes = b"LibreOffice 24.2.1") -> None:
        self.calls: list[list[str]] = []
        self._on_convert = on_convert
        self._version = version

    def __call__(self, argv: list[str], timeout: float):
        self.calls.append(list(argv))
        if "--version" in argv:
            return _Proc(0, stdout=self._version)
        if self._on_convert is not None:
            return self._on_convert(argv, timeout)
        return _Proc(0)


def _make_source(tmp_path: Path) -> Path:
    source = tmp_path / "deck.docx"
    source.write_bytes(b"fake-docx-bytes")
    return source


def test_unavailable_binary_is_explicit(tmp_path, monkeypatch) -> None:
    # 模拟 PATH 中无 soffice/libreoffice（不依赖开发机环境）
    monkeypatch.setattr("owli_parser.adapters.conversion.soffice.shutil.which", lambda name: None)
    converter = SofficeConverter(runner=FakeRunner())
    assert converter.available() is False
    assert converter.engine_info() == ("libreoffice", None)
    with pytest.raises(ConversionUnavailable):
        converter.convert(_make_source(tmp_path), tmp_path / "out")


def test_success_path_command_construction_and_page_count(tmp_path) -> None:
    out_dir = tmp_path / "out"

    def on_convert(argv: list[str], timeout: float):
        # 模拟 LibreOffice 行为：按 --outdir 与源文件 stem 产出 PDF
        outdir = Path(argv[argv.index("--outdir") + 1])
        sample = Path(argv[-1])
        _write_valid_pdf(outdir / f"{sample.stem}.pdf", pages=2)
        return _Proc(0)

    runner = FakeRunner(on_convert=on_convert)
    converter = SofficeConverter(binary="/usr/bin/soffice", runner=runner)
    assert converter.available() is True

    result = converter.convert(_make_source(tmp_path), out_dir, timeout_seconds=30.0)
    assert result.pdf_path == out_dir / "deck.pdf"
    assert result.page_count == 2  # pypdf 真实读取
    assert result.engine == "libreoffice"
    assert result.engine_version == "LibreOffice 24.2.1"

    convert_argv = next(argv for argv in runner.calls if "--convert-to" in argv)
    assert "--headless" in convert_argv and "--norestore" in convert_argv
    assert convert_argv[convert_argv.index("--convert-to") + 1] == "pdf"
    assert any(arg.startswith("-env:UserInstallation=file://") for arg in convert_argv), (
        "必须在隔离 profile 下执行，避免并发锁冲突"
    )
    assert convert_argv[-1].endswith("deck.docx")


def test_nonzero_exit_maps_to_conversion_error(tmp_path) -> None:
    runner = FakeRunner(on_convert=lambda argv, timeout: _Proc(1, stderr=b"source file could not be loaded"))
    converter = SofficeConverter(binary="/usr/bin/soffice", runner=runner)
    with pytest.raises(ConversionError) as excinfo:
        converter.convert(_make_source(tmp_path), tmp_path / "out")
    assert "exit=1" in str(excinfo.value)
    assert "could not be loaded" in str(excinfo.value)


def test_timeout_maps_to_conversion_error(tmp_path) -> None:
    def on_convert(argv: list[str], timeout: float):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    converter = SofficeConverter(binary="/usr/bin/soffice", runner=FakeRunner(on_convert=on_convert))
    with pytest.raises(ConversionError, match="超时"):
        converter.convert(_make_source(tmp_path), tmp_path / "out", timeout_seconds=5.0)


def test_success_without_artifact_is_rejected(tmp_path) -> None:
    converter = SofficeConverter(binary="/usr/bin/soffice", runner=FakeRunner())
    with pytest.raises(ConversionError, match="产物缺失"):
        converter.convert(_make_source(tmp_path), tmp_path / "out")


def test_unsupported_extension_and_missing_source(tmp_path) -> None:
    converter = SofficeConverter(binary="/usr/bin/soffice", runner=FakeRunner())
    txt = tmp_path / "note.txt"
    txt.write_text("not office")
    with pytest.raises(UnsupportedConversion):
        converter.convert(txt, tmp_path / "out")
    with pytest.raises(ConversionError, match="源文件不存在"):
        converter.convert(tmp_path / "missing.docx", tmp_path / "out")


def test_invalid_pdf_page_count_is_none(tmp_path) -> None:
    out_dir = tmp_path / "out"

    def on_convert(argv: list[str], timeout: float):
        outdir = Path(argv[argv.index("--outdir") + 1])
        (outdir / f"{Path(argv[-1]).stem}.pdf").write_bytes(b"%not-a-valid-pdf")
        return _Proc(0)

    converter = SofficeConverter(binary="/usr/bin/soffice", runner=FakeRunner(on_convert=on_convert))
    result = converter.convert(_make_source(tmp_path), out_dir)
    assert result.pdf_path.is_file()
    assert result.page_count is None  # 读取失败不伪造数字
