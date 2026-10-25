"""契约合法/非法 fixture、配置校验与 SSRF 防护测试。"""

from __future__ import annotations

import uuid
from datetime import UTC

import pytest
from pydantic import ValidationError

from owli_parser.adapters.network.safe_fetcher import (
    _check_network_mode,
    _is_forbidden_ip,
    _is_retryable_status,
    _parse_and_validate_url,
)
from owli_parser.bootstrap.settings import load_boot_settings
from owli_parser.core.errors import ConfigInvalidError
from owli_parser.ports.fetcher import FetchBlocked, FetchPolicy


def _policy(**overrides) -> FetchPolicy:
    base: dict = {
        "allowed_schemes": ("http", "https"),
        "allowed_ports": (80, 443),
        "max_redirects": 3,
        "connect_timeout_seconds": 10,
        "read_timeout_seconds": 30,
        "total_timeout_seconds": 600,
        "stalled_timeout_seconds": 60,
        "max_bytes": 1024 * 1024,
        "allow_private_networks": False,
        "internal_allowlist": (),
        "proxy_url": None,
        "tls_ca_bundle": None,
        "network_mode": "hybrid",
    }
    base.update(overrides)
    return FetchPolicy(**base)


class TestIrContract:
    def test_valid_ir_with_page_region(self) -> None:
        from owli_parser.contracts.ir import (
            Block,
            CoverageReport,
            DocumentIR,
            PageGeometry,
            PageRegionSourceRef,
            Provenance,
            QualityReport,
            TextPayload,
        )

        block = Block(
            id="b-0001",
            type="text",
            order=0,
            text="hello",
            source_refs=[
                PageRegionSourceRef(
                    artifact_id="a1",
                    page_index=0,
                    geometry_id="page-0",
                    bbox={"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2},
                )
            ],
            payload=TextPayload(heading_path=[]),
            provenance=Provenance(
                adapter_name="pdf-text-local", adapter_version="0.1.0", adapter_mode="local"
            ),
            acl_ref="dataset:d1",
        )
        ir = DocumentIR(
            doc_id="doc",
            version_id="v1",
            dataset_id="d1",
            tenant_id="t1",
            content_hash="h",
            effective_config_digest="c",
            pages=[PageGeometry(page_index=0, geometry_id="page-0", width=612, height=792)],
            blocks=[block],
            coverage=CoverageReport(unit_kind="page", total_units=1, succeeded_units=1, failed_units=0),
            quality=QualityReport(algorithm_revision="q1"),
        )
        assert ir.blocks[0].source_refs[0].kind == "page_region"  # type: ignore[union-attr]

    def test_invalid_bbox_rejected(self) -> None:
        from owli_parser.contracts.ir import PageRegionSourceRef

        with pytest.raises(ValidationError):
            PageRegionSourceRef(
                artifact_id="a1",
                page_index=0,
                geometry_id="page-0",
                bbox={"x0": 0.9, "y0": 0.1, "x1": 0.1, "y1": 0.2},  # x0 > x1
            )
        with pytest.raises(ValidationError):
            PageRegionSourceRef(
                artifact_id="a1",
                page_index=0,
                geometry_id="page-0",
                bbox={"x0": 0.5, "y0": 0.1, "x1": 1.5, "y1": 0.2},  # 超出 0-1
            )

    def test_duplicate_block_ids_rejected(self) -> None:
        from owli_parser.contracts.ir import (
            ArtifactSourceRef,
            Block,
            CoverageReport,
            DocumentIR,
            Provenance,
            QualityReport,
            TextPayload,
        )

        def make_block(order: int) -> Block:
            return Block(
                id="dup",
                type="text",
                order=order,
                text="x",
                source_refs=[ArtifactSourceRef(artifact_id="a1", reason="file")],
                payload=TextPayload(heading_path=[]),
                provenance=Provenance(adapter_name="x", adapter_version="0", adapter_mode="local"),
                acl_ref="dataset:d1",
            )

        with pytest.raises(ValidationError):
            DocumentIR(
                doc_id="doc",
                version_id="v1",
                dataset_id="d1",
                tenant_id="t1",
                content_hash="h",
                effective_config_digest="c",
                blocks=[make_block(0), make_block(1)],
                coverage=CoverageReport(
                    unit_kind="document", total_units=1, succeeded_units=1, failed_units=0
                ),
                quality=QualityReport(algorithm_revision="q1"),
            )

    def test_event_envelope_major_version(self) -> None:
        from owli_parser.contracts.events import EventEnvelope

        assert EventEnvelope.accepts_major("1.0")
        assert not EventEnvelope.accepts_major("2.0")

    def test_event_envelope_unknown_field_and_seq_invariant(self) -> None:
        """契约矩阵：事件未知字段明确拒绝（不静默误读）；seq 从 1 起。"""
        from datetime import datetime

        from pydantic import ValidationError

        from owli_parser.contracts.events import EventEnvelope

        base = {
            "event_id": "evt-1",
            "seq": 1,
            "type": "job.state",
            "tenant_scope": "tenant:t1",
            "resource_id": "job-1",
            "ts": datetime.now(UTC),
        }
        envelope = EventEnvelope(**base)  # 合法例
        assert envelope.seq == 1

        with pytest.raises(ValidationError):
            EventEnvelope(**base, unknown_field=1)  # 未知字段拒绝
        with pytest.raises(ValidationError):
            EventEnvelope(**{**base, "seq": 0})  # seq 不变量


class TestSettings:
    def test_env_override_and_cross_validation(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("OWLI_JOBS__LEASE_SECONDS", "300")
        settings = load_boot_settings()
        assert settings.jobs.lease_seconds == 300

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ConfigInvalidError):
            load_boot_settings(app={"name": "x", "typo_key": True})

    def test_cross_field_validation(self) -> None:
        with pytest.raises(ConfigInvalidError):
            load_boot_settings(jobs={"lease_seconds": 30, "heartbeat_seconds": 60})
        with pytest.raises(ConfigInvalidError):
            load_boot_settings(chunk={"target_tokens": 900, "max_tokens": 800})
        with pytest.raises(ConfigInvalidError):
            load_boot_settings(retrieval={"top_k": 200, "candidate_limit": 100})

    def test_offline_mode_rejects_proxy(self) -> None:
        with pytest.raises(ConfigInvalidError):
            load_boot_settings(network={"mode": "offline", "proxy_url": "http://proxy.local:3128"})


class TestFetchPolicy:
    def test_reject_userinfo_and_schemes(self) -> None:
        policy = _policy()
        with pytest.raises(FetchBlocked):
            _parse_and_validate_url("http://user:pass@example.com/a.pdf", policy)
        with pytest.raises(FetchBlocked):
            _parse_and_validate_url("ftp://example.com/a.pdf", policy)
        with pytest.raises(FetchBlocked):
            _parse_and_validate_url("file:///etc/passwd", policy)
        with pytest.raises(FetchBlocked):
            _parse_and_validate_url("http://example.com:8080/a.pdf", policy)

    def test_reject_dangerous_ips(self) -> None:
        import ipaddress

        assert _is_forbidden_ip(ipaddress.ip_address("127.0.0.1")) is not None
        assert _is_forbidden_ip(ipaddress.ip_address("169.254.169.254")) is not None
        assert _is_forbidden_ip(ipaddress.ip_address("::1")) is not None
        assert _is_forbidden_ip(ipaddress.ip_address("fe80::1")) is not None
        assert _is_forbidden_ip(ipaddress.ip_address("224.0.0.1")) is not None
        # IPv4-mapped IPv6 不得绕过
        assert _is_forbidden_ip(ipaddress.ip_address("::ffff:127.0.0.1")) is not None
        assert _is_forbidden_ip(ipaddress.ip_address("::ffff:169.254.169.254")) is not None
        # 公网地址本身类别合法
        assert _is_forbidden_ip(ipaddress.ip_address("93.184.216.34")) is None

    def test_offline_mode_blocks_public(self) -> None:
        policy = _policy(network_mode="offline", internal_allowlist=("files.internal.test:443",))
        _check_network_mode("files.internal.test", 443, policy)  # 获准内网放行
        with pytest.raises(FetchBlocked):
            _check_network_mode("example.com", 443, policy)

    def test_retryable_statuses_come_from_policy(self) -> None:
        policy = _policy()
        assert _is_retryable_status(429, policy)
        assert _is_retryable_status(503, policy)
        assert not _is_retryable_status(404, policy)
        custom = _policy(retryable_status_codes=(418,))
        assert _is_retryable_status(418, custom)
        assert not _is_retryable_status(503, custom)

    def test_private_requires_explicit_policy(self) -> None:
        from owli_parser.adapters.network.safe_fetcher import _resolve_and_check

        # 私网地址在默认策略下被拒绝（解析 127.0.0.1 必被 loopback 规则拒绝）
        with pytest.raises(FetchBlocked):
            _resolve_and_check("localhost", 443, _policy())


class TestAccessTokenDomain:
    def test_token_parse_roundtrip_and_rejection(self) -> None:
        from owli_parser.core.errors import NotAuthenticatedError
        from owli_parser.modules.access import domain as access_domain

        raw, parsed = access_domain.generate_token("owli_pat")
        assert raw.startswith("owli_pat_")
        reparsed = access_domain.parse_token("owli_pat", raw)
        assert reparsed.public_id == parsed.public_id
        assert reparsed.secret == parsed.secret
        with pytest.raises(NotAuthenticatedError):
            access_domain.parse_token("owli_pat", "not-a-token")
        with pytest.raises(NotAuthenticatedError):
            access_domain.parse_token("owli_pat", raw + "_extra")

    def test_ttl_limits(self) -> None:
        from owli_parser.core.errors import InvalidInputError
        from owli_parser.modules.access import domain as access_domain

        with pytest.raises(InvalidInputError):
            access_domain.validate_ttl(0, default_ttl=100, max_ttl=1000)
        with pytest.raises(InvalidInputError):
            access_domain.validate_ttl(99999, default_ttl=100, max_ttl=1000)
        assert access_domain.validate_ttl(500, default_ttl=100, max_ttl=1000) == 500


class TestChunking:
    def test_structure_first_and_table_row_groups(self) -> None:
        from owli_parser.bootstrap.settings import ChunkSection
        from owli_parser.contracts.ir import (
            ArtifactSourceRef,
            Block,
            CoverageReport,
            DocumentIR,
            Provenance,
            QualityReport,
            TableCell,
            TablePayload,
            TextPayload,
        )
        from owli_parser.modules.knowledge.chunking import build_chunks

        provenance = Provenance(adapter_name="x", adapter_version="0", adapter_mode="local")
        blocks = [
            Block(
                id="b-0000",
                type="heading",
                order=0,
                text="第一章",
                source_refs=[ArtifactSourceRef(artifact_id="a1", reason="file")],
                payload=TextPayload(heading_path=["第一章"], level=1),
                provenance=provenance,
                acl_ref="dataset:d1",
            ),
            Block(
                id="b-0001",
                type="text",
                order=1,
                text="正文内容。" * 50,
                source_refs=[ArtifactSourceRef(artifact_id="a1", reason="file")],
                payload=TextPayload(heading_path=["第一章"]),
                provenance=provenance,
                acl_ref="dataset:d1",
            ),
            Block(
                id="b-0002",
                type="table",
                order=2,
                text="col1 | col2",
                source_refs=[
                    ArtifactSourceRef(artifact_id="a1", reason="file"),
                ],
                payload=TablePayload(
                    rows=[
                        [TableCell(text="h1", is_header=True), TableCell(text="h2", is_header=True)],
                        *[[TableCell(text=f"a{i}"), TableCell(text=f"b{i}")] for i in range(120)],
                    ],
                    header_row_count=1,
                ),
                provenance=provenance,
                acl_ref="dataset:d1",
            ),
        ]
        ir = DocumentIR(
            doc_id="doc",
            version_id="v1",
            dataset_id="d1",
            tenant_id="t1",
            content_hash="h",
            effective_config_digest="c",
            blocks=blocks,
            coverage=CoverageReport(unit_kind="document", total_units=1, succeeded_units=1, failed_units=0),
            quality=QualityReport(algorithm_revision="q1"),
        )
        settings = ChunkSection(target_tokens=100, max_tokens=150, overlap_tokens=10)
        chunks = build_chunks(ir, settings)
        kinds = [c.kind for c in chunks]
        assert "parent" in kinds
        explanations = [c.explanation.get("kind") for c in chunks]
        assert "table_full" in explanations
        assert "table_row_group" in explanations
        # 行组复制表头
        row_groups = [c for c in chunks if c.explanation.get("kind") == "table_row_group"]
        assert all(c.explanation.get("header_copied") for c in row_groups)
        # 所有子块有 source_refs
        assert all(c.source_refs for c in chunks)

    def test_unknown_ids_digest_stable(self) -> None:
        from owli_parser.modules.knowledge.chunking import canonical_ids_digest

        a = canonical_ids_digest(["b", "a", "c"])
        b = canonical_ids_digest(["c", "b", "a"])
        assert a == b
        assert a != canonical_ids_digest(["a", "b"])


class TestIdempotency:
    async def test_same_key_same_request(self, container) -> None:
        from owli_parser.ports.idempotency import IdempotencyConflict

        tenant_id = uuid.uuid4()
        actor_id = uuid.uuid4()
        first = await container.idempotency.begin(
            tenant_id=tenant_id,
            actor_kind="user",
            actor_id=actor_id,
            operation="imports.create",
            key="k1",
            request_hash="h1",
        )
        again = await container.idempotency.begin(
            tenant_id=tenant_id,
            actor_kind="user",
            actor_id=actor_id,
            operation="imports.create",
            key="k1",
            request_hash="h1",
        )
        assert again.id == first.id
        with pytest.raises(IdempotencyConflict):
            await container.idempotency.begin(
                tenant_id=tenant_id,
                actor_kind="user",
                actor_id=actor_id,
                operation="imports.create",
                key="k1",
                request_hash="h2",
            )
