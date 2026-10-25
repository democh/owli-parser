"""ParserAdapter 协议与解析结果数据类型。

- Adapter 输出 ParseOutcome（blocks、单位级错误、覆盖率及来源），规范化层统一
  生成 DocumentIR；adapter 不得自行决定租户身份、ACL 和业务版本。
- 重 CPU/GPU 同步调用运行在 worker 子进程（本模块实现均为同步）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class SourceHandle:
    """已落地的本地输入文件（解析器默认无网络）。"""

    path: Path
    mime: str | None
    filename: str
    content_hash: str
    artifact_id: str


@dataclass(frozen=True, slots=True)
class ProbeFeatures:
    """预检特征：路由与条件评估使用；缺失特征记为 None（unknown）。"""

    mime: str | None
    extension: str
    size_bytes: int
    text_layer_ratio: float | None = None
    page_count: int | None = None
    encrypted: bool | None = None
    has_zip_container: bool | None = None
    language_hint: str | None = None
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SupportDecision:
    supported: bool
    reason: str | None = None


@dataclass
class ExtractedBlock:
    type: str
    text: str
    source_ref: dict[str, object]
    payload: dict[str, object] = field(default_factory=dict)
    parent_index: int | None = None
    lang: str | None = None
    confidence_raw: float | None = None
    confidence_method: str | None = None


@dataclass
class ExtractedPage:
    page_index: int
    width: float
    height: float
    rotation: int = 0
    width_unit: str = "pt"


@dataclass
class UnitError:
    unit_id: str
    code: str
    message: str
    retryable: bool = False


@dataclass
class ParseOutcome:
    blocks: list[ExtractedBlock]
    pages: list[ExtractedPage] = field(default_factory=list)
    unit_errors: list[UnitError] = field(default_factory=list)
    total_units: int = 1
    succeeded_units: int = 1
    unit_kind: Literal["page", "time_window", "document"] = "document"
    engine: str = "unknown"
    engine_version: str = "0"
    warnings: list[str] = field(default_factory=list)

    def add_unit_error(self, unit_id: str, code: str, message: str, *, retryable: bool = False) -> None:
        self.unit_errors.append(UnitError(unit_id=unit_id, code=code, message=message, retryable=retryable))


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """执行上下文：deadline、取消回调与进度回调。"""

    deadline_epoch: float
    cancellation: Callable[[], bool]
    progress: Callable[[int, int], None] | None = None

    def check_cancelled(self) -> None:
        if self.cancellation():
            raise ParseCancelledError()


class ParseCancelledError(Exception):
    """调用方请求取消（释放资源并阻止发布）。"""


class ParseFailedError(Exception):
    """不可恢复解析错误。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ParserAdapter(Protocol):
    name: str
    version: str
    mode: Literal["local", "remote", "composite"]

    def supports(self, features: ProbeFeatures) -> SupportDecision: ...

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome: ...

    def capabilities(self) -> dict[str, object]: ...
