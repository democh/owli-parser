"""输入预检：提取路由所需的 ProbeFeatures。

- 数字版 PDF 预检字体/文本覆盖/页级特征；text_layer_ratio 为采样启发式，
  不是质量保证（阈值 0.5 仅为待校准初始值，来自 defaults.yaml 时的调用方传入）。
- 缺失特征记为 None；when 条件对 unknown 不命中并记录原因。
"""

from __future__ import annotations

from pathlib import Path

from owli_parser.modules.ingestion import preflight as mime_preflight
from owli_parser.modules.parsing.adapters_base import ProbeFeatures, SourceHandle

_PDF_SAMPLE_PAGES = 10
_MIN_TEXT_CHARS = 20


def probe(handle: SourceHandle) -> ProbeFeatures:
    ext = handle.path.suffix.lower().lstrip(".")
    size = handle.path.stat().st_size if handle.path.exists() else 0
    head = _read_head(handle.path)
    mime = mime_preflight.detect_mime(head, handle.filename) or handle.mime
    features = ProbeFeatures(mime=mime, extension=ext, size_bytes=size)
    if ext == "pdf" or mime == "application/pdf":
        features = _probe_pdf(handle.path, features)
    features = ProbeFeatures(
        mime=features.mime,
        extension=features.extension,
        size_bytes=features.size_bytes,
        text_layer_ratio=features.text_layer_ratio,
        page_count=features.page_count,
        encrypted=features.encrypted,
        has_zip_container=features.has_zip_container,
        language_hint=features.language_hint,
        extra=features.extra,
    )
    return features


def _read_head(path: Path, size: int = 8192) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(size)


def _probe_pdf(path: Path, features: ProbeFeatures) -> ProbeFeatures:
    try:
        from pypdf import PdfReader
    except ImportError:
        return features
    try:
        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        encrypted = bool(reader.is_encrypted)
    except Exception:
        return features
    text_pages = 0
    sampled = min(page_count, _PDF_SAMPLE_PAGES)
    for index in range(sampled):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:  # noqa: S112 - 单页异常按无文本计，属预检容错
            continue
        if len(text.strip()) >= _MIN_TEXT_CHARS:
            text_pages += 1
    ratio = text_pages / sampled if sampled else None
    return ProbeFeatures(
        mime="application/pdf",
        extension="pdf",
        size_bytes=features.size_bytes,
        text_layer_ratio=ratio,
        page_count=page_count,
        encrypted=encrypted,
        extra={"sampled_pages": sampled},
    )
