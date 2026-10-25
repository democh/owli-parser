"""检索黄金集端到端测试：真实解析发布 → 索引 → 评估 → 基线与回归记录。

覆盖：
- 真实语料（3 文档）经完整链路索引后，Recall@K/MRR/nDCG 达到设定基线；
- 基线未达 → state=failed 并列出违规项（CI 护栏语义）；
- 同名黄金集幂等更新；运行记录落库并经 API 可查（仅指标，不含内容）。
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from owli_parser.bootstrap.app import create_app
from owli_parser.bootstrap.routes_catalog import _authoritative_chunk_ids
from owli_parser.core.context import ActorKind
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.knowledge.models import GoldenRunRow, GoldenSetRow
from owli_parser.modules.retrieval.golden_runner import GoldenRunner
from tests.conftest import create_user_with_tenant

CORPUS = {
    "quantum": "量子计算利用量子比特进行并行运算\n",
    "retrieval": "检索系统通过倒排索引匹配查询词\n",
    "database": "数据库索引加速结构化查询\n",
}


def _scope(tenant_id: uuid.UUID, user_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


async def _login(client: AsyncClient, username: str, tenant_id: uuid.UUID) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {
        "X-OWLI-CSRF": response.json()["csrf_token"],
        "X-Tenant-ID": str(tenant_id),
    }


async def _index_corpus(container, username: str):
    """上传 3 篇语料 → 解析发布 → 返回 (user, tenant, dataset, chunks_by_keyword)。"""
    user_id, tenant_id, _ = await create_user_with_tenant(container, username=username)
    scope = _scope(tenant_id, user_id)
    dataset = await container.catalog.create_dataset(
        scope, name=f"ds-golden-{username}", description=None, visibility="tenant"
    )
    chunks_by_key = {}
    for key, text in CORPUS.items():
        body = text.encode("utf-8")
        upload = await container.uploads.init_upload(
            scope, dataset_id=dataset.id, filename=f"{key}.txt", declared_total_bytes=len(body)
        )
        await container.uploads.put_part(scope, upload.id, 1, iter([body]))
        artifact_id = await container.uploads.complete_upload(scope, upload.id)
        result = await container.submit_parsing.submit(
            scope,
            artifact_id=artifact_id,
            dataset_id=dataset.id,
            pipeline_ref=None,
            document_id=None,
            expected_revision=None,
            filename=f"{key}.txt",
        )
        pipeline = container.submit_parsing.compile_default_pipeline()
        await container.run_parsing_job._execute(result.job_id, "worker-test", pipeline)
        chunks, _, _ = await container.knowledge.list_chunks(
            tenant_id, document_id=result.document_id, version_id=None, cursor=None, limit=50
        )
        keyword = CORPUS[key].strip()[:2]
        chunks_by_key[key] = [c for c in chunks if c.enabled and keyword in c.text]
    return user_id, tenant_id, dataset.id, chunks_by_key


def _make_search_fn(container, scope: CatalogScope):
    async def search_fn(case: dict) -> list[str]:
        dataset_ids = [str(x) for x in case.get("dataset_ids", [])]

        async def authority(candidates):
            return await _authoritative_chunk_ids(container, scope, candidates)

        result = await container.retrieval.search(
            tenant_id=str(scope.tenant_id),
            query=str(case["query"]),
            query_vector=None,
            dataset_ids=dataset_ids or None,
            acl_refs=[f"dataset:{d}" for d in dataset_ids] or None,
            authoritative_filter=authority,
        )
        return [str(item.payload.get("chunk_id")) for item in result.items]

    return search_fn


async def test_golden_run_passes_and_records_metrics(container) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, dataset_id, chunks_by_key = await _index_corpus(container, "golden-a")

    cases = [
        {
            "query": "量子计算",
            "dataset_ids": [str(dataset_id)],
            "relevant": [str(c.id) for c in chunks_by_key["quantum"]],
            "groups": {"lang": "zh", "topic": "quantum"},
        },
        {
            "query": "倒排索引",
            "dataset_ids": [str(dataset_id)],
            "relevant": [str(c.id) for c in chunks_by_key["retrieval"]],
            "groups": {"lang": "zh", "topic": "retrieval"},
        },
        {
            "query": "数据库索引",
            "dataset_ids": [str(dataset_id)],
            "relevant": [str(c.id) for c in chunks_by_key["database"]],
            "groups": {"lang": "zh", "topic": "db"},
        },
    ]
    golden = await container.knowledge.upsert_golden_set(
        name="retrieval-smoke",
        cases=cases,
        expectations={"k_values": [1, 3], "min_metrics": {"recall@3": 1.0, "mrr": 1.0}},
        tenant_id=tenant_id,
    )
    runner = GoldenRunner(container.knowledge, container.clock)
    run = await runner.run(
        golden_set=golden,
        search_fn=_make_search_fn(container, _scope(tenant_id, user_id)),
        adapter_ref="lexical-bm25",
        config_digest="cfg-v1",
    )

    assert run.passed is True, run.violations
    assert run.aggregate["recall@3"] == 1.0
    assert run.aggregate["mrr"] == 1.0
    assert set(run.by_group) == {"lang", "topic"}
    assert run.by_group["topic"]["recall@3"] == 1.0

    # 落库 + API 只读回查（仅指标，不含内容）
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, "golden-a", tenant_id)
        response = await client.get("/v1/quality/regression-runs", headers=headers)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["run_id"] == run.run_id
        assert items[0]["state"] == "passed"
        assert items[0]["aggregate"]["recall@3"] == 1.0
        assert "cases" not in items[0]  # 列表不返回 case 明细内容


async def test_golden_run_fails_on_baseline_violation(container) -> None:
    container.settings.retrieval.enabled = True
    user_id, tenant_id, dataset_id, chunks_by_key = await _index_corpus(container, "golden-b")

    cases = [
        {
            "query": "完全无关的外星语料",
            "dataset_ids": [str(dataset_id)],
            "relevant": [str(chunks_by_key["quantum"][0].id)],
            "groups": {"lang": "zh"},
        }
    ]
    golden = await container.knowledge.upsert_golden_set(
        name="retrieval-negative",
        cases=cases,
        expectations={"k_values": [1, 3], "min_metrics": {"recall@3": 0.5}},
        tenant_id=tenant_id,
    )
    runner = GoldenRunner(container.knowledge, container.clock)
    run = await runner.run(
        golden_set=golden,
        search_fn=_make_search_fn(container, _scope(tenant_id, user_id)),
        adapter_ref="lexical-bm25",
    )
    assert run.passed is False
    assert run.violations and "recall@3" in run.violations[0]

    async with container.uow.read_only() as session:
        row = (
            await session.execute(select(GoldenRunRow).where(GoldenRunRow.golden_set_id == golden.id))
        ).scalar_one()
    assert row.state == "failed"
    assert row.metrics["passed"] is False
    assert row.metrics["violations"]


async def test_golden_set_upsert_is_idempotent(container) -> None:
    first = await container.knowledge.upsert_golden_set(
        name="idempotent-set",
        cases=[{"query": "q1", "relevant": ["c1"]}],
        expectations={"k_values": [1]},
    )
    second = await container.knowledge.upsert_golden_set(
        name="idempotent-set",
        cases=[{"query": "q2", "relevant": ["c2"]}, {"query": "q3", "relevant": ["c3"]}],
        expectations={"k_values": [1, 3]},
    )
    assert first.id == second.id
    loaded = await container.knowledge.get_golden_set("idempotent-set")
    assert loaded is not None
    assert [case["query"] for case in loaded.cases] == ["q2", "q3"]
    assert loaded.expectations == {"k_values": [1, 3]}
    async with container.uow.read_only() as session:
        count = (await session.execute(select(func.count()).select_from(GoldenSetRow))).scalar_one()
    assert count == 1


async def test_golden_runs_filter_and_set_name(container) -> None:
    """多黄金集：运行列表可按集过滤，并回传集名（历史不被静默隐藏）。"""
    set_a = await container.knowledge.upsert_golden_set(name="multi-set-a", cases=[], expectations={})
    set_b = await container.knowledge.upsert_golden_set(name="multi-set-b", cases=[], expectations={})
    run_a = await container.knowledge.create_golden_run(
        golden_set_id=set_a.id,
        adapter_ref="lexical-bm25",
        config_digest=None,
        metrics={"passed": True, "aggregate": {}},
        state="passed",
    )
    run_b = await container.knowledge.create_golden_run(
        golden_set_id=set_b.id,
        adapter_ref="lexical-bm25",
        config_digest=None,
        metrics={"passed": False, "aggregate": {}},
        state="failed",
    )
    assert run_a.golden_set_name == "multi-set-a"
    assert run_b.golden_set_name == "multi-set-b"

    all_runs = await container.knowledge.list_golden_runs(limit=10)
    assert {run.golden_set_id for run in all_runs} == {set_a.id, set_b.id}

    only_a = await container.knowledge.list_golden_runs(limit=10, golden_set_id=set_a.id)
    assert [run.id for run in only_a] == [run_a.id]
    assert only_a[0].golden_set_name == "multi-set-a"

    none_match = await container.knowledge.list_golden_runs(limit=10, golden_set_id=uuid.uuid4())
    assert none_match == []


async def test_trigger_regression_run_api(container) -> None:
    """API 触发同步回归：admin 可触发、结果落库并返回判定与指标。"""
    container.settings.retrieval.enabled = True
    _user_id, tenant_id, dataset_id, chunks_by_key = await _index_corpus(container, "api-run")
    cases = [
        {
            "query": "量子计算",
            "dataset_ids": [str(dataset_id)],
            "relevant": [str(c.id) for c in chunks_by_key["quantum"]],
            "groups": {"lang": "zh"},
        }
    ]
    golden = await container.knowledge.upsert_golden_set(
        name="api-run-set",
        cases=cases,
        expectations={"k_values": [3], "min_metrics": {"recall@3": 1.0}},
        tenant_id=tenant_id,
    )

    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, "api-run", tenant_id)
        response = await client.post(
            "/v1/quality/regression-runs",
            json={"golden_set_id": str(golden.id)},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["passed"] is True
        assert payload["aggregate"]["recall@3"] == 1.0

        # 落库可查（质量页刷新后可见）
        runs = await container.knowledge.list_golden_runs(limit=5, golden_set_id=golden.id)
        assert any(run.run_id == payload["run_id"] for run in runs)


async def test_trigger_regression_permissions_and_capability(container) -> None:
    """权限与能力边界：viewer 403、跨租户 404、检索未启用 503。"""
    container.settings.retrieval.enabled = True
    _user_id, tenant_id, dataset_id, _chunks = await _index_corpus(container, "perm-owner")
    golden = await container.knowledge.upsert_golden_set(
        name="perm-set",
        cases=[{"query": "量子", "dataset_ids": [str(dataset_id)], "relevant": []}],
        expectations={},
        tenant_id=tenant_id,
    )

    # viewer 403（写操作需要 tenant admin）
    _viewer_id, viewer_tenant, _ = await create_user_with_tenant(
        container, username="perm-viewer", role="viewer"
    )
    assert viewer_tenant != tenant_id
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        viewer_headers = await _login(client, "perm-viewer", viewer_tenant)
        denied = await client.post(
            "/v1/quality/regression-runs",
            json={"golden_set_id": str(golden.id)},
            headers=viewer_headers,
        )
        assert denied.status_code == 403

        # 跨租户 404（不泄露存在性）：owner 的黄金集对另一 admin 不可见
        _other_id, other_tenant, _ = await create_user_with_tenant(container, username="perm-other")
        other_headers = await _login(client, "perm-other", other_tenant)
        cross = await client.post(
            "/v1/quality/regression-runs",
            json={"golden_set_id": str(golden.id)},
            headers=other_headers,
        )
        assert cross.status_code == 404

        # 检索未启用 503
        container.settings.retrieval.enabled = False
        owner_headers = await _login(client, "perm-owner", tenant_id)
        disabled = await client.post(
            "/v1/quality/regression-runs",
            json={"golden_set_id": str(golden.id)},
            headers=owner_headers,
        )
        assert disabled.status_code == 503
