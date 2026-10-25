"""契约导出测试：快照生成、稳定性与漂移检查（CI 门禁语义）。"""

from __future__ import annotations

import json

from owli_parser.bootstrap.contracts_export import (
    build_contracts,
    check_contracts,
    export_contracts,
)


def test_build_contracts_generates_valid_schemas() -> None:
    contracts = build_contracts()
    assert set(contracts) == {"openapi.json", "ir.schema.json", "event.schema.json"}

    openapi = json.loads(contracts["openapi.json"])
    assert "/v1/retrieval/test" in openapi["paths"]
    assert "/v1/quality/regression-runs" in openapi["paths"]
    assert "/v1/auth/login" in openapi["paths"]
    assert openapi["info"]["title"] == "owli-parser API"

    ir_schema = json.loads(contracts["ir.schema.json"])
    assert ir_schema["title"] == "DocumentIR"
    assert ir_schema["$defs"], "IR schema 应包含判别联合子模型定义"

    event_schema = json.loads(contracts["event.schema.json"])
    assert event_schema["title"] == "EventEnvelope"


def test_contract_build_is_deterministic() -> None:
    assert build_contracts() == build_contracts()


def test_export_then_check_is_clean(tmp_path) -> None:
    written = export_contracts(tmp_path)
    assert {path.name for path in written} == {
        "openapi.json",
        "ir.schema.json",
        "event.schema.json",
    }
    assert check_contracts(tmp_path) == []


def test_check_detects_drift_and_missing(tmp_path) -> None:
    export_contracts(tmp_path)
    # 篡改快照 → 检出漂移
    target = tmp_path / "openapi.json"
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    diffs = check_contracts(tmp_path)
    assert len(diffs) == 1
    assert "openapi.json" in diffs[0]

    # 缺失快照 → 明确提示
    (tmp_path / "openapi.json").unlink()
    (tmp_path / "ir.schema.json").unlink()
    diffs = check_contracts(tmp_path)
    assert len(diffs) == 2
    assert any("缺失" in diff for diff in diffs)
