"""上传、URL/对象导入与解析任务路由。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from owli_parser.bootstrap.dependencies import (
    ActorState,
    get_container,
    idempotency_context,
    resolve_actor,
    resolve_scope,
)
from owli_parser.core.errors import ConflictError, InvalidInputError, PayloadTooLargeError
from owli_parser.modules.catalog.service import CatalogScope

router = APIRouter(tags=["ingestion"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UploadInit(_Strict):
    dataset_id: uuid.UUID
    filename: str = Field(min_length=1, max_length=512)
    total_bytes: int | None = Field(default=None, gt=0)


class ImportSourceUrl(_Strict):
    kind: str = "url"
    url: str = Field(min_length=1, max_length=2048)
    filename_hint: str | None = Field(default=None, max_length=512)


class ImportSourceStorageObject(_Strict):
    kind: str = "storage_object"
    source_location_id: uuid.UUID
    relative_key: str = Field(min_length=1, max_length=512)
    object_version: str | None = None


class ImportCreate(_Strict):
    dataset_id: uuid.UUID
    source: dict[str, Any]
    pipeline_ref: str | None = None
    document_id: uuid.UUID | None = None
    expected_revision: int | None = None


class ParsingJobCreate(_Strict):
    artifact_id: uuid.UUID
    dataset_id: uuid.UUID
    pipeline_ref: str | None = None
    document_id: uuid.UUID | None = None
    expected_revision: int | None = None
    parse_only: bool = False
    # 原始文件名（仅取 basename；影响预检与 adapter 路由）
    filename: str | None = Field(default=None, max_length=512)


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


@router.post("/uploads/init", status_code=201)
async def upload_init(
    payload: UploadInit,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    info = await container.uploads.init_upload(
        scope,
        dataset_id=payload.dataset_id,
        filename=payload.filename,
        declared_total_bytes=payload.total_bytes,
    )
    return {
        "id": str(info.id),
        "part_size_bytes": info.part_size_bytes,
        "declared_total_bytes": info.declared_total_bytes,
        "expires_at": info.expires_at,
        "state": info.state,
    }


@router.put("/uploads/{upload_id}/parts/{part_no}")
async def upload_part(
    upload_id: uuid.UUID,
    part_no: int,
    file: UploadFile,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    max_part = container.settings.upload.part_size_bytes
    # 有界读取：超过单分片上限直接拒绝（实际字节数以写入结果为准）
    chunks: list[bytes] = []
    read_total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        read_total += len(chunk)
        if read_total > max_part + 1024 * 1024:
            raise PayloadTooLargeError("分片超过单分片上限")
        chunks.append(chunk)
    receipt = await container.uploads.put_part(scope, upload_id, part_no, iter(chunks))
    return receipt


@router.post("/uploads/direct", status_code=201)
async def upload_direct(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    dataset_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """小文件 multipart 直传快捷入口：只产 artifact，不解析。

    大文件请走 init/part/complete 分片流程；边界由 upload.direct_max_bytes 控制。
    """
    container = get_container(state.request)
    artifact_id = await container.uploads.direct_upload(
        scope,
        dataset_id=dataset_id,
        filename=file.filename or "unnamed",
        source_file=file.file,  # 同步 file-like，在线程池中流式读取
    )
    return {"artifact_id": str(artifact_id)}


@router.get("/uploads")
async def list_uploads(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    items = await container.uploads.list_uploads(scope)
    return {
        "items": [
            {
                "id": str(u.id),
                "dataset_id": str(u.dataset_id),
                "filename": u.filename,
                "state": u.state,
                "received_bytes": u.received_bytes,
                "declared_total_bytes": u.declared_total_bytes,
                "artifact_id": str(u.artifact_id) if u.artifact_id else None,
                "expires_at": u.expires_at,
            }
            for u in items
        ]
    }


@router.post("/uploads/{upload_id}/complete")
async def upload_complete(
    upload_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    artifact_id = await container.uploads.complete_upload(scope, upload_id)
    return {"artifact_id": str(artifact_id)}


@router.post("/uploads/{upload_id}/cancel")
async def upload_cancel(
    upload_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, str]:
    container = get_container(state.request)
    await container.uploads.cancel_upload(scope, upload_id)
    return {"status": "cancelled"}


@router.get("/uploads/{upload_id}")
async def upload_status(
    upload_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    info = await container.uploads.get_upload(scope, upload_id)
    return {
        "id": str(info.id),
        "state": info.state,
        "parts": info.parts,
        "received_bytes": info.received_bytes,
        "declared_total_bytes": info.declared_total_bytes,
        "artifact_id": str(info.artifact_id) if info.artifact_id else None,
    }


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


@router.post("/imports", status_code=202)
async def create_import(
    payload: ImportCreate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    idem: Any = Depends(idempotency_context),
) -> dict[str, Any]:
    container = get_container(state.request)
    source = payload.source
    kind = source.get("kind", "url")
    idem_key = idem.key or f"import:{uuid.uuid4()}"
    request_hash = _hash_import_request(payload)
    record = await container.idempotency.begin(
        tenant_id=scope.tenant_id,
        actor_kind=str(scope.kind),
        actor_id=scope.actor_id or uuid.uuid4(),
        operation="imports.create",
        key=idem_key,
        request_hash=request_hash,
    )
    if record.state == "completed" and record.response_ref:
        existing = await container.imports.get_import(scope, uuid.UUID(record.response_ref["id"]))
        return _import_payload(existing, container)
    if kind == "url":
        url = str(source.get("url", "")).strip()
        if not url:
            raise InvalidInputError("source.url 不能为空")
        info = await container.imports.create_url_import(
            scope,
            dataset_id=payload.dataset_id,
            url=url,
            filename_hint=source.get("filename_hint"),
            pipeline_ref=payload.pipeline_ref,
            document_id=payload.document_id,
            expected_revision=payload.expected_revision,
        )
    elif kind == "storage_object":
        raw_location = source.get("source_location_id")
        if not raw_location:
            raise InvalidInputError("source.source_location_id 不能为空")
        info = await container.imports.create_storage_object_import(
            scope,
            dataset_id=payload.dataset_id,
            source_location_id=uuid.UUID(str(raw_location)),
            relative_key=str(source.get("relative_key", "")),
            object_version=source.get("object_version"),
            pipeline_ref=payload.pipeline_ref,
            document_id=payload.document_id,
            expected_revision=payload.expected_revision,
        )
    else:
        raise InvalidInputError("source.kind 只允许 url/storage_object")
    # 登记下载任务（outbox 由 worker/派发器投递；此处直接触发下载由 worker 消费）
    async with container.uow.begin() as session:
        await container.outbox.record(
            aggregate_type="source_import",
            aggregate_id=str(info.id),
            event_type="import.download",
            payload={"import_id": str(info.id)},
            session=session,
        )
    await container.idempotency.complete(record_id=record.id, response_ref={"id": str(info.id)})
    return _import_payload(info, container)


@router.get("/imports")
async def list_imports(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    container = get_container(state.request)
    items, next_cursor, has_more = await container.imports.list_imports(scope, cursor=cursor, limit=limit)
    return {
        "items": [_import_payload(i, container) for i in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.get("/imports/{import_id}")
async def get_import(
    import_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    info = await container.imports.get_import(scope, import_id)
    return _import_payload(info, container)


@router.post("/imports/{import_id}/cancel")
async def cancel_import(
    import_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    info = await container.imports.cancel_import(scope, import_id)
    return _import_payload(info, container)


@router.post("/imports/{import_id}/retry")
async def retry_import(
    import_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    info = await container.imports.retry_import(
        scope, import_id, max_attempts=container.settings.jobs.max_attempts
    )
    async with container.uow.begin() as session:
        await container.outbox.record(
            aggregate_type="source_import",
            aggregate_id=str(info.id),
            event_type="import.download",
            payload={"import_id": str(info.id)},
            session=session,
        )
    return _import_payload(info, container)


def _import_payload(info: Any, container: Any) -> dict[str, Any]:
    return {
        "id": str(info.id),
        "dataset_id": str(info.dataset_id),
        "source_kind": info.source_kind,
        "display_host": info.display_host,
        "display_path": info.display_path,
        "filename_hint": info.filename_hint,
        "state": info.state,
        "attempt": info.attempt,
        "bytes_received": info.bytes_received,
        "total_bytes": info.total_bytes,
        "artifact_id": str(info.artifact_id) if info.artifact_id else None,
        "parsing_job_id": str(info.parsing_job_id) if info.parsing_job_id else None,
        "error": info.error,
        "status_url": f"/v1/imports/{info.id}",
    }


def _hash_import_request(payload: ImportCreate) -> str:
    import hashlib
    import json

    # 请求摘要包含完整 URL 的受保护摘要（此处对规范化 JSON 整体取哈希）
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 解析任务
# ---------------------------------------------------------------------------


@router.post("/parsing/jobs", status_code=202)
async def create_parsing_job(
    payload: ParsingJobCreate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    result = await container.submit_parsing.submit(
        scope,
        artifact_id=payload.artifact_id,
        dataset_id=payload.dataset_id,
        pipeline_ref=payload.pipeline_ref,
        document_id=payload.document_id,
        expected_revision=payload.expected_revision,
        parse_only=payload.parse_only,
        filename=payload.filename,
    )
    return {
        "id": str(result.job_id),
        "state": "PENDING",
        "document_id": str(result.document_id),
        "version_id": str(result.version_id),
        "status_url": f"/v1/parsing/jobs/{result.job_id}",
    }


@router.get("/parsing/jobs")
async def list_parsing_jobs(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    state_filter: str | None = Query(default=None, alias="state"),
    dataset_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    container = get_container(state.request)
    actor_kind = str(scope.kind)
    actor_id = scope.actor_id if scope.kind.value == "application" else None
    items, next_cursor, has_more = await container.jobs.list_jobs(
        scope.tenant_id,
        actor_kind=actor_kind if scope.kind.value == "application" else None,
        actor_id=actor_id,
        state=state_filter,
        dataset_id=dataset_id,
        cursor=cursor,
        limit=limit,
    )
    return {
        "items": [_job_payload(j) for j in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.get("/parsing/jobs/{job_id}")
async def get_parsing_job(
    job_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    job = await container.jobs.get_job(scope.tenant_id, job_id)
    if scope.kind.value == "application" and job.actor_id != scope.actor_id:
        from owli_parser.core.errors import NotFoundError

        raise NotFoundError("任务不存在")
    return _job_payload(job)


@router.get("/parsing/jobs/{job_id}/events")
async def get_job_events(
    job_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    container = get_container(state.request)
    events, latest_seq = await container.jobs.list_events(
        scope.tenant_id, job_id, after_seq=after_seq, limit=limit
    )
    return {
        "items": [
            {
                "seq": e.seq,
                "type": e.event_type,
                "attempt": e.attempt,
                "from_state": e.from_state,
                "to_state": e.to_state,
                "ts": e.ts,
                "detail": e.detail,
            }
            for e in events
        ],
        "latest_seq": latest_seq,
        "cursor_expired": after_seq > 0 and after_seq > latest_seq,
    }


@router.get("/parsing/jobs/{job_id}/steps")
async def list_job_steps(
    job_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
    stage: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> dict[str, Any]:
    """单位级步骤（页/时间窗）：按页记录解析结果，供续跑与运维查询。"""
    container = get_container(state.request)
    job = await container.jobs.get_job(scope.tenant_id, job_id)
    if scope.kind.value == "application" and job.actor_id != scope.actor_id:
        from owli_parser.core.errors import NotFoundError

        raise NotFoundError("任务不存在")
    items, total = await container.jobs.list_steps(scope.tenant_id, job_id, stage=stage, limit=limit)
    return {
        "items": [
            {
                "stage": item.stage,
                "unit_id": item.unit_id,
                "attempt": item.attempt,
                "state": item.state,
                "checkpoint_ref": item.checkpoint_ref,
                "error_code": item.error_code,
                "started_at": item.started_at,
                "finished_at": item.finished_at,
            }
            for item in items
        ],
        "total": total,
        "limit": limit,
    }


@router.post("/parsing/jobs/{job_id}/cancel")
async def cancel_parsing_job(
    job_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    job = await container.jobs.get_job(scope.tenant_id, job_id)
    if scope.kind.value == "application" and job.actor_id != scope.actor_id:
        raise ConflictError("不能取消其他应用的任务")
    changed = await container.jobs.begin_cancelling(job_id)
    if changed:
        await container.jobs.finalize_cancelled(job_id, None)
    latest = await container.jobs.get_job(scope.tenant_id, job_id)
    return _job_payload(latest)


@router.post("/parsing/jobs/{job_id}/retry")
async def retry_parsing_job(
    job_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    job = await container.jobs.retry_job(
        scope.tenant_id, job_id, max_attempts=container.settings.jobs.max_attempts
    )
    async with container.uow.begin() as session:
        await container.outbox.record(
            aggregate_type="parsing_job",
            aggregate_id=str(job.id),
            event_type="job.route",
            payload={"job_id": str(job.id)},
            session=session,
        )
    return _job_payload(job)


def _job_payload(job: Any) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "document_id": str(job.document_id),
        "version_id": str(job.version_id),
        "dataset_id": str(job.dataset_id),
        "state": job.state,
        "stage": job.stage,
        "attempt": job.attempt,
        "pool": job.pool,
        "progress": job.progress,
        "total_units": job.total_units,
        "completed_units": job.completed_units,
        "coverage": job.coverage,
        "error": job.error,
        "parse_only": job.parse_only,
        "retrieval_ready": job.retrieval_ready,
        # A/B 实验归属（冻结在任务快照；未参与实验时为 None）
        "experiment": (job.execution_plan or {}).get("experiment"),
        "status_url": f"/v1/parsing/jobs/{job.id}",
    }
