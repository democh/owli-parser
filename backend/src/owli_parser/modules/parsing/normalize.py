"""IR 规范化：ParseOutcome → DocumentIR。

- 统一补齐来源锚点的 artifact_id / normalization_revision 等公共字段；
  文本块偏移为块内偏移（normalization_revision=block-local），不伪装原件精确偏移。
- coverage 以预检确认的工作单元为分母；缺失单位显式列出。
- 质量报告独立于置信度：空块率、单位错误数、平均置信度分开报告。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, cast

from owli_parser.contracts.ir import (
    ArtifactSourceRef,
    AttachmentSourceRef,
    Block,
    CoverageReport,
    DocumentIR,
    MediaSpanSourceRef,
    PageGeometry,
    PageRegionSourceRef,
    Provenance,
    QualityReport,
    TableRegionSourceRef,
    TextSpanSourceRef,
)
from owli_parser.modules.parsing.adapters_base import ParseOutcome
from owli_parser.modules.parsing.langdetect import detect_language
from owli_parser.modules.parsing.near_dup import find_near_duplicates

_QUALITY_REVISION = "quality-v3"  # v3：+近重复提示（只报告不删除）
# 精确去重的最小规范化长度（短文本如页码不去重）
_DEDUP_MIN_CHARS = 8
# 近重复扫描的块数上限（超出跳过并记 warning，诚实降级）
_NEAR_DUP_SCAN_LIMIT = 800
# notes 中列出的近重复明细上限
_NEAR_DUP_NOTES_LIMIT = 20
_NORMALIZATION_REVISION = "block-local-v1"

_TEXT_PAYLOAD_DEFAULT: dict[str, Any] = {"heading_path": []}


def build_document_ir(
    *,
    parse_outcome: ParseOutcome,
    adapter_name: str,
    adapter_version: str,
    adapter_mode: str,
    doc_id: uuid.UUID,
    version_id: uuid.UUID,
    dataset_id: uuid.UUID,
    tenant_id: uuid.UUID,
    content_hash: str,
    effective_config_digest: str,
    source_artifact_id: uuid.UUID,
    langs: list[str] | None = None,
) -> DocumentIR:
    pages = [
        PageGeometry(
            page_index=p.page_index,
            geometry_id=f"page-{p.page_index}",
            width=p.width,
            height=p.height,
            rotation=(cast(Literal[0, 90, 180, 270], p.rotation) if p.rotation in (0, 90, 180, 270) else 0),
            width_unit=(cast(Literal["pt", "px"], p.width_unit) if p.width_unit in ("pt", "px") else "pt"),
        )
        for p in parse_outcome.pages
    ]
    geometry_ids = {g.geometry_id for g in pages}
    blocks: list[Block] = []
    # 精确去重：规范化（空白折叠）后完全相等且长度达下限的块仅保留首个
    # （页眉页脚等重复内容；过短文本如页码不去重以避免误删）
    seen_texts: set[str] = set()
    duplicate_removed = 0
    for extracted in parse_outcome.blocks:
        normalized_text = " ".join(extracted.text.split())
        if len(normalized_text) >= _DEDUP_MIN_CHARS:
            if normalized_text in seen_texts:
                duplicate_removed += 1
                continue
            seen_texts.add(normalized_text)
        order = len(blocks)
        block_id = f"b-{order:04d}"
        source_ref = _build_source_ref(
            extracted.source_ref,
            artifact_id=str(source_artifact_id),
            geometry_ids=geometry_ids,
            text=extracted.text,
        )
        payload = dict(extracted.payload) if extracted.payload else dict(_TEXT_PAYLOAD_DEFAULT)
        payload.setdefault("payload_type", _payload_type_for(extracted.type))
        confidence = None
        if extracted.confidence_raw is not None:
            from owli_parser.contracts.ir import Confidence

            confidence = Confidence(
                raw_score=max(0.0, min(1.0, extracted.confidence_raw)),
                method=extracted.confidence_method or "uncalibrated",
                calibration_revision=None,
            )
        blocks.append(
            Block(
                id=block_id,
                type=extracted.type,  # type: ignore[arg-type]
                order=order,
                parent_id=None,
                text=extracted.text,
                lang=extracted.lang or detect_language(extracted.text).lang,
                source_refs=[source_ref],
                payload=payload,  # type: ignore[arg-type]
                confidence=confidence,
                provenance=Provenance(
                    adapter_name=adapter_name,
                    adapter_version=adapter_version,
                    adapter_mode=adapter_mode,  # type: ignore[arg-type]
                    engine=parse_outcome.engine,
                ),
                acl_ref=f"dataset:{dataset_id}",
            )
        )

    failed_units = len({e.unit_id for e in parse_outcome.unit_errors})
    missing = sorted({e.unit_id for e in parse_outcome.unit_errors})
    coverage = CoverageReport(
        unit_kind=parse_outcome.unit_kind,
        total_units=max(parse_outcome.total_units, parse_outcome.succeeded_units),
        succeeded_units=parse_outcome.succeeded_units,
        failed_units=max(parse_outcome.total_units - parse_outcome.succeeded_units, 0),
        missing_units=missing,
    )
    empty_ratio = (
        sum(1 for b in parse_outcome.blocks if not b.text.strip()) / len(parse_outcome.blocks)
        if parse_outcome.blocks
        else 1.0
    )
    confidences = [b.confidence.raw_score for b in blocks if b.confidence is not None]
    # 乱码率：U+FFFD 替换字符占全部文本字符的比例（文档"质量报告保留乱码率"）
    total_chars = sum(len(b.text) for b in blocks)
    replacement_chars = sum(b.text.count("\ufffd") for b in blocks)
    replacement_ratio = replacement_chars / total_chars if total_chars else 0.0
    # 近重复提示（只报告不删除；精确重复已在块构建阶段去除）
    near_dup_pairs = []
    near_dup_note: list[str] = []
    if len(blocks) <= _NEAR_DUP_SCAN_LIMIT:
        near_dup_pairs = find_near_duplicates([b.text for b in blocks])
        near_dup_note = [
            f"near_duplicate {blocks[pair.left_index].id}~{blocks[pair.right_index].id} "
            f"({pair.similarity:.4f})"
            for pair in near_dup_pairs[:_NEAR_DUP_NOTES_LIMIT]
        ]
    else:
        near_dup_note = [f"near_duplicate_scan_skipped block_count>{_NEAR_DUP_SCAN_LIMIT}"]
    quality = QualityReport(
        metrics={
            "empty_block_ratio": round(empty_ratio, 6),
            "replacement_char_ratio": round(replacement_ratio, 6),
            "duplicate_block_ratio": (
                round(duplicate_removed / len(parse_outcome.blocks), 6) if parse_outcome.blocks else 0.0
            ),
            "near_duplicate_pair_count": float(len(near_dup_pairs)),
            "unit_error_count": float(failed_units),
            "block_count": float(len(parse_outcome.blocks)),
            "avg_block_confidence": (round(sum(confidences) / len(confidences), 6) if confidences else -1.0),
        },
        algorithm_revision=_QUALITY_REVISION,
        notes="; ".join([*(parse_outcome.warnings or []), *near_dup_note]) or None,
    )
    return DocumentIR(
        doc_id=str(doc_id),
        version_id=str(version_id),
        dataset_id=str(dataset_id),
        tenant_id=str(tenant_id),
        content_hash=content_hash,
        effective_config_digest=effective_config_digest,
        langs=langs or [],
        pages=pages,
        blocks=blocks,
        coverage=coverage,
        quality=quality,
    )


def _payload_type_for(block_type: str) -> str:
    mapping = {
        "text": "text",
        "heading": "text",
        "list": "text",
        "code": "text",
        "page_header": "text",
        "page_footer": "text",
        "footnote": "text",
        "table": "table",
        "formula": "formula",
        "image": "image",
        "figure_desc": "figure_desc",
        "audio_seg": "audio_seg",
        "video_frame": "video_frame",
    }
    return mapping.get(block_type, "text")


def _build_source_ref(
    raw: dict[str, Any],
    *,
    artifact_id: str,
    geometry_ids: set[str],
    text: str,
) -> Any:
    kind = raw.get("kind", "artifact")
    if kind == "page_region":
        page_index = int(raw.get("page_index", 0))
        geometry_id = str(raw.get("geometry_id", f"page-{page_index}"))
        if geometry_id not in geometry_ids:
            geometry_id = f"page-{page_index}"
        bbox = raw.get("bbox") or {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}
        return PageRegionSourceRef(
            artifact_id=artifact_id,
            page_index=page_index,
            geometry_id=geometry_id,
            bbox=bbox,  # type: ignore[arg-type]
        )
    if kind == "text_span":
        child_artifact = str(raw.get("text_artifact_id") or artifact_id)
        start = int(raw.get("start", 0))
        end = int(raw.get("end", len(text)))
        if end < start:
            end = start
        return TextSpanSourceRef(
            artifact_id=artifact_id,
            text_artifact_id=child_artifact,
            start=start,
            end=end,
            normalization_revision=_NORMALIZATION_REVISION,
            part=raw.get("part") if isinstance(raw.get("part"), str) else None,
            path=raw.get("path") if isinstance(raw.get("path"), str) else None,
        )
    if kind == "table_region":
        return TableRegionSourceRef(
            artifact_id=artifact_id,
            table_id=str(raw.get("table_id", "table-0")),
            sheet_name=raw.get("sheet_name") if isinstance(raw.get("sheet_name"), str) else None,
            row_start=int(raw.get("row_start", 0)),
            row_end=int(raw.get("row_end", 0)),
            col_start=int(raw.get("col_start", 0)),
            col_end=int(raw.get("col_end", 0)),
        )
    if kind == "media_span":
        return MediaSpanSourceRef(
            artifact_id=artifact_id,
            track_id=str(raw.get("track_id", "track-0")),
            start_ms=int(raw.get("start_ms", 0)),
            end_ms=max(int(raw.get("end_ms", 1)), int(raw.get("start_ms", 0)) + 1),
            timebase_revision=str(raw.get("timebase_revision", "v1")),
        )
    if kind == "attachment":
        return AttachmentSourceRef(
            artifact_id=artifact_id,
            parent_artifact_id=str(raw.get("parent_artifact_id", artifact_id)),
            entry_id=str(raw.get("entry_id", "0")),
            child_artifact_id=str(raw.get("child_artifact_id", artifact_id)),
        )
    return ArtifactSourceRef(artifact_id=artifact_id, reason="file-level source")
