"""契约导出：OpenAPI 快照与 IR/事件 JSON Schema（文档 §5 验证矩阵）。

- CI 工作流：`owli contracts check` 对比重新导出与已提交快照，未提交差异即失败；
- 输出稳定：sort_keys + 固定缩进 + ensure_ascii=False，保证 git diff 语义清晰；
- 快照不含运行期数据（仅 schema），无泄密面。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from owli_parser.contracts.events import EventEnvelope
from owli_parser.contracts.ir import DocumentIR

# 快照文件名 → schema 构建器（惰性导入 create_app，避免 CLI 启动即装配路由）
_CONTRACT_FILES: dict[str, str] = {
    "openapi.json": "openapi",
    "ir.schema.json": "ir",
    "event.schema.json": "event",
}


def _build_openapi() -> dict[str, Any]:
    from owli_parser.bootstrap.app import create_app

    app = create_app()
    return app.openapi()


def _build_ir_schema() -> dict[str, Any]:
    return DocumentIR.model_json_schema()


def _build_event_schema() -> dict[str, Any]:
    return EventEnvelope.model_json_schema()


_BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "openapi": _build_openapi,
    "ir": _build_ir_schema,
    "event": _build_event_schema,
}


def render_contract(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build_contracts() -> dict[str, str]:
    """构建全部契约文本（文件名 → 内容）。"""
    return {filename: render_contract(_BUILDERS[kind]()) for filename, kind in _CONTRACT_FILES.items()}


def export_contracts(outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, content in build_contracts().items():
        target = outdir / filename
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written


def check_contracts(outdir: Path) -> list[str]:
    """对比磁盘快照；返回差异描述列表（空表示一致）。"""
    diffs: list[str] = []
    for filename, content in build_contracts().items():
        target = outdir / filename
        if not target.exists():
            diffs.append(f"{filename}: 快照缺失（请运行 owli contracts export）")
            continue
        if target.read_text(encoding="utf-8") != content:
            diffs.append(f"{filename}: 与当前代码不一致（请重新导出并提交）")
    return diffs
