"""统一 IR Schema 2.0（核心契约）。

关键约定（见文档后端 §7 与第四部分 §2）：
- bbox 为原页面视觉方向的左上原点、右下方向、0–1 归一化；
  page_index 从 0 开始，UI 页码另算。
- SourceRef 为判别联合；未映射位置不伪造 bbox，使用文件级 artifact 引用。
- BlockPayload 按 type 校验；扩展字段必须 namespaced、有限大小。
- Confidence 未校准得分不能跨 adapter 比较，缺失不是 1.0。
- acl_ref 首期引用数据集授权，文档/block 继承。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IR_SCHEMA_VERSION = "2.0"


def _no_extra() -> ConfigDict:
    return ConfigDict(extra="forbid", frozen=True)


class BBox(BaseModel):
    """归一化边界框：0≤x0<x1≤1、0≤y0<y1≤1。"""

    model_config = _no_extra()

    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_order(self) -> BBox:
        if not self.x0 < self.x1:
            raise ValueError("bbox 需满足 x0 < x1")
        if not self.y0 < self.y1:
            raise ValueError("bbox 需满足 y0 < y1")
        return self


# ---------------------------------------------------------------------------
# SourceRef 判别联合（artifact_id + kind 为共同字段）
# ---------------------------------------------------------------------------


class _SourceRefBase(BaseModel):
    model_config = _no_extra()

    artifact_id: str  # 来源 artifact（必须属于当前租户并与版本关联）


class PageRegionSourceRef(_SourceRefBase):
    kind: Literal["page_region"] = "page_region"
    page_index: int = Field(ge=0)
    geometry_id: str
    bbox: BBox
    polygon: list[tuple[float, float]] | None = None


class TextSpanSourceRef(_SourceRefBase):
    kind: Literal["text_span"] = "text_span"
    text_artifact_id: str
    start: int = Field(ge=0, description="Unicode code point 偏移，0 基半开")
    end: int = Field(ge=0)
    normalization_revision: str
    part: str | None = None
    path: str | None = None
    node_id: str | None = None

    @model_validator(mode="after")
    def _check_span(self) -> TextSpanSourceRef:
        if self.end < self.start:
            raise ValueError("text_span 需满足 start <= end")
        return self


class TableRegionSourceRef(_SourceRefBase):
    kind: Literal["table_region"] = "table_region"
    table_id: str
    sheet_name: str | None = None
    row_start: int = Field(ge=0)
    row_end: int = Field(ge=0)
    col_start: int = Field(ge=0)
    col_end: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_range(self) -> TableRegionSourceRef:
        if self.row_end < self.row_start or self.col_end < self.col_start:
            raise ValueError("table_region 需满足 start <= end（0 基半开区间）")
        return self


class MediaSpanSourceRef(_SourceRefBase):
    kind: Literal["media_span"] = "media_span"
    track_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    timebase_revision: str

    @model_validator(mode="after")
    def _check_time(self) -> MediaSpanSourceRef:
        if self.end_ms <= self.start_ms:
            raise ValueError("media_span 需满足 start_ms < end_ms")
        return self


class AttachmentSourceRef(_SourceRefBase):
    kind: Literal["attachment"] = "attachment"
    parent_artifact_id: str
    entry_id: str
    child_artifact_id: str


class ArtifactSourceRef(_SourceRefBase):
    """显式文件级降级来源；不能伪造页坐标。"""

    kind: Literal["artifact"] = "artifact"
    reason: str


SourceRef = Annotated[
    PageRegionSourceRef
    | TextSpanSourceRef
    | TableRegionSourceRef
    | MediaSpanSourceRef
    | AttachmentSourceRef
    | ArtifactSourceRef,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# BlockPayload 判别联合
# ---------------------------------------------------------------------------


class TextPayload(BaseModel):
    model_config = _no_extra()

    payload_type: Literal["text"] = "text"
    level: int | None = None  # heading 层级
    heading_path: list[str] = Field(default_factory=list)
    list_ordered: bool | None = None
    code_language: str | None = None


class TableCell(BaseModel):
    model_config = _no_extra()

    text: str
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)
    is_header: bool = False


class TablePayload(BaseModel):
    """表格结构真值；table_html 仅为派生展示，不替代结构。"""

    model_config = _no_extra()

    payload_type: Literal["table"] = "table"
    rows: list[list[TableCell]] = Field(default_factory=list)
    header_row_count: int = Field(default=0, ge=0)
    caption: str | None = None
    table_html: str | None = None


class FormulaPayload(BaseModel):
    model_config = _no_extra()

    payload_type: Literal["formula"] = "formula"
    latex: str
    confidence_note: str | None = None


class ImagePayload(BaseModel):
    model_config = _no_extra()

    payload_type: Literal["image"] = "image"
    image_artifact_id: str | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    caption: str | None = None


class FigureDescPayload(BaseModel):
    """VLM/模型生成的图片描述：必须带派生声明与模型 revision。"""

    model_config = _no_extra()

    payload_type: Literal["figure_desc"] = "figure_desc"
    image_artifact_id: str | None = None
    derived: bool = True
    model_revision: str
    prompt_revision: str
    input_refs: list[str] = Field(default_factory=list)


class AudioSegPayload(BaseModel):
    model_config = _no_extra()

    payload_type: Literal["audio_seg"] = "audio_seg"
    track_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker: str | None = None  # 仅在已启用且验证过的 diarization 管线产生


class VideoFramePayload(BaseModel):
    model_config = _no_extra()

    payload_type: Literal["video_frame"] = "video_frame"
    frame_artifact_id: str | None = None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


BlockPayload = Annotated[
    TextPayload
    | TablePayload
    | FormulaPayload
    | ImagePayload
    | FigureDescPayload
    | AudioSegPayload
    | VideoFramePayload,
    Field(discriminator="payload_type"),
]

_PAYLOAD_BY_BLOCK_TYPE: dict[str, str] = {
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

BlockType = Literal[
    "text",
    "heading",
    "table",
    "formula",
    "image",
    "figure_desc",
    "audio_seg",
    "video_frame",
    "list",
    "page_header",
    "page_footer",
    "footnote",
    "code",
]


# ---------------------------------------------------------------------------
# 置信度、来源与几何
# ---------------------------------------------------------------------------


class Confidence(BaseModel):
    model_config = _no_extra()

    raw_score: float = Field(ge=0.0, le=1.0)
    normalized_score: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str
    calibration_revision: str | None = None


class Provenance(BaseModel):
    model_config = _no_extra()

    adapter_name: str
    adapter_version: str
    adapter_mode: Literal["local", "remote", "composite"]
    engine: str | None = None
    model_revision: str | None = None
    prompt_revision: str | None = None
    generated_by_model: bool = False


class PageGeometry(BaseModel):
    model_config = _no_extra()

    page_index: int = Field(ge=0)
    geometry_id: str
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: Literal[0, 90, 180, 270] = 0
    width_unit: Literal["pt", "px"] = "pt"
    crop_box: BBox | None = None
    coordinate_transform: dict[str, float] | None = Field(
        default=None, description="转换产物的显式坐标变换参数；已归一化视觉坐标不得重复应用旋转"
    )


class CoverageReport(BaseModel):
    """coverage = 成功工作单元 / 预检确认的总工作单元。

    PDF 按页、媒体按时长加权；不得以解析后才知道的 block 数为分母。
    """

    model_config = _no_extra()

    unit_kind: Literal["page", "time_window", "document"]
    total_units: int = Field(ge=0)
    succeeded_units: int = Field(ge=0)
    failed_units: int = Field(ge=0)
    missing_units: list[str] = Field(default_factory=list)
    total_duration_ms: int | None = Field(default=None, ge=0)
    failed_duration_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> CoverageReport:
        if self.succeeded_units + self.failed_units != self.total_units:
            raise ValueError("coverage 单元数不自洽：succeeded + failed != total")
        return self

    @property
    def coverage(self) -> float:
        if self.total_units == 0:
            return 1.0
        return self.succeeded_units / self.total_units


class QualityReport(BaseModel):
    model_config = _no_extra()

    metrics: dict[str, float] = Field(default_factory=dict)
    algorithm_revision: str
    notes: str | None = None


# ---------------------------------------------------------------------------
# Block 与 DocumentIR
# ---------------------------------------------------------------------------


class Block(BaseModel):
    model_config = _no_extra()

    id: str
    type: BlockType
    order: int = Field(ge=0)
    parent_id: str | None = None
    text: str
    lang: str | None = None
    source_refs: list[SourceRef] = Field(min_length=1)
    payload: BlockPayload
    confidence: Confidence | None = None
    provenance: Provenance
    acl_ref: str

    @model_validator(mode="after")
    def _check_payload_type(self) -> Block:
        expected = _PAYLOAD_BY_BLOCK_TYPE[self.type]
        actual = getattr(self.payload, "payload_type", None)
        if expected != actual:
            raise ValueError(f"block.type={self.type} 与 payload_type={actual} 不一致")
        return self


class DocumentIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    doc_id: str
    version_id: str
    dataset_id: str
    tenant_id: str
    content_hash: str
    effective_config_digest: str
    langs: list[str] = Field(default_factory=list)
    pages: list[PageGeometry] = Field(default_factory=list)
    blocks: list[Block]
    coverage: CoverageReport
    quality: QualityReport

    @model_validator(mode="after")
    def _check_invariants(self) -> DocumentIR:
        ids = [b.id for b in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("block_id 必须唯一")
        id_set = set(ids)
        for block in self.blocks:
            if block.parent_id is not None and block.parent_id not in id_set:
                raise ValueError(f"block {block.id} 的 parent_id 不存在")
        # parent 图无环检查
        parent_map = {b.id: b.parent_id for b in self.blocks}
        for start in ids:
            seen: set[str] = set()
            cursor: str | None = start
            while cursor is not None:
                if cursor in seen:
                    raise ValueError("block parent 图存在环")
                seen.add(cursor)
                cursor = parent_map.get(cursor)
        geometry_ids = {g.geometry_id for g in self.pages}
        for block in self.blocks:
            for ref in block.source_refs:
                if isinstance(ref, PageRegionSourceRef) and ref.geometry_id not in geometry_ids:
                    raise ValueError(f"block {block.id} 的 page_region.geometry_id 未在 pages 中定义")
        return self


__all__ = [
    "IR_SCHEMA_VERSION",
    "ArtifactSourceRef",
    "AttachmentSourceRef",
    "AudioSegPayload",
    "BBox",
    "Block",
    "BlockPayload",
    "BlockType",
    "Confidence",
    "CoverageReport",
    "DocumentIR",
    "FigureDescPayload",
    "FormulaPayload",
    "ImagePayload",
    "MediaSpanSourceRef",
    "PageGeometry",
    "PageRegionSourceRef",
    "Provenance",
    "QualityReport",
    "SourceRef",
    "TablePayload",
    "TableRegionSourceRef",
    "TextPayload",
    "TextSpanSourceRef",
    "VideoFramePayload",
]
