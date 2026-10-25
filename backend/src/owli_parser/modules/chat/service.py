"""问答服务：证据固定（evidence_snapshots）→ 流式生成 → 引用校验 → 事件重放。

设计约束（文档 M4）：
- 生成前把证据冻结进 evidence_snapshots（chunk_revision + acl_refs + 文本/来源快照）；
  历史答案与引用不从可变 chunk 正文重建；
- 无证据明确拒答（不编造依据）；答案中 [n] 引用编号越界 → 伪引用拒绝（run 失败）；
- 事件逐条持久化（seq 唯一），断流后可按 after_seq 重放；客户端断开 → run 取消；
- 检索与生成均通过注入回调获得（模块不直接依赖 retrieval/模型网关）。
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select

from owli_parser.core.clock import Clock
from owli_parser.core.errors import CapabilityNotReadyError, NotFoundError, ValidationFailedError
from owli_parser.infrastructure.uow import UnitOfWork
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.chat.models import ChatEventRow, ChatRunRow, EvidenceSnapshotRow

_EVENT_SCHEMA_VERSION = "1.0"
_MAX_EVIDENCE = 8
_MAX_QUESTION_CHARS = 4000
_PREVIEW_CHARS = 200
_CITATION_RE = re.compile(r"\[(\d+)\]")
# 生成期间每隔若干增量检查一次取消标记（避免每个 token 一次 PG 查询）
_CANCEL_CHECK_EVERY = 8
# 预算键：单次问答允许的 prompt+completion token 上限（绑定 budget JSON）
_BUDGET_MAX_TOKENS_KEY = "max_tokens_per_run"


def budget_max_tokens(budget: dict[str, Any] | None) -> int | None:
    """从绑定 budget JSON 读取 max_tokens_per_run（非法/缺失返回 None）。"""
    if not isinstance(budget, dict):
        return None
    value = budget.get(_BUDGET_MAX_TOKENS_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def estimate_tokens(text: str) -> int:
    """无 usage 时的保守估算（2 字符≈1 token）；实际 usage 到达后以其为准。"""
    return _estimate_tokens_from_chars(len(text))


def _estimate_tokens_from_chars(chars: int) -> int:
    return 1 if chars <= 0 else (chars + 1) // 2


_SYSTEM_PROMPT = (
    "你是企业知识库问答助手，只能依据给定证据回答。规则：\n"
    "1) 每条依据前有 [n] 编号；引用依据时在句末标注对应编号，如 [1][2]；\n"
    "2) 引用编号不得超出给定依据范围；\n"
    "3) 依据不足以回答时明确说明未找到依据，不得编造。"
)


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    doc_id: uuid.UUID
    version_id: uuid.UUID
    chunk_id: uuid.UUID
    chunk_revision: int
    acl_refs: list[str]
    text: str
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    score: float = 0.0


# 证据检索回调：租户 + 数据集过滤 + 查询 → 证据列表（由 bootstrap 装配检索管线）
SearchEvidenceFn = Callable[[uuid.UUID, list[str], str], Awaitable[list[EvidenceInput]]]


class GenerateStreamFn(Protocol):
    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> AsyncGenerator[str, None]: ...


@dataclass(frozen=True, slots=True)
class GenerationHandle:
    """生成通道句柄：流式函数 + 绑定预算（自由 JSON；支持 max_tokens_per_run）。"""

    stream: GenerateStreamFn
    budget: dict[str, Any] = field(default_factory=dict)


# 生成解析器：租户 → 句柄（None 表示未配置 generation 用途绑定）
GenerateResolver = Callable[[uuid.UUID], Awaitable[GenerationHandle | None]]


@dataclass(frozen=True, slots=True)
class ChatRunInfo:
    id: uuid.UUID
    tenant_id: uuid.UUID
    actor_kind: str
    state: str
    dataset_ids: list[str]
    request_snapshot: dict[str, Any]
    error: dict[str, Any] | None
    parent_run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ChatEventInfo:
    seq: int
    event_type: str
    payload: dict[str, Any]


class ChatService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        search_evidence: SearchEvidenceFn | None,
        generate_resolver: GenerateResolver | None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._search_evidence = search_evidence
        self._generate_resolver = generate_resolver

    # ------------------------------------------------------------------
    # 运行创建 / 查询 / 事件重放
    # ------------------------------------------------------------------

    async def create_run(
        self,
        scope: CatalogScope,
        *,
        question: str,
        dataset_ids: list[str],
        credential_id: uuid.UUID | None = None,
        parent_run_id: uuid.UUID | None = None,
    ) -> ChatRunInfo:
        clean = question.strip()
        if not 1 <= len(clean) <= _MAX_QUESTION_CHARS:
            raise ValidationFailedError(f"question 长度需在 1-{_MAX_QUESTION_CHARS} 之间")
        if parent_run_id is not None:
            async with self._uow.read_only() as session:
                parent = (
                    await session.execute(
                        select(ChatRunRow).where(
                            ChatRunRow.id == parent_run_id,
                            ChatRunRow.tenant_id == scope.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
            if parent is None:
                raise NotFoundError("父问答运行不存在")
            if parent.state == "running":
                raise ValidationFailedError("父问答运行尚未结束，不能追问")
            if not dataset_ids:
                dataset_ids = [str(value) for value in (parent.dataset_ids or [])]
        async with self._uow.begin() as session:
            row = ChatRunRow(
                tenant_id=scope.tenant_id,
                actor_kind=str(scope.kind),
                actor_id=scope.actor_id or uuid.uuid4(),
                credential_id=credential_id,
                parent_run_id=parent_run_id,
                state="running",
                dataset_ids=list(dataset_ids),
                request_snapshot={"question": clean},
            )
            session.add(row)
            await session.flush()
            return self._run_info(row)

    async def get_run(self, scope: CatalogScope, run_id: uuid.UUID) -> ChatRunInfo:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(
                    select(ChatRunRow).where(
                        ChatRunRow.id == run_id,
                        ChatRunRow.tenant_id == scope.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("问答运行不存在")
            return self._run_info(row)

    async def request_cancel(self, scope: CatalogScope, run_id: uuid.UUID) -> ChatRunInfo:
        """请求取消（幂等）：非终态置 cancelled；已终态保持原状。

        生成循环在下一次检查点结束并发出 run.cancelled；已送达的增量不会撤回。
        """
        async with self._uow.begin() as session:
            row = (
                await session.execute(
                    select(ChatRunRow).where(
                        ChatRunRow.id == run_id,
                        ChatRunRow.tenant_id == scope.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("问答运行不存在")
            if row.state not in ("done", "failed", "cancelled"):
                row.state = "cancelled"
                row.error = {"code": "cancelled_by_request"}
            return self._run_info(row)

    async def list_events(
        self, scope: CatalogScope, run_id: uuid.UUID, *, after_seq: int = 0
    ) -> list[ChatEventInfo]:
        await self.get_run(scope, run_id)  # 租户校验（跨租户 404）
        async with self._uow.read_only() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatEventRow)
                        .where(
                            ChatEventRow.run_id == run_id,
                            ChatEventRow.seq > after_seq,
                        )
                        .order_by(ChatEventRow.seq)
                    )
                )
                .scalars()
                .all()
            )
            return [self._event_info(row) for row in rows]

    # ------------------------------------------------------------------
    # 执行（SSE 数据流）：事件逐条持久化；取消/断流时标记 cancelled
    # ------------------------------------------------------------------

    async def execute(self, run_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
        run = await self._load_run(run_id)
        try:
            yield await self._emit(run, "run.started", {"run_id": str(run.id), "state": run.state})
            async for event in self._execute(run):
                yield event
        except GeneratorExit:
            # 客户端断开：标记取消后透传（生成器关闭后不得再 yield）
            await self._mark_state(run.id, "cancelled", error={"code": "client_disconnected"})
            raise
        except Exception as exc:
            await self._mark_state(run.id, "failed", error={"code": "execution_error", "message": str(exc)})
            yield await self._emit(run, "run.failed", {"code": "execution_error", "message": str(exc)})

    async def _execute(self, run: ChatRunRow) -> AsyncIterator[dict[str, Any]]:
        question = str((run.request_snapshot or {}).get("question") or "")
        dataset_ids = [str(value) for value in (run.dataset_ids or [])]

        # 0) 已被请求取消（并发取消/入队前取消）
        if await self._is_cancelled(run.id):
            yield await self._emit(run, "run.cancelled", {"reason": "client_requested"})
            return

        # 1) 证据检索（失败即 run 失败）
        evidences: list[EvidenceInput] = []
        if self._search_evidence is not None:
            try:
                evidences = await self._search_evidence(run.tenant_id, dataset_ids, question)
            except CapabilityNotReadyError:
                raise
            except Exception as exc:
                await self._mark_state(
                    run.id, "failed", error={"code": "retrieval_failed", "message": str(exc)}
                )
                yield await self._emit(run, "run.failed", {"code": "retrieval_failed", "message": str(exc)})
                return
        evidences = evidences[:_MAX_EVIDENCE]

        if await self._is_cancelled(run.id):
            yield await self._emit(run, "run.cancelled", {"reason": "client_requested"})
            return

        if not evidences:
            # 无证据明确拒答（不编造）
            await self._mark_state(
                run.id,
                "done",
                request_snapshot={**(run.request_snapshot or {}), "refused": True},
            )
            yield await self._emit(run, "retrieval.empty", {"message": "未检索到可用于回答的依据"})
            yield await self._emit(run, "answer.refused", {"message": "资料中未找到依据，无法回答该问题。"})
            return

        # 2) 固定引用：生成前冻结证据（revision/权限/正文快照）
        await self._freeze_evidence(run, evidences)
        yield await self._emit(
            run,
            "retrieval.done",
            {
                "evidences": [
                    {
                        "evidence_id": f"e{index + 1}",
                        "chunk_id": str(evidence.chunk_id),
                        "version_id": str(evidence.version_id),
                        "chunk_revision": evidence.chunk_revision,
                        "text_preview": evidence.text[:_PREVIEW_CHARS],
                    }
                    for index, evidence in enumerate(evidences)
                ]
            },
        )

        # 3) 生成（未配置 generation 用途绑定 → 明确失败）
        if await self._is_cancelled(run.id):
            yield await self._emit(run, "run.cancelled", {"reason": "client_requested"})
            return
        if self._generate_resolver is None:
            await self._mark_state(
                run.id, "failed", error={"code": "capability_not_ready", "message": "生成未装配"}
            )
            yield await self._emit(
                run, "run.failed", {"code": "capability_not_ready", "message": "生成未装配"}
            )
            return
        handle = await self._generate_resolver(run.tenant_id)
        if handle is None:
            await self._mark_state(
                run.id,
                "failed",
                error={
                    "code": "capability_not_ready",
                    "message": "缺少 generation 用途绑定（模型能力档案 + 连接）",
                },
            )
            yield await self._emit(
                run,
                "run.failed",
                {
                    "code": "capability_not_ready",
                    "message": "缺少 generation 用途绑定（模型能力档案 + 连接）",
                },
            )
            return

        prompt = self._build_prompt(question, evidences, await self._load_history(run))
        prompt_tokens = estimate_tokens("".join(message["content"] for message in prompt))
        max_tokens = budget_max_tokens(handle.budget)
        if max_tokens is not None and prompt_tokens > max_tokens:
            prompt_usage = {
                "prompt_tokens_estimated": prompt_tokens,
                "total_tokens_estimated": prompt_tokens,
                "source": "estimate",
            }
            detail = {
                "code": "budget_exceeded",
                "max_tokens": max_tokens,
                "used_tokens": prompt_tokens,
            }
            await self._mark_state(
                run.id,
                "failed",
                error=detail,
                request_snapshot={**(run.request_snapshot or {}), "usage": prompt_usage},
            )
            yield await self._emit(
                run,
                "run.failed",
                {
                    **detail,
                    "message": "问题与证据已超出本次 token 预算，未调用模型",
                },
            )
            return

        answer_text = ""
        completion_chars = 0
        delta_count = 0
        used_usage: dict[str, int] | None = None
        exceeded: dict[str, Any] | None = None

        def record_usage(values: dict[str, int]) -> None:
            nonlocal used_usage
            used_usage = values

        stream = handle.stream(prompt, on_usage=record_usage if max_tokens is not None else None)
        try:
            async for delta in stream:
                delta_count += 1
                if delta_count % _CANCEL_CHECK_EVERY == 0 and await self._is_cancelled(run.id):
                    yield await self._emit(run, "run.cancelled", {"reason": "client_requested"})
                    return
                proposed_chars = completion_chars + len(delta)
                if max_tokens is not None:
                    proposed_total = prompt_tokens + _estimate_tokens_from_chars(proposed_chars)
                    if proposed_total > max_tokens:
                        exceeded = {
                            "max_tokens": max_tokens,
                            "used_tokens": proposed_total,
                            "source": "estimate",
                        }
                        break
                answer_text += delta
                completion_chars = proposed_chars
                yield await self._emit(run, "answer.delta", {"text": delta})
        except CapabilityNotReadyError as exc:
            await self._mark_state(
                run.id, "failed", error={"code": "capability_not_ready", "message": str(exc)}
            )
            yield await self._emit(run, "run.failed", {"code": "capability_not_ready", "message": str(exc)})
            return
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

        if exceeded is not None:
            stream_usage = {
                "prompt_tokens_estimated": prompt_tokens,
                "completion_tokens_estimated": _estimate_tokens_from_chars(completion_chars),
                "total_tokens_estimated": exceeded["used_tokens"],
                "source": "estimate",
            }
            await self._mark_state(
                run.id,
                "failed",
                error={"code": "budget_exceeded", **exceeded},
                request_snapshot={**(run.request_snapshot or {}), "usage": stream_usage},
            )
            yield await self._emit(
                run,
                "run.failed",
                {
                    "code": "budget_exceeded",
                    **exceeded,
                    "message": "生成长度已超出本次 token 预算，已中止",
                },
            )
            return

        # 流在检查点之间结束时仍需确认取消（短回答也能及时终止）
        if await self._is_cancelled(run.id):
            yield await self._emit(run, "run.cancelled", {"reason": "client_requested"})
            return

        final_usage: dict[str, Any]
        if used_usage is not None:
            final_usage = {**used_usage, "source": "provider"}
        else:
            completion_tokens = _estimate_tokens_from_chars(completion_chars)
            final_usage = {
                "prompt_tokens_estimated": prompt_tokens,
                "completion_tokens_estimated": completion_tokens,
                "total_tokens_estimated": prompt_tokens + completion_tokens,
                "source": "estimate",
            }
        if max_tokens is not None:
            total_tokens = final_usage.get("total_tokens") or final_usage.get("total_tokens_estimated")
            total_tokens = int(total_tokens) if isinstance(total_tokens, (int, float, str)) else 0
            if total_tokens > max_tokens:
                detail = {
                    "code": "budget_exceeded",
                    "max_tokens": max_tokens,
                    "used_tokens": total_tokens,
                    "source": final_usage.get("source"),
                }
                await self._mark_state(
                    run.id,
                    "failed",
                    error=detail,
                    request_snapshot={**(run.request_snapshot or {}), "usage": final_usage},
                )
                yield await self._emit(
                    run,
                    "run.failed",
                    {
                        **detail,
                        "message": "本次生成已超出 token 预算，已拒绝完成答案",
                    },
                )
                return

        answer = answer_text

        # 4) 伪引用拒绝：越界编号即失败（不落 answer.done）
        cited = sorted({int(value) for value in _CITATION_RE.findall(answer)})
        invalid = [value for value in cited if value < 1 or value > len(evidences)]
        if invalid:
            await self._mark_state(
                run.id,
                "failed",
                error={"code": "pseudo_citation", "invalid_citations": invalid},
                request_snapshot={**(run.request_snapshot or {}), "usage": final_usage},
            )
            yield await self._emit(
                run,
                "citation.rejected",
                {
                    "invalid_citations": invalid,
                    "evidence_count": len(evidences),
                    "message": "答案引用了证据范围之外的编号，已拒绝",
                },
            )
            return

        await self._mark_state(
            run.id,
            "done",
            request_snapshot={**(run.request_snapshot or {}), "usage": final_usage},
        )
        yield await self._emit(
            run,
            "answer.done",
            {
                "text": answer,
                "citations": [f"e{value}" for value in cited],
                "usage": final_usage,
            },
        )

    # ------------------------------------------------------------------
    # 内部助手
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        question: str,
        evidences: list[EvidenceInput],
        history: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        evidence_block = "\n\n".join(
            f"[{index + 1}] {evidence.text}" for index, evidence in enumerate(evidences)
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        messages.extend(history or [])
        messages.append(
            {
                "role": "user",
                "content": f"证据：\n{evidence_block}\n\n问题：{question}",
            }
        )
        return messages

    async def _load_history(self, run: ChatRunRow, *, max_turns: int = 6) -> list[dict[str, str]]:
        """加载父链历史（最多 max_turns 轮）：仅已完成/拒答的问答进入上下文。"""
        chain: list[ChatRunRow] = []
        parent_id = run.parent_run_id
        async with self._uow.read_only() as session:
            while parent_id is not None and len(chain) < max_turns:
                parent = (
                    await session.execute(
                        select(ChatRunRow).where(
                            ChatRunRow.id == parent_id,
                            ChatRunRow.tenant_id == run.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if parent is None:
                    break
                chain.append(parent)
                parent_id = parent.parent_run_id
            if not chain:
                return []
            events = (
                (
                    await session.execute(
                        select(ChatEventRow)
                        .where(
                            ChatEventRow.run_id.in_([parent.id for parent in chain]),
                            ChatEventRow.event_type.in_(("answer.done", "answer.refused")),
                        )
                        .order_by(ChatEventRow.seq)
                    )
                )
                .scalars()
                .all()
            )
        latest: dict[uuid.UUID, ChatEventRow] = {}
        for row_event in events:
            latest[row_event.run_id] = row_event
        history: list[dict[str, str]] = []
        for parent in reversed(chain):
            question = str((parent.request_snapshot or {}).get("question") or "").strip()
            latest_event = latest.get(parent.id)
            if not question or latest_event is None:
                continue
            payload = latest_event.payload or {}
            answer = (
                payload.get("text") if latest_event.event_type == "answer.done" else payload.get("message")
            )
            if isinstance(answer, str) and answer.strip():
                history.append({"role": "user", "content": question})
                history.append({"role": "assistant", "content": answer})
        return history

    async def _freeze_evidence(self, run: ChatRunRow, evidences: list[EvidenceInput]) -> None:
        async with self._uow.begin() as session:
            for index, evidence in enumerate(evidences):
                session.add(
                    EvidenceSnapshotRow(
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        evidence_id=f"e{index + 1}",
                        doc_id=evidence.doc_id,
                        version_id=evidence.version_id,
                        chunk_id=evidence.chunk_id,
                        chunk_revision=evidence.chunk_revision,
                        acl_refs=list(evidence.acl_refs),
                        text_snapshot=evidence.text,
                        source_refs=list(evidence.source_refs),
                    )
                )

    async def _load_run(self, run_id: uuid.UUID) -> ChatRunRow:
        async with self._uow.read_only() as session:
            row = (
                await session.execute(select(ChatRunRow).where(ChatRunRow.id == run_id))
            ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("问答运行不存在")
        return row

    async def _is_cancelled(self, run_id: uuid.UUID) -> bool:
        async with self._uow.read_only() as session:
            state = (
                await session.execute(select(ChatRunRow.state).where(ChatRunRow.id == run_id))
            ).scalar_one_or_none()
        return state == "cancelled"

    async def _mark_state(
        self,
        run_id: uuid.UUID,
        state: str,
        *,
        error: dict[str, Any] | None = None,
        request_snapshot: dict[str, Any] | None = None,
    ) -> None:
        async with self._uow.begin() as session:
            row = (await session.execute(select(ChatRunRow).where(ChatRunRow.id == run_id))).scalar_one()
            row.state = state
            if error is not None:
                row.error = error
            if request_snapshot is not None:
                row.request_snapshot = request_snapshot

    async def _emit(self, run: ChatRunRow, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """持久化一条事件并返回 SSE 数据字典（seq 由 max+1 保证单调）。"""
        async with self._uow.begin() as session:
            latest = (
                await session.execute(
                    select(ChatEventRow.seq)
                    .where(ChatEventRow.run_id == run.id)
                    .order_by(ChatEventRow.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            seq = (latest or 0) + 1
            session.add(
                ChatEventRow(
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    seq=seq,
                    schema_version=_EVENT_SCHEMA_VERSION,
                    event_type=event_type,
                    payload=payload,
                )
            )
        return {"seq": seq, "type": event_type, "run_id": str(run.id), "payload": payload}

    @staticmethod
    def _run_info(row: ChatRunRow) -> ChatRunInfo:
        return ChatRunInfo(
            id=row.id,
            tenant_id=row.tenant_id,
            actor_kind=row.actor_kind,
            state=row.state,
            dataset_ids=[str(value) for value in (row.dataset_ids or [])],
            request_snapshot=row.request_snapshot or {},
            error=row.error,
            parent_run_id=row.parent_run_id,
        )

    @staticmethod
    def _event_info(row: ChatEventRow) -> ChatEventInfo:
        return ChatEventInfo(seq=row.seq, event_type=row.event_type, payload=row.payload or {})
