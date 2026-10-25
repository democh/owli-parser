"""M4 问答最小闭环：SSE 客户端契约、证据冻结、引用校验、拒答与断流恢复。

- GenerationClient：SSE data 行解析、畸形行跳过、HTTP/超时错误映射；
- ChatService：证据快照（chunk_revision 冻结）、事件序列持久化、伪引用拒绝、
  无证据拒答、无绑定明确失败、after_seq 重放、断流标记 cancelled；
- API：SSE 流式响应（data/[DONE]）与应用 token scope 强制（chat:invoke）。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from owli_parser.adapters.model_gateway import GenerationClient
from owli_parser.bootstrap.app import create_app
from owli_parser.core.context import ActorKind
from owli_parser.core.errors import (
    ExternalServiceUnavailableError,
    NotFoundError,
    UpstreamTimeoutError,
    ValidationFailedError,
)
from owli_parser.modules.catalog.domain import Role, UserAccessContext
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.chat.service import ChatService, EvidenceInput, GenerationHandle
from tests.conftest import create_user_with_tenant


def _scope(tenant_id: uuid.UUID, user_id: uuid.UUID) -> CatalogScope:
    return CatalogScope(
        tenant_id=tenant_id,
        kind=ActorKind.USER,
        actor_id=user_id,
        user_ctx=UserAccessContext(user_id=user_id, role=Role.ADMIN),
    )


def _sse_bytes(deltas: list[str], *, done: bool = True) -> bytes:
    lines = []
    for delta in deltas:
        payload = {"choices": [{"delta": {"content": delta}}]}
        lines.append(f"data: {json.dumps(payload)}\n\n")
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


async def test_generation_client_stream_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = (
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
            "data: 这不是JSON\n\n"
            'data: {"choices":[{"delta":{}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"，世界"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":4,"total_tokens":12}}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        return httpx.Response(200, content=body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    recorded: dict[str, int] = {}
    client = GenerationClient(
        base_url="http://gen.internal:8000/v1",
        model_name="qwen2.5",
        api_key="sk-gen",
        client=http,
    )
    collected = [
        delta async for delta in client.stream([{"role": "user", "content": "hi"}], on_usage=recorded.update)
    ]
    assert collected == ["你好", "，世界"]
    assert recorded == {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}
    body = json.loads(seen[0].content)
    assert body["model"] == "qwen2.5" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert seen[0].headers["authorization"] == "Bearer sk-gen"

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    failing_client = GenerationClient(
        base_url="http://gen.internal:8000/v1",
        model_name="m",
        api_key=None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(failing)),
    )
    with pytest.raises(ExternalServiceUnavailableError, match="500"):
        [delta async for delta in failing_client.stream([])]

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("t")

    timeout_client = GenerationClient(
        base_url="http://gen.internal:8000/v1",
        model_name="m",
        api_key=None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)),
    )
    with pytest.raises(UpstreamTimeoutError):
        [delta async for delta in timeout_client.stream([])]


async def _chat_service(
    container, *, evidences=None, deltas=None, generator_present=True, budget=None, usage=None
):
    async def search_evidence(tenant_id, dataset_ids, query):
        return list(evidences or [])

    async def generate(messages, *, on_usage=None):
        if usage is not None and on_usage is not None:
            on_usage(usage)
        for delta in deltas or []:
            yield delta

    async def resolver(tenant_id):
        if not generator_present:
            return None
        return GenerationHandle(stream=generate, budget=dict(budget or {}))

    return ChatService(
        container.uow,
        container.clock,
        search_evidence=search_evidence,
        generate_resolver=resolver,
    )


def _evidence() -> EvidenceInput:
    return EvidenceInput(
        doc_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        chunk_revision=3,
        acl_refs=["dataset:abc"],
        text="公司差旅报销需在 30 日内提交。",
        source_refs=[{"kind": "text_span", "part": 0, "char_start": 0, "char_end": 10}],
        score=0.9,
    )


async def _run_events(service, run_id) -> list[dict]:
    return [event async for event in service.execute(run_id)]


async def test_chat_full_answer_freezes_evidence(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-ok")
    scope = _scope(tenant_id, user_id)
    service = await _chat_service(
        container,
        evidences=[_evidence()],
        deltas=["差旅报销需在 30 日内提交", "[1]。"],
    )
    run = await service.create_run(scope, question="差旅报销多久内提交？", dataset_ids=[])
    events = await _run_events(service, run.id)
    types = [event["type"] for event in events]
    assert types == ["run.started", "retrieval.done", "answer.delta", "answer.delta", "answer.done"]
    answer = events[-1]["payload"]
    assert answer["text"] == "差旅报销需在 30 日内提交[1]。"
    assert answer["citations"] == ["e1"]
    assert answer["usage"]["source"] == "estimate"

    # 证据快照冻结（revision/权限/正文）
    from sqlalchemy import select

    from owli_parser.modules.chat.models import EvidenceSnapshotRow

    async with container.uow.read_only() as session:
        rows = (
            (await session.execute(select(EvidenceSnapshotRow).where(EvidenceSnapshotRow.run_id == run.id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    snapshot = rows[0]
    assert snapshot.evidence_id == "e1"
    assert snapshot.chunk_revision == 3
    assert snapshot.acl_refs == ["dataset:abc"]
    assert "30 日" in snapshot.text_snapshot

    info = await service.get_run(scope, run.id)
    assert info.state == "done"

    # 事件重放：after_seq 只返回其后事件
    replayed = await service.list_events(scope, run.id, after_seq=2)
    assert [event.seq for event in replayed] == [3, 4, 5]
    assert replayed[-1].event_type == "answer.done"


async def test_chat_pseudo_citation_rejected(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-pseudo")
    scope = _scope(tenant_id, user_id)
    service = await _chat_service(container, evidences=[_evidence()], deltas=["见文献[9]。"])
    run = await service.create_run(scope, question="引用越界？", dataset_ids=[])
    events = await _run_events(service, run.id)
    assert events[-1]["type"] == "citation.rejected"
    assert events[-1]["payload"]["invalid_citations"] == [9]
    info = await service.get_run(scope, run.id)
    assert info.state == "failed"
    assert info.error is not None and info.error["code"] == "pseudo_citation"
    # 被拒答案不得落 answer.done
    assert all(event["type"] != "answer.done" for event in events)


async def test_chat_refuses_without_evidence(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-empty")
    scope = _scope(tenant_id, user_id)
    service = await _chat_service(container, evidences=[], deltas=["不应生成"])
    run = await service.create_run(scope, question="无资料问题", dataset_ids=[])
    events = await _run_events(service, run.id)
    assert [event["type"] for event in events] == ["run.started", "retrieval.empty", "answer.refused"]
    info = await service.get_run(scope, run.id)
    assert info.state == "done"
    assert info.request_snapshot.get("refused") is True


async def test_chat_missing_generation_binding_fails(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-nogen")
    scope = _scope(tenant_id, user_id)
    service = await _chat_service(container, evidences=[_evidence()], generator_present=False)
    run = await service.create_run(scope, question="绑定缺失？", dataset_ids=[])
    events = await _run_events(service, run.id)
    assert events[-1]["type"] == "run.failed"
    assert events[-1]["payload"]["code"] == "capability_not_ready"
    info = await service.get_run(scope, run.id)
    assert info.state == "failed"


async def test_chat_cancel_before_execute_skips_generation(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-cancel-pre")
    scope = _scope(tenant_id, user_id)
    service = await _chat_service(container, evidences=[_evidence()], deltas=["不应生成"])
    run = await service.create_run(scope, question="取消？", dataset_ids=[])

    info = await service.request_cancel(scope, run.id)
    assert info.state == "cancelled"
    assert info.error == {"code": "cancelled_by_request"}

    events = await _run_events(service, run.id)
    assert [event["type"] for event in events] == ["run.started", "run.cancelled"]
    assert events[-1]["payload"]["reason"] == "client_requested"

    # 幂等：重复取消不改变终态
    again = await service.request_cancel(scope, run.id)
    assert again.state == "cancelled"


async def test_chat_cancel_mid_generation_stops_without_answer_done(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-cancel-mid")
    scope = _scope(tenant_id, user_id)
    holder: dict[str, uuid.UUID] = {}
    services: list[ChatService] = []

    async def search_evidence(_tenant, _datasets, _query):
        return [_evidence()]

    async def generate(_messages, *, on_usage=None):
        for index in range(12):
            if index == 2:
                await services[0].request_cancel(scope, holder["run_id"])
            yield f"d{index}"

    async def resolver(_tenant):
        return GenerationHandle(stream=generate)

    service = ChatService(
        container.uow,
        container.clock,
        search_evidence=search_evidence,
        generate_resolver=resolver,
    )
    services.append(service)
    run = await service.create_run(scope, question="中途取消？", dataset_ids=[])
    holder["run_id"] = run.id

    events = await _run_events(service, run.id)
    types = [event["type"] for event in events]
    assert "answer.done" not in types
    assert types[-1] == "run.cancelled"
    # 检查点每 8 个增量：取消发生在第 3 个增量后，第 8 个增量前终止
    assert sum(1 for event in events if event["type"] == "answer.delta") == 7

    info = await service.get_run(scope, run.id)
    assert info.state == "cancelled"
    expected_error = {"code": "cancelled_by_request"}
    assert info.error == expected_error


async def test_chat_cancel_api_is_idempotent(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-cancel-api")
    scope = _scope(tenant_id, user_id)
    container.chat = await _chat_service(container, evidences=[_evidence()], deltas=["x"])
    run = await container.chat.create_run(scope, question="取消 API？", dataset_ids=[])
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": "chat-cancel-api", "password": "correct-horse-battery"},
        )
        headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": str(tenant_id),
        }
        first = await client.post(f"/v1/chat/runs/{run.id}/cancel", headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["state"] == "cancelled"
        assert first.json()["error"] == {"code": "cancelled_by_request"}

        second = await client.post(f"/v1/chat/runs/{run.id}/cancel", headers=headers)
        assert second.status_code == 200
        assert second.json()["state"] == "cancelled"


async def test_generation_binding_budget_reaches_resolver(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-budget-wire")
    connection = await container.configuration.create_model_connection(
        tenant_id,
        name="conn-gen",
        protocol="openai_compatible",
        base_url="http://models.internal:8000/v1",
        vendor_label=None,
        region=None,
        credential_ref=None,
        actor_id=user_id,
    )
    await container.configuration.publish_model_connection(tenant_id, connection.id)
    profile = await container.configuration.create_model_profile(
        tenant_id,
        connection_id=connection.id,
        model_name="qwen2.5",
        capabilities=["generation"],
        context_window=None,
        tokenizer_ref=None,
        max_input_tokens=None,
        max_output_tokens=None,
        supported_parameters=[],
        pricing_ref=None,
        embedding_dimensions=None,
        embedding_normalization=None,
        embedding_distance=None,
        output_kinds=[],
        actor_id=user_id,
    )
    await container.configuration.upsert_model_binding(
        tenant_id,
        "generation",
        primary_profile_id=profile.id,
        fallback_profile_ids=[],
        budget={"max_tokens_per_run": 1234},
        expected_revision=None,
        actor_id=user_id,
    )

    from owli_parser.bootstrap.model_wiring import make_generation_resolver

    handle = await make_generation_resolver(container)(tenant_id)
    assert handle is not None
    assert handle.budget == {"max_tokens_per_run": 1234}
    binding = await container.configuration.get_model_binding(tenant_id, "generation")
    assert binding is not None
    assert binding.budget == {"max_tokens_per_run": 1234}


async def test_chat_budget_exceeded_before_generation(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-budget-pre")
    scope = _scope(tenant_id, user_id)
    service = await _chat_service(
        container,
        evidences=[_evidence()],
        deltas=["不应生成"],
        budget={"max_tokens_per_run": 10},
    )
    run = await service.create_run(scope, question="预算？", dataset_ids=[])
    events = await _run_events(service, run.id)

    assert [event["type"] for event in events] == [
        "run.started",
        "retrieval.done",
        "run.failed",
    ]
    payload = events[-1]["payload"]
    assert payload["code"] == "budget_exceeded"
    assert payload["max_tokens"] == 10
    info = await service.get_run(scope, run.id)
    assert info.state == "failed"
    assert info.error is not None and info.error["code"] == "budget_exceeded"
    assert info.request_snapshot["usage"]["source"] == "estimate"


async def test_chat_budget_exceeded_during_generation(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-budget-mid")
    scope = _scope(tenant_id, user_id)
    service = await _chat_service(
        container,
        evidences=[_evidence()],
        deltas=["长" * 400, "长" * 400],
        budget={"max_tokens_per_run": 100},
    )
    run = await service.create_run(scope, question="中途预算？", dataset_ids=[])
    events = await _run_events(service, run.id)

    types = [event["type"] for event in events]
    assert "answer.done" not in types
    assert "answer.delta" not in types
    assert types[-1] == "run.failed"
    assert events[-1]["payload"]["code"] == "budget_exceeded"
    assert events[-1]["payload"]["source"] == "estimate"


async def test_chat_records_provider_usage(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-usage-ok")
    scope = _scope(tenant_id, user_id)
    usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    service = await _chat_service(
        container,
        evidences=[_evidence()],
        deltas=["回答[1]"],
        budget={"max_tokens_per_run": 100},
        usage=usage,
    )
    run = await service.create_run(scope, question="用量？", dataset_ids=[])
    events = await _run_events(service, run.id)

    assert events[-1]["type"] == "answer.done"
    assert events[-1]["payload"]["usage"] == {**usage, "source": "provider"}
    info = await service.get_run(scope, run.id)
    assert info.request_snapshot["usage"]["total_tokens"] == 30


async def test_chat_provider_usage_over_budget_rejects(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-usage-over")
    scope = _scope(tenant_id, user_id)
    usage = {"prompt_tokens": 60, "completion_tokens": 80, "total_tokens": 140}
    service = await _chat_service(
        container,
        evidences=[_evidence()],
        deltas=["回答[1]"],
        budget={"max_tokens_per_run": 100},
        usage=usage,
    )
    run = await service.create_run(scope, question="超预算？", dataset_ids=[])
    events = await _run_events(service, run.id)

    types = [event["type"] for event in events]
    assert "answer.done" not in types
    assert types[-1] == "run.failed"
    assert events[-1]["payload"]["code"] == "budget_exceeded"
    assert events[-1]["payload"]["source"] == "provider"


async def test_chat_multi_turn_injects_history_and_inherits_dataset(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-multi")
    scope = _scope(tenant_id, user_id)
    calls: list[list[dict[str, str]]] = []

    async def search_evidence(_tenant, _datasets, _query):
        return [_evidence()]

    async def generate(messages, *, on_usage=None):
        calls.append(messages)
        yield "回答[1]"

    async def resolver(_tenant):
        return GenerationHandle(stream=generate)

    service = ChatService(
        container.uow,
        container.clock,
        search_evidence=search_evidence,
        generate_resolver=resolver,
    )
    parent = await service.create_run(scope, question="第一个问题", dataset_ids=["ds-1"])
    await _run_events(service, parent.id)

    child = await service.create_run(scope, question="第二个问题", dataset_ids=[], parent_run_id=parent.id)
    assert child.dataset_ids == ["ds-1"]  # 未显式指定时继承父运行
    await _run_events(service, child.id)

    assert len(calls) == 2
    child_messages = calls[1]
    assert [message["role"] for message in child_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert child_messages[1]["content"] == "第一个问题"
    assert child_messages[2]["content"] == "回答[1]"
    assert "第二个问题" in child_messages[3]["content"]
    info = await service.get_run(scope, child.id)
    assert info.parent_run_id == parent.id


async def test_chat_multi_turn_rejects_running_or_foreign_parent(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-multi-bad")
    scope = _scope(tenant_id, user_id)
    service = await _chat_service(container, evidences=[_evidence()], deltas=["回答[1]"])
    parent = await service.create_run(scope, question="未执行完成", dataset_ids=[])

    with pytest.raises(ValidationFailedError):
        await service.create_run(scope, question="追问", dataset_ids=[], parent_run_id=parent.id)
    with pytest.raises(NotFoundError):
        await service.create_run(scope, question="跨租户", dataset_ids=[], parent_run_id=uuid.uuid4())


async def test_chat_api_multi_turn_parent_payload(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-multi-api")
    scope = _scope(tenant_id, user_id)
    container.chat = await _chat_service(container, evidences=[_evidence()], deltas=["回答[1]"])
    parent = await container.chat.create_run(scope, question="父问题", dataset_ids=[])
    await _run_events(container.chat, parent.id)

    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": "chat-multi-api", "password": "correct-horse-battery"},
        )
        headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": str(tenant_id),
        }
        async with client.stream(
            "POST",
            "/v1/chat/runs",
            json={"question": "子问题", "parent_run_id": str(parent.id)},
            headers=headers,
        ) as response:
            lines = [line async for line in response.aiter_lines() if line.startswith("data:")]
        events = [json.loads(line[5:].strip()) for line in lines if line[5:].strip() != "[DONE]"]
        child_id = events[0]["run_id"]
        recorded = await client.get(f"/v1/chat/runs/{child_id}", headers=headers)
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["parent_run_id"] == str(parent.id)


async def test_chat_disconnect_marks_cancelled(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-cancel")
    scope = _scope(tenant_id, user_id)
    service = await _chat_service(container, evidences=[_evidence()], deltas=["第一段", "第二段", "第三段"])
    run = await service.create_run(scope, question="断流？", dataset_ids=[])
    generator = service.execute(run.id)
    first = await generator.__anext__()
    assert first["type"] == "run.started"
    await generator.aclose()  # 模拟客户端断开
    info = await service.get_run(scope, run.id)
    assert info.state == "cancelled"
    assert info.error is not None and info.error["code"] == "client_disconnected"


async def test_chat_api_sse_and_scope_enforcement(container) -> None:
    user_id, tenant_id, _ = await create_user_with_tenant(container, username="chat-api")
    # 用户会话调用：SSE 流可解析（stub 检索与生成）
    container.chat = await _chat_service(container, evidences=[_evidence()], deltas=["回答[1]"])
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": "chat-api", "password": "correct-horse-battery"},
        )
        headers = {
            "X-OWLI-CSRF": login.json()["csrf_token"],
            "X-Tenant-ID": str(tenant_id),
        }
        async with client.stream(
            "POST", "/v1/chat/runs", json={"question": "测试问题"}, headers=headers
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = [line async for line in response.aiter_lines() if line.startswith("data:")]
        events = [json.loads(line[5:].strip()) for line in lines if line[5:].strip() != "[DONE]"]
        assert lines[-1].strip().endswith("[DONE]")
        assert events[0]["type"] == "run.started"
        assert events[-1]["type"] == "answer.done"

        # 应用 token 无 chat:invoke → 403（fail-closed 规则表命中后拒绝）
        app_id = await container.access.create_application(
            tenant_id,
            name="chat-denied",
            description=None,
            allowed_scopes=["documents:read"],
            created_by_id=user_id,
        )
        issued = await container.access.issue_token(
            tenant_id,
            app_id.id,
            name="chat-denied-token",
            scopes=["documents:read"],
            dataset_ids=[],
            expires_in_seconds=600,
            created_by_id=user_id,
        )
    # 独立 client（不带会话 cookie，避免同时携带两种凭据的歧义 401）
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as app_client:
        denied = await app_client.post(
            "/v1/chat/runs",
            json={"question": "x"},
            headers={"Authorization": f"Bearer {issued.raw_token}"},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "scope_required"
