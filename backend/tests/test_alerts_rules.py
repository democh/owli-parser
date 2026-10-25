"""告警规则守卫：结构完整、告警名唯一，且只引用应用真实暴露的指标。

规则文件与 docs/alerts.md 同步维护；指标名漂移（重命名/删除）在此处失败，
避免"规则看着在、指标早已不存在"的静默失效。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

RULES_PATH = Path(__file__).resolve().parents[2] / "deploy" / "prometheus" / "owli-alerts.yaml"
DOCS_PATH = Path(__file__).resolve().parents[2] / "docs" / "alerts.md"

# 应用 /metrics 暴露的指标基名（直方图按 _bucket/_count/_sum 展开）
KNOWN_METRICS = {
    "owli_jobs_pending_total",
    "owli_jobs_oldest_pending_seconds",
    "owli_active_leases_total",
    "owli_outbox_pending_total",
    "owli_outbox_oldest_pending_seconds",
    "owli_stage_duration_seconds",
    "owli_http_request_duration_seconds",
    "owli_token_rejected_total",
    "owli_http_requests_total",
    "owli_retrieval_queries_total",
    "owli_retrieval_channel_queries_total",
    "owli_retrieval_dropped_total",
    "owli_retention_purged_documents_total",
    "owli_retention_purged_artifacts_total",
    "owli_retention_index_entries_total",
    "owli_retention_errors_total",
}


def _load_rules() -> list[dict]:
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    groups = document.get("groups")
    assert isinstance(groups, list) and groups
    rules: list[dict] = []
    for group in groups:
        assert group["name"].startswith("owli-parser.")
        rules.extend(group["rules"])
    return rules


def test_alert_rule_structure() -> None:
    rules = _load_rules()
    assert len(rules) >= 10
    alerts = [rule["alert"] for rule in rules]
    assert len(alerts) == len(set(alerts)), "告警名必须唯一"
    for rule in rules:
        assert rule["expr"].strip()
        assert rule["labels"]["severity"] in {"warning", "critical"}
        assert rule["annotations"]["summary"].strip()


def test_rule_expressions_reference_known_metrics() -> None:
    referenced: set[str] = set()
    for rule in _load_rules():
        for name in re.findall(r"\bowli_[a-z_]+", rule["expr"]):
            for suffix in ("_bucket", "_count", "_sum"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
            referenced.add(name)
    unknown = referenced - KNOWN_METRICS
    assert not unknown, f"规则引用了未暴露的指标：{sorted(unknown)}"


def test_docs_cover_every_alert() -> None:
    docs = DOCS_PATH.read_text(encoding="utf-8")
    missing = [rule["alert"] for rule in _load_rules() if rule["alert"] not in docs]
    assert not missing, f"docs/alerts.md 缺少告警：{missing}"
