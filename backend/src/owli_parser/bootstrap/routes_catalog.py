"""数据集、文档、版本、分块与产物内容路由。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from owli_parser.bootstrap.dependencies import (
    ActorState,
    get_container,
    require_tenant_admin,
    resolve_actor,
    resolve_scope,
)
from owli_parser.core.errors import (
    CapabilityNotReadyError,
    InvalidInputError,
    NotFoundError,
    RevisionConflictError,
)
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.knowledge.models import ChunkRow
from owli_parser.modules.retrieval.golden_runner import GoldenRunner
from owli_parser.ports.storage import ByteRange

router = APIRouter(tags=["catalog"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetCreate(_Strict):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    visibility: str = "tenant"
    storage_profile_id: uuid.UUID | None = None
    chunk_policy: dict[str, Any] | None = None


class DatasetPatch(_Strict):
    expected_revision: int
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    visibility: str | None = None
    allow_partial: bool | None = None
    chunk_policy: dict[str, Any] | None = None
    storage_profile_id: uuid.UUID | None = None


class DocumentPatch(_Strict):
    expected_revision: int
    filename: str = Field(min_length=1, max_length=512)


class ChunkPatch(_Strict):
    text: str | None = None
    enabled: bool | None = None


class ReviewResolve(_Strict):
    action: str
    note: str | None = Field(default=None, max_length=512)


class RegressionRunRequest(_Strict):
    golden_set_id: uuid.UUID


class RetrievalTestRequest(_Strict):
    query: str = Field(min_length=1, max_length=2000)
    dataset_ids: list[uuid.UUID] = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=100)
    # 召回测试台可调参数（缺省取配置；candidate_limit 不可覆盖）
    per_channel_limit: int | None = Field(default=None, ge=1, le=200)
    rrf_k: int | None = Field(default=None, ge=1, le=1000)
    channel_weights: dict[str, float] | None = None
    min_relevance_threshold: float | None = Field(default=None, ge=0.0)


def _require_if_match(if_match: str | None, expected_revision: int) -> None:
    if if_match is None:
        raise RevisionConflictError("修改资源必须携带 If-Match 头")
    try:
        token = int(if_match.strip('"'))
    except ValueError as exc:
        raise InvalidInputError("If-Match 格式不合法") from exc
    if token != expected_revision:
        raise RevisionConflictError("If-Match 与服务端 revision 不一致")


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------


@router.get("/datasets")
async def list_datasets(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    container = get_container(state.request)
    items, next_cursor, has_more = await container.catalog.list_datasets(scope, cursor=cursor, limit=limit)
    return {
        "items": [
            {
                "id": str(d.id),
                "name": d.name,
                "description": d.description,
                "visibility": d.visibility,
                "owner_user_id": str(d.owner_user_id),
                "revision": d.revision,
                "allow_partial": d.allow_partial,
                "storage_profile_id": str(d.storage_profile_id) if d.storage_profile_id else None,
                "created_at": d.created_at,
            }
            for d in items
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.post("/datasets", status_code=201)
async def create_dataset(
    payload: DatasetCreate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    info = await container.catalog.create_dataset(
        scope,
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
        storage_profile_id=payload.storage_profile_id,
        chunk_policy=payload.chunk_policy,
    )
    return {"id": str(info.id), "revision": info.revision}


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    d = await container.catalog.get_dataset(scope, dataset_id)
    return {
        "id": str(d.id),
        "name": d.name,
        "description": d.description,
        "visibility": d.visibility,
        "owner_user_id": str(d.owner_user_id),
        "revision": d.revision,
        "auth_revision": d.auth_revision,
        "allow_partial": d.allow_partial,
        "chunk_policy": d.chunk_policy,
        "storage_profile_id": str(d.storage_profile_id) if d.storage_profile_id else None,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


@router.patch("/datasets/{dataset_id}")
async def patch_dataset(
    dataset_id: uuid.UUID,
    payload: DatasetPatch,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    _require_if_match(if_match, payload.expected_revision)
    container = get_container(state.request)
    d = await container.catalog.update_dataset(
        scope,
        dataset_id,
        expected_revision=payload.expected_revision,
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
        allow_partial=payload.allow_partial,
        chunk_policy=payload.chunk_policy,
        storage_profile_id=payload.storage_profile_id,
    )
    return {"id": str(d.id), "revision": d.revision}


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(
    dataset_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> Response:
    container = get_container(state.request)
    await container.catalog.delete_dataset(scope, dataset_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 文档与版本
# ---------------------------------------------------------------------------


@router.get("/documents")
async def list_documents(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    dataset_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    include_deleted: bool = Query(default=False),
) -> dict[str, Any]:
    container = get_container(state.request)
    items, next_cursor, has_more = await container.catalog.list_documents(
        scope, dataset_id=dataset_id, cursor=cursor, limit=limit, include_deleted=include_deleted
    )
    return {
        "items": [
            {
                "id": str(d.id),
                "dataset_id": str(d.dataset_id),
                "filename": d.filename,
                "mime": d.mime,
                "size_bytes": d.size_bytes,
                "content_hash": d.content_hash,
                "visible_version_id": str(d.visible_version_id) if d.visible_version_id else None,
                "desired_version_id": str(d.desired_version_id) if d.desired_version_id else None,
                "revision": d.revision,
                "deleted_at": d.deleted_at,
                "created_at": d.created_at,
                "updated_at": d.updated_at,
            }
            for d in items
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    d = await container.catalog.get_document(scope, document_id)
    return {
        "id": str(d.id),
        "dataset_id": str(d.dataset_id),
        "filename": d.filename,
        "mime": d.mime,
        "size_bytes": d.size_bytes,
        "content_hash": d.content_hash,
        "visible_version_id": str(d.visible_version_id) if d.visible_version_id else None,
        "desired_version_id": str(d.desired_version_id) if d.desired_version_id else None,
        "revision": d.revision,
        "deleted_at": d.deleted_at,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


@router.patch("/documents/{document_id}")
async def patch_document(
    document_id: uuid.UUID,
    payload: DocumentPatch,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    _require_if_match(if_match, payload.expected_revision)
    container = get_container(state.request)
    d = await container.catalog.rename_document(
        scope, document_id, filename=payload.filename, expected_revision=payload.expected_revision
    )
    return {"id": str(d.id), "revision": d.revision}


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> Response:
    container = get_container(state.request)
    await container.catalog.delete_document(scope, document_id)
    return Response(status_code=204)


@router.get("/documents/{document_id}/versions")
async def list_versions(
    document_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    versions = await container.catalog.list_versions(scope, document_id)
    # 版本质量摘要：待复核数一次聚合（前端版本选择器徽标）
    review_counts = await container.knowledge.count_open_reviews_by_version(
        scope.tenant_id, [v.id for v in versions]
    )
    return [
        {
            "id": str(v.id),
            "version_no": v.version_no,
            "lifecycle": v.lifecycle,
            "coverage": v.coverage,
            "quality_report": v.quality_report,
            "pending_review_count": review_counts.get(v.id, 0),
            "ir_artifact_id": str(v.ir_artifact_id) if v.ir_artifact_id else None,
            "created_at": v.created_at,
        }
        for v in versions
    ]


@router.get("/documents/{document_id}/ir")
async def get_ir(
    document_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    version_id: uuid.UUID | None = Query(default=None),
) -> Response:
    """版本化 IR（含 source_refs 与质量报告）；完整 IR 用于受权导出。"""
    container = get_container(state.request)
    version = await container.catalog.get_version(scope, document_id, version_id)
    if version.ir_artifact_id is None:
        raise NotFoundError("该版本尚未生成 IR")
    location, profile = await container.artifacts.resolve_location(scope.tenant_id, version.ir_artifact_id)
    reader = container.artifacts.open_reader(location, profile)
    try:
        data = reader.read()
    finally:
        reader.close()
    return Response(
        content=data,
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


# 进程内 IR 缓存：artifact 不可变，按 artifact_id 缓存解析结果；
# 上限 32 个版本，避免千页 IR 在每次分页请求中重复反序列化。
_IR_CACHE: dict[str, Any] = {}
_IR_CACHE_LIMIT = 32


async def _load_ir(container: Any, tenant_id: uuid.UUID, ir_artifact_id: uuid.UUID) -> Any:
    """按 artifact_id 读取并缓存 DocumentIR（artifact 不可变，缓存安全）。"""
    from owli_parser.contracts.ir import DocumentIR as _DocumentIR

    cache_key = str(ir_artifact_id)
    ir = _IR_CACHE.get(cache_key)
    if ir is not None:
        return ir
    location, profile = await container.artifacts.resolve_location(tenant_id, ir_artifact_id)
    reader = container.artifacts.open_reader(location, profile)
    try:
        data = reader.read()
    finally:
        reader.close()
    ir = _DocumentIR.model_validate_json(data)
    if len(_IR_CACHE) >= _IR_CACHE_LIMIT:
        _IR_CACHE.pop(next(iter(_IR_CACHE)))
    _IR_CACHE[cache_key] = ir
    return ir


_PREVIEW_KINDS: dict[str, str] = {
    "application/pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "image/tiff": "image",
    "image/bmp": "image",
    "image/webp": "image",
    "image/gif": "image",
}


@router.get("/documents/{document_id}/preview")
async def get_preview_descriptor(
    document_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    version_id: uuid.UUID | None = Query(default=None),
) -> dict[str, Any]:
    """预览 descriptor：前端据此选择已安装 renderer 并获取受权资源路径。

    预览状态、解析状态、索引状态分别展示：本接口只描述预览；
    “可以预览”不代表“已解析”，“已解析”不代表“已发布可检索”。
    """
    container = get_container(state.request)
    document = await container.catalog.get_document(scope, document_id)
    # 预览针对原件：未发布版本也可预览（回退最新候选），与解析/检索状态解耦
    version = await container.catalog.get_version(scope, document_id, version_id, fallback_latest=True)
    artifact = await container.artifacts.get_artifact(scope.tenant_id, version.source_artifact_id)
    mime = artifact.mime or ""
    kind = _PREVIEW_KINDS.get(mime, "unsupported")
    if kind == "unsupported" and mime.startswith("text/"):
        kind = "text"
    if kind == "unsupported" and mime in ("application/json", "application/xml"):
        kind = "text"
    pages: list[dict[str, Any]] = []
    if kind == "pdf" and version.ir_artifact_id is not None:
        try:
            ir = await _load_ir(container, scope.tenant_id, version.ir_artifact_id)
            pages = [
                {
                    "page_index": page.page_index,
                    "geometry_id": page.geometry_id,
                    "width": page.width,
                    "height": page.height,
                    "rotation": page.rotation,
                    "width_unit": page.width_unit,
                }
                for page in ir.pages
            ]
        except Exception:
            pages = []
    return {
        "document_id": str(document.id),
        "version_id": str(version.id),
        "artifact_id": str(artifact.id),
        "filename": document.filename,
        "mime": mime or None,
        "kind": kind,
        "size_bytes": artifact.size_bytes,
        # 受权流式资源路径（前端同源 fetch/window fetch 携带会话 Cookie；
        # 服务端在每次请求重新鉴权，缓存不绕过权限）
        "content_path": (
            f"/v1/artifacts/{artifact.id}/content?document_id={document.id}&version_id={version.id}"
        ),
        "pages": pages,
        "status": "ready",
    }


@router.get("/documents/{document_id}/blocks")
async def list_blocks(
    document_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    version_id: uuid.UUID | None = Query(default=None),
    block_type: str | None = Query(default=None, alias="type"),
    parent_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """分页 blocks（交互端）；完整 IR 仍走 /ir 受权导出，不要求一次下载千页。"""

    container = get_container(state.request)
    version = await container.catalog.get_version(scope, document_id, version_id)
    if version.ir_artifact_id is None:
        raise NotFoundError("该版本尚未生成 IR")
    ir = await _load_ir(container, scope.tenant_id, version.ir_artifact_id)
    blocks = list(ir.blocks)
    if block_type:
        blocks = [b for b in blocks if b.type == block_type]
    if parent_id:
        blocks = [b for b in blocks if b.parent_id == parent_id]
    offset = 0
    if cursor:
        from owli_parser.core.pagination import decode_cursor

        offset = int(decode_cursor(cursor).get("offset", 0))
    window = blocks[offset : offset + limit]
    has_more = offset + limit < len(blocks)
    from owli_parser.core.pagination import encode_cursor

    return {
        "items": [b.model_dump(mode="json") for b in window],
        "next_cursor": encode_cursor({"offset": offset + limit}) if has_more else None,
        "has_more": has_more,
        "total": len(blocks),
        "version_id": str(version.id),
    }


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------


@router.get("/documents/{document_id}/chunks")
async def list_chunks(
    document_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    version_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    include_disabled: bool = Query(default=False),
) -> dict[str, Any]:
    container = get_container(state.request)
    await container.catalog.get_document(scope, document_id)  # 读取权限校验
    items, next_cursor, has_more = await container.knowledge.list_chunks(
        scope.tenant_id,
        document_id=document_id,
        version_id=version_id,
        cursor=cursor,
        limit=limit,
        include_disabled=include_disabled,
    )
    return {
        "items": [_chunk_payload(c) for c in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.get("/chunks/{chunk_id}")
async def get_chunk(
    chunk_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    chunk = await container.knowledge.get_chunk(scope.tenant_id, chunk_id)
    await container.catalog.get_document(scope, chunk.doc_id)  # 读取权限校验
    return _chunk_payload(chunk)


@router.patch("/chunks/{chunk_id}")
async def patch_chunk(
    chunk_id: uuid.UUID,
    payload: ChunkPatch,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    container = get_container(state.request)
    existing = await container.knowledge.get_chunk(scope.tenant_id, chunk_id)
    # 写权限统一由数据集授权判定：用户 viewer 拒绝；应用需 write grant
    # （documents:write scope 已由依赖层集中强制）
    await container.catalog.require_dataset_write(scope, existing.dataset_id)
    if if_match is None:
        raise RevisionConflictError("修改分块必须携带 If-Match 头")
    try:
        expected_revision = int(if_match.strip('"'))
    except ValueError as exc:
        raise InvalidInputError("If-Match 格式不合法") from exc
    chunk = await container.knowledge.edit_chunk(
        scope.tenant_id,
        chunk_id,
        expected_revision=expected_revision,
        text=payload.text,
        enabled=payload.enabled,
        created_by_kind=str(scope.kind),
        created_by_id=scope.actor_id or uuid.uuid4(),
    )
    return _chunk_payload(chunk)


# ---------------------------------------------------------------------------
# 复核队列（低置信度 / 缺失单位人工审核）
# ---------------------------------------------------------------------------


@router.get("/review/queue")
async def list_review_queue(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    status: str | None = Query(default="open"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    reason: str | None = Query(default=None),
    version_id: uuid.UUID | None = Query(default=None),
) -> dict[str, Any]:
    container = get_container(state.request)
    items, next_cursor, has_more = await container.knowledge.list_review_items(
        scope.tenant_id,
        status=status,
        cursor=cursor,
        limit=limit,
        reason=reason,
        version_id=version_id,
    )
    return {
        "items": [
            {
                "id": str(i.id),
                "version_id": str(i.version_id),
                "document_id": str(i.document_id) if i.document_id else None,
                "job_id": str(i.job_id) if i.job_id else None,
                "block_id": i.block_id,
                "reason": i.reason,
                "status": i.status,
                "reviewer_id": str(i.reviewer_id) if i.reviewer_id else None,
                "resolution": i.resolution,
                "created_at": i.created_at,
            }
            for i in items
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.post("/review/queue/{review_item_id}/resolve")
async def resolve_review_item(
    review_item_id: uuid.UUID,
    payload: ReviewResolve,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    item = await container.knowledge.resolve_review_item(
        scope.tenant_id,
        review_item_id,
        action=payload.action,
        note=payload.note,
        reviewer_id=scope.actor_id,
    )
    return {"id": str(item.id), "status": item.status, "resolution": item.resolution}


# ---------------------------------------------------------------------------
# 检索测试台与索引动作（M3 能力；未启用时明确返回 capability_not_ready，
# 不得显示虚构可用能力）
# ---------------------------------------------------------------------------


@router.post("/retrieval/test")
async def retrieval_test(
    payload: RetrievalTestRequest,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    if not container.settings.retrieval.enabled:
        raise CapabilityNotReadyError("检索通道在 M3 里程碑启用（retrieval.enabled=false）")
    if payload.channel_weights is not None:
        unknown = set(payload.channel_weights) - {"dense", "lexical"}
        if unknown:
            raise InvalidInputError(f"未知通道权重：{sorted(unknown)}")
        if any(weight < 0 or weight > 10 for weight in payload.channel_weights.values()):
            raise InvalidInputError("通道权重须在 [0, 10] 区间")

    async def authoritative_filter(candidates: Any) -> set[str]:
        return await _authoritative_chunk_ids(container, scope, candidates)

    result = await container.retrieval.search(
        tenant_id=str(scope.tenant_id),
        query=payload.query,
        # embedding 用途绑定在 M3 接入；当前仅 lexical 通道参与召回
        query_vector=None,
        dataset_ids=[str(d) for d in payload.dataset_ids] or None,
        acl_refs=[f"dataset:{d}" for d in payload.dataset_ids] or None,
        authoritative_filter=authoritative_filter,
        top_k=payload.top_k,
        per_channel_limit=payload.per_channel_limit,
        rrf_k=payload.rrf_k,
        channel_weights=payload.channel_weights,
        min_relevance_threshold=payload.min_relevance_threshold,
    )
    return {
        "items": [
            {
                "id": item.id,
                "rrf_score": item.rrf_score,
                "channel_ranks": item.channel_ranks,
                "channel_raw_scores": item.channel_raw_scores,
                "channel_contributions": item.channel_contributions,
                "rerank_score": item.rerank_score,
                "payload": item.payload,
            }
            for item in result.items
        ],
        "channels_used": result.channels_used,
        "applied_filters": result.applied_filters,
        "applied_params": result.applied_params,
        "low_relevance": result.low_relevance,
        "rerank_applied": result.rerank_applied,
        "warnings": result.warnings,
    }


async def _authoritative_chunk_ids(container: Any, scope: CatalogScope, candidates: Any) -> set[str]:
    """权威校验：候选 chunk 必须以 PG 为准（存在、enabled、revision 未过期）。

    索引态不可作为内容有效性事实源；不通过校验的候选不得进入结果。
    """
    specs: dict[uuid.UUID, int] = {}
    for hit in candidates:
        chunk_id = hit.payload.get("chunk_id")
        revision = hit.payload.get("chunk_revision")
        if isinstance(chunk_id, str) and isinstance(revision, int):
            try:
                specs[uuid.UUID(chunk_id)] = revision
            except ValueError:
                continue
    if not specs:
        return set()
    async with container.uow.read_only() as session:
        rows = (
            await session.execute(
                select(ChunkRow.id, ChunkRow.current_revision, ChunkRow.enabled).where(
                    ChunkRow.tenant_id == scope.tenant_id,
                    ChunkRow.id.in_(list(specs.keys())),
                )
            )
        ).all()
    valid = {str(row[0]) for row in rows if row[2] and row[1] == specs[row[0]]}
    return {hit.id for hit in candidates if str(hit.payload.get("chunk_id")) in valid}


# ---------------------------------------------------------------------------
# 质量回归（黄金集运行记录；文档 §9 检索基线）
# ---------------------------------------------------------------------------

# 同步执行上限：超过请走 CLI（避免 API 请求长时间阻塞）
_REGRESSION_MAX_CASES = 200


@router.post("/quality/regression-runs")
async def trigger_regression_run(
    payload: RegressionRunRequest,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """同步执行黄金集回归（admin）；结果落 golden_runs 并返回判定与指标。"""
    container = get_container(state.request)
    if not container.settings.retrieval.enabled:
        raise CapabilityNotReadyError("检索通道未启用（retrieval.enabled=false）")

    golden = await container.knowledge.get_golden_set_by_id(payload.golden_set_id)
    if golden is None or (golden.tenant_id is not None and golden.tenant_id != scope.tenant_id):
        # 跨租户不泄露存在性
        raise NotFoundError("黄金集不存在")
    if len(golden.cases or []) > _REGRESSION_MAX_CASES:
        raise InvalidInputError(f"用例数超过同步上限（{_REGRESSION_MAX_CASES}），请使用 CLI 评估")

    from owli_parser.bootstrap.quality_runner import make_retrieval_search_fn

    search_fn = make_retrieval_search_fn(container, scope.tenant_id)
    runner = GoldenRunner(container.knowledge, container.clock)
    run = await runner.run(golden_set=golden, search_fn=search_fn, adapter_ref="lexical-bm25")
    return {
        "run_id": run.run_id,
        "passed": run.passed,
        "violations": run.violations,
        "aggregate": run.aggregate,
    }


@router.get("/quality/regression-runs")
async def list_regression_runs(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    limit: int = Query(default=20, ge=1, le=100),
    golden_set_id: uuid.UUID | None = Query(default=None),
) -> dict[str, Any]:
    """黄金集回归运行列表：仅指标与判定，不含被检索内容；可按黄金集过滤。"""
    del scope
    container = get_container(state.request)
    runs = await container.knowledge.list_golden_runs(limit=limit, golden_set_id=golden_set_id)
    return {
        "items": [
            {
                "id": str(run.id),
                "golden_set_id": str(run.golden_set_id),
                "golden_set_name": run.golden_set_name,
                "run_id": run.run_id,
                "adapter_ref": run.adapter_ref,
                "config_digest": run.config_digest,
                "state": run.state,
                "created_at": run.created_at,
                "aggregate": (run.metrics or {}).get("aggregate", {}),
                "passed": (run.metrics or {}).get("passed"),
                "violations": (run.metrics or {}).get("violations", []),
            }
            for run in runs
        ]
    }


@router.get("/quality/regression-runs/{run_id}")
async def get_regression_run(
    run_id: str,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    """回归运行详情：含逐 case 指标（不含被检索正文）；跨租户 404。"""
    container = get_container(state.request)
    run = await container.knowledge.get_golden_run(run_id)
    if run is None or (run.tenant_id is not None and run.tenant_id != scope.tenant_id):
        raise NotFoundError("回归运行不存在")
    metrics = run.metrics or {}
    return {
        "id": str(run.id),
        "golden_set_id": str(run.golden_set_id),
        "golden_set_name": run.golden_set_name,
        "run_id": run.run_id,
        "adapter_ref": run.adapter_ref,
        "config_digest": run.config_digest,
        "state": run.state,
        "created_at": run.created_at,
        "aggregate": metrics.get("aggregate", {}),
        "passed": metrics.get("passed"),
        "violations": metrics.get("violations", []),
        "cases": metrics.get("cases", []),
    }


@router.post("/chunks/{chunk_id}/reembed")
async def reembed_chunk(
    chunk_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    """按 PG 当前 revision 重建单块索引（编辑后手动触发）。

    旧 revision 的索引条目不清删，由检索侧权威校验自然失效——不会召回旧内容。
    """
    container = get_container(state.request)
    if not container.settings.retrieval.enabled:
        raise CapabilityNotReadyError("索引通道未启用（retrieval.enabled=false）")
    existing = await container.knowledge.get_chunk(scope.tenant_id, chunk_id)
    await container.catalog.require_dataset_write(scope, existing.dataset_id)
    return await container.index_publication.reindex_chunk(tenant_id=scope.tenant_id, chunk_id=chunk_id)


# ---------------------------------------------------------------------------
# 产物内容（受权流式下载）
# ---------------------------------------------------------------------------


@router.get("/artifacts/{artifact_id}/content")
async def artifact_content(
    artifact_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    document_id: uuid.UUID | None = Query(default=None),
    version_id: uuid.UUID | None = Query(default=None),
) -> Response:
    """受权流式资源；要求与文档/版本关联（非"知道 ID 即可下载"）。"""
    container = get_container(state.request)
    if document_id is None:
        raise InvalidInputError("必须提供 document_id 以关联授权上下文")
    doc = await container.catalog.get_document(scope, document_id)
    if version_id is not None:
        version = await container.catalog.get_version(scope, document_id, version_id)
        allowed_ids = {
            str(version.source_artifact_id),
            str(version.ir_artifact_id) if version.ir_artifact_id else "",
        }
    else:
        allowed_ids = {str(doc.id)}
    if doc.content_hash is None:
        raise NotFoundError("文档缺少内容")
    if str(artifact_id) not in allowed_ids:
        # 通过版本列表逐一核对（防止用其他文档的 artifact 探测）
        versions = await container.catalog.list_versions(scope, document_id)
        known: set[str] = set()
        for v in versions:
            known.add(str(v.source_artifact_id))
            if v.ir_artifact_id:
                known.add(str(v.ir_artifact_id))
        if str(artifact_id) not in known:
            raise NotFoundError("产物不存在或与文档无关联")
    location, profile = await container.artifacts.resolve_location(scope.tenant_id, artifact_id)
    range_header = state.request.headers.get("range")
    byte_range = _parse_range(range_header, location.size_bytes)
    reader = container.artifacts.open_reader(location, profile, byte_range)
    try:
        data = reader.read()
    finally:
        reader.close()
    headers = {
        "Cache-Control": "private, no-store",
        "Accept-Ranges": "bytes",
        "Content-Disposition": 'inline; filename="artifact.bin"',
    }
    status = 200
    if byte_range is not None:
        headers["Content-Range"] = (
            f"bytes {byte_range.start}-{byte_range.end_inclusive}/{location.size_bytes}"
        )
        status = 206
    return Response(content=data, media_type="application/octet-stream", headers=headers, status_code=status)


def _parse_range(range_header: str | None, size: int) -> ByteRange | None:
    if not range_header:
        return None
    if "," in range_header:
        raise InvalidInputError("不支持多段 Range 请求")
    if not range_header.startswith("bytes="):
        raise InvalidInputError("Range 头不合法")
    spec = range_header[len("bytes=") :]
    start_text, _, end_text = spec.partition("-")
    try:
        if start_text == "":
            # 后缀范围 bytes=-N
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start = max(size - suffix, 0)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError as exc:
        raise InvalidInputError("Range 头不合法") from exc
    if start >= size or end < start:
        from owli_parser.core.errors import AppError, ErrorCode

        raise AppError("Range 越界", code=ErrorCode.INVALID_INPUT, http_status=416)
    return ByteRange(start=start, end_inclusive=min(end, size - 1))


def _chunk_payload(c: Any) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "version_id": str(c.version_id),
        "doc_id": str(c.doc_id),
        "dataset_id": str(c.dataset_id),
        "ordinal": c.ordinal,
        "kind": c.kind,
        "parent_id": str(c.parent_id) if c.parent_id else None,
        "revision": c.current_revision,
        "text": c.text,
        "token_count": c.token_count,
        "source_refs": c.source_refs,
        "explanation": c.explanation,
        "structure": c.structure,
        "enabled": c.enabled,
        "embedding_state": c.embedding_state,
    }


__all__ = ["router"]
