"""CLI：回归集模板命令（L-6）——生成 cases.json 骨架，免手写占位 UUID。

- --template 写文件后退出（不连接数据库、不构造 Container）；
- 文件已存在时拒绝覆盖（避免误删手写用例）；
- 缺 --file/--tenant 时给出可操作错误，而不是静默读默认配置。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from typer.testing import CliRunner

from owli_parser.bootstrap.cli import app


def _invoke(args: list[str]):
    return CliRunner().invoke(app, ["parsing-regression", *args])


def test_template_writes_valid_skeleton(tmp_path: Path) -> None:
    target = tmp_path / "cases.json"
    result = _invoke(["--template", str(target)])
    assert result.exit_code == 0, result.output
    assert "已写出回归集模板" in result.output

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert len(payload["cases"]) == 1
    case = payload["cases"][0]
    assert set(case) == {"reference_version_id", "hypothesis_version_id", "groups"}
    uuid.UUID(case["reference_version_id"])
    uuid.UUID(case["hypothesis_version_id"])
    assert case["groups"] == {"format": "pdf", "language": "zh"}
    assert payload["expectations"] == {"max_cer": 0.05, "min_reading_order": 0.9}


def test_template_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "cases.json"
    target.write_text('{"cases": []}', encoding="utf-8")
    result = _invoke(["--template", str(target)])
    assert result.exit_code != 0
    assert target.read_text(encoding="utf-8") == '{"cases": []}'


def test_file_and_tenant_are_required_without_template(tmp_path: Path) -> None:
    missing_file = _invoke([])
    assert missing_file.exit_code != 0
    assert "--file" in missing_file.output

    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "reference_version_id": str(uuid.uuid4()),
                        "hypothesis_version_id": str(uuid.uuid4()),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    missing_tenant = _invoke(["--file", str(cases)])
    assert missing_tenant.exit_code != 0
    assert "--tenant" in missing_tenant.output
