"""Pipeline 配置模型与编译（YAML → Pydantic 校验 → 不可变 execution plan）。

编译流程：safe YAML 加载 -> Pydantic 类型校验 -> 引用/条件运算符检查 ->
生成不可变 execution plan 与 canonical digest。
- `when` 使用带类型的运算符白名单，禁止 eval；缺失特征记为 unknown（条件不命中）。
- first_success 为优先降级；sequence 为顺序增强（required 步骤失败使阶段失败）。
- 热更新 = 校验后发布不可变快照，仅影响新任务；既有任务钉住 effective_config_digest。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from owli_parser.core.errors import ConfigInvalidError, UnsupportedFormatError
from owli_parser.modules.parsing.adapters_base import ProbeFeatures

PIPELINE_SCHEMA_VERSION = "1.0"

_ALLOWED_OPERATORS: dict[str, Any] = {
    "gte": lambda a, b: a is not None and a >= b,
    "lte": lambda a, b: a is not None and a <= b,
    "gt": lambda a, b: a is not None and a > b,
    "lt": lambda a, b: a is not None and a < b,
    "eq": lambda a, b: a is not None and a == b,
    "ne": lambda a, b: a is not None and a != b,
    "in": lambda a, b: a is not None and a in b,
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WhenCondition(_Strict):
    feature: str
    operator: Literal["gte", "lte", "gt", "lt", "eq", "ne", "in"]
    value: Any

    def evaluate(self, features: ProbeFeatures) -> tuple[bool, bool]:
        """返回 (hit, known)。特征缺失时 known=False，条件不命中。"""
        actual = getattr(features, self.feature, None)
        if actual is None:
            return False, False
        fn = _ALLOWED_OPERATORS[self.operator]
        try:
            return bool(fn(actual, self.value)), True
        except TypeError:
            return False, True


class PipelineStep(_Strict):
    adapter: str
    required: bool = True
    when: dict[str, dict[str, Any]] | None = None

    def parsed_when(self) -> list[WhenCondition]:
        if not self.when:
            return []
        conditions: list[WhenCondition] = []
        for feature, spec in self.when.items():
            if not isinstance(spec, dict) or len(spec) != 1:
                raise ConfigInvalidError(f"when 条件形状不合法: {feature}")
            operator, value = next(iter(spec.items()))
            if operator not in _ALLOWED_OPERATORS:
                raise ConfigInvalidError(f"when 运算符不在白名单: {operator}")
            conditions.append(
                WhenCondition(
                    feature=feature,
                    operator=cast(Literal["gte", "lte", "gt", "lt", "eq", "ne", "in"], operator),
                    value=value,
                )
            )
        return conditions


class FormatPipeline(_Strict):
    mode: Literal["first_success", "sequence"]
    candidates: list[PipelineStep] = Field(default_factory=list)
    steps: list[PipelineStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_shape(self) -> FormatPipeline:
        if self.mode == "first_success":
            if not self.candidates:
                raise ValueError("first_success 模式需要非空 candidates")
            if self.steps:
                raise ValueError("first_success 模式不得使用 steps")
        if self.mode == "sequence":
            if not self.steps:
                raise ValueError("sequence 模式需要非空 steps")
            if self.candidates:
                raise ValueError("sequence 模式不得使用 candidates")
        return self

    def all_steps(self) -> list[PipelineStep]:
        return list(self.candidates) + list(self.steps)


class PipelineDocument(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    name: str
    revision: int = Field(ge=1)
    pipelines: dict[str, FormatPipeline]

    @model_validator(mode="after")
    def _check_adapters_unique(self) -> PipelineDocument:
        for fmt, pipeline in self.pipelines.items():
            names = [s.adapter for s in pipeline.all_steps()]
            if len(names) != len(set(names)):
                raise ValueError(f"pipeline {fmt} 中 adapter 重复")
        return self


class CompiledPipeline:
    """不可变编译产物：execution plan + canonical digest。"""

    def __init__(self, document: PipelineDocument, yaml_text: str) -> None:
        self.document = document
        canonical = json.dumps(
            document.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        self.digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.yaml_text = yaml_text
        # 预解析 when 条件（编译期固定结构）
        self._conditions: dict[str, list[list[WhenCondition]]] = {}
        for fmt, pipeline in document.pipelines.items():
            self._conditions[fmt] = [step.parsed_when() for step in pipeline.all_steps()]

    def pipeline_for(self, fmt: str) -> FormatPipeline | None:
        return self.document.pipelines.get(fmt)

    def conditions_for(self, fmt: str, step_index: int) -> list[WhenCondition]:
        return self._conditions[fmt][step_index]

    def execution_plan(self) -> dict[str, Any]:
        """冻结进任务的执行计划（路由只读取该快照）。"""
        return {
            "pipeline_name": self.document.name,
            "pipeline_revision": self.document.revision,
            "config_digest": self.digest,
            "pipelines": {
                fmt: {
                    "mode": p.mode,
                    "steps": [{"adapter": s.adapter, "required": s.required} for s in p.all_steps()],
                }
                for fmt, p in self.document.pipelines.items()
            },
        }


def compile_pipeline(yaml_text: str, known_adapters: set[str]) -> CompiledPipeline:
    """编译并做引用检查；未知 adapter 为配置错误（配置应只发布已安装分支）。"""
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ConfigInvalidError("pipeline YAML 解析失败") from exc
    if not isinstance(data, dict):
        raise ConfigInvalidError("pipeline 顶层必须是对象")
    try:
        document = PipelineDocument.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigInvalidError(f"pipeline 校验失败: {exc}") from exc
    for fmt, pipeline in document.pipelines.items():
        for step in pipeline.all_steps():
            if step.adapter not in known_adapters:
                raise ConfigInvalidError(f"pipeline {fmt} 引用了未注册 adapter: {step.adapter}")
    return CompiledPipeline(document, yaml_text)


def load_default_pipeline_yaml() -> str:
    from importlib.resources import files

    return Path(str(files("owli_parser.resources").joinpath("default_pipeline.yaml"))).read_text(
        encoding="utf-8"
    )


def detect_format(features: ProbeFeatures) -> str:
    """按扩展名/MIME 映射到 pipeline format key。"""
    ext = features.extension.lower()
    mime = features.mime or ""
    if ext == "pdf" or mime == "application/pdf":
        return "pdf"
    if ext == "docx":
        return "docx"
    if ext == "pptx":
        return "pptx"
    if ext in {"xlsx", "xlsm"}:
        return "xlsx"
    # EPUB/IPYNB 先于通用 ZIP 判定（EPUB 的 magic 也是 PK；ipynb 可能被识别为 text）
    if ext == "epub" or mime == "application/epub+zip":
        return "epub"
    if ext == "ipynb" or mime == "application/x-ipynb+json":
        return "jupyter"
    if ext == "zip" or mime == "application/zip":
        return "zip"
    if ext == "eml" or mime == "message/rfc822":
        return "email"
    if ext in {"md", "markdown"}:
        return "markdown"
    if ext == "csv" or mime == "text/csv":
        return "csv"
    if mime.startswith("text/") or ext in {"txt", "log", "rst", "tex", "json", "yaml", "yml"}:
        return "text"
    return ext or "unknown"


class RouteDecision:
    def __init__(
        self,
        *,
        fmt: str,
        adapter_name: str | None,
        mode: Literal["first_success", "sequence"],
        reason: str,
        skipped: list[tuple[str, str]],
    ) -> None:
        self.fmt = fmt
        self.adapter_name = adapter_name
        self.mode = mode
        self.reason = reason
        self.skipped = skipped


def explain_route(
    compiled: CompiledPipeline,
    fmt: str,
    features: ProbeFeatures,
    supports: dict[str, bool],
) -> RouteDecision:
    """路由解释：始终返回结构化决策（无可用分支时 adapter_name=None，不抛错）。

    dry-run 需要逐候选的降级原因；实际执行仍走 route_first_success（无分支即失败）。
    """
    pipeline = compiled.pipeline_for(fmt)
    if pipeline is None:
        return RouteDecision(
            fmt=fmt,
            adapter_name=None,
            mode="first_success",
            reason=f"没有针对格式 {fmt} 的 pipeline 分支",
            skipped=[],
        )
    if pipeline.mode != "first_success":
        return RouteDecision(
            fmt=fmt,
            adapter_name=None,
            mode=pipeline.mode,
            reason=f"该 pipeline 为 {pipeline.mode} 模式，不是 first_success",
            skipped=[],
        )
    skipped: list[tuple[str, str]] = []
    for index, step in enumerate(pipeline.candidates):
        conditions = compiled.conditions_for(fmt, index)
        hit = True
        unknown_features: list[str] = []
        for condition in conditions:
            condition_hit, known = condition.evaluate(features)
            if not known:
                unknown_features.append(condition.feature)
            if not condition_hit:
                hit = False
                break
        if unknown_features and not hit:
            skipped.append((step.adapter, f"特征缺失: {','.join(unknown_features)}"))
            continue
        if not hit:
            skipped.append((step.adapter, "when 条件不命中"))
            continue
        if not supports.get(step.adapter, False):
            skipped.append((step.adapter, "adapter 不支持该输入"))
            continue
        return RouteDecision(
            fmt=fmt,
            adapter_name=step.adapter,
            mode="first_success",
            reason=f"候选 {step.adapter} 命中",
            skipped=skipped,
        )
    return RouteDecision(
        fmt=fmt,
        adapter_name=None,
        mode="first_success",
        reason=f"格式 {fmt} 没有可执行的解析分支",
        skipped=skipped,
    )


def route_first_success(
    compiled: CompiledPipeline,
    fmt: str,
    features: ProbeFeatures,
    supports: dict[str, bool],
) -> RouteDecision:
    """first_success 路由：返回第一个条件命中且支持的候选；无分支即失败。"""
    decision = explain_route(compiled, fmt, features, supports)
    if decision.adapter_name is None:
        raise UnsupportedFormatError(f"{decision.reason}：{decision.skipped}")
    return decision
