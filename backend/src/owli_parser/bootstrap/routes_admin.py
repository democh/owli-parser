"""管理路由：应用/Token/授权、scopes、capabilities、pipeline 与审计。"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from owli_parser.bootstrap.dependencies import (
    ActorState,
    get_container,
    require_platform_admin,
    require_tenant_admin,
    resolve_actor,
    resolve_scope,
)
from owli_parser.contracts.scopes import SCOPE_REGISTRY, SCOPES_REGISTRY_VERSION
from owli_parser.core.clock import ensure_utc
from owli_parser.core.errors import (
    ConfigInvalidError,
    InvalidInputError,
    NotFoundError,
    RevisionConflictError,
    ValidationFailedError,
)
from owli_parser.modules.access.service import GrantEntry
from owli_parser.modules.catalog.models import DocumentRow, DocVersionRow
from owli_parser.modules.catalog.service import CatalogScope
from owli_parser.modules.jobs.models import ParsingJobRow
from owli_parser.modules.parsing import pipeline as pipeline_module
from owli_parser.modules.parsing import probe as probe_module
from owli_parser.modules.parsing.adapters_base import ProbeFeatures, SourceHandle
from owli_parser.modules.parsing.builtin_adapters import default_registry

router = APIRouter(tags=["admin"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplicationCreate(_Strict):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    allowed_scopes: list[str]


class ApplicationPatch(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    allowed_scopes: list[str] | None = None
    status: str | None = None


class GrantPut(_Strict):
    grants: list[dict[str, Any]]


class TokenIssue(_Strict):
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str]
    dataset_ids: list[str]
    expires_in_seconds: int | None = Field(default=None, gt=0)


# ---------------------------------------------------------------------------
# scopes / capabilities
# ---------------------------------------------------------------------------


@router.get("/scopes")
async def list_scopes(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    from owli_parser.modules.identity.domain import TenantRole

    del state, scope  # 仅用于授权校验
    return {
        "registry_version": SCOPES_REGISTRY_VERSION,
        "items": [
            {
                "name": d.name,
                "description": d.description,
                "category": d.category,
                "constraints": d.items,
                "requires_dataset_grant": d.requires_dataset_grant,
            }
            for d in SCOPE_REGISTRY.values()
        ],
        "roles": sorted(TenantRole.ALL),
    }


@router.get("/capabilities")
async def capabilities(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    registry = default_registry()
    adapters = [
        {
            "name": name,
            "version": getattr(adapter, "version", "0"),
            "mode": getattr(adapter, "mode", "local"),
            "capabilities": adapter.capabilities(),
        }
        for name, adapter in registry.items()
    ]
    formats = sorted(
        pipeline_module.PipelineDocument.model_validate(
            __import__("yaml").safe_load(pipeline_module.load_default_pipeline_yaml())
        ).pipelines
    )
    payload: dict[str, Any] = {
        "tenant_id": str(scope.tenant_id),
        "adapters": adapters,
        "formats": formats,
        "retrieval_enabled": container.settings.retrieval.enabled,
        "network_mode": container.settings.network.mode,
        "limits": {
            "upload_direct_max_bytes": container.settings.upload.direct_max_bytes,
            "upload_total_max_bytes": container.settings.upload.total_max_bytes,
            "part_size_bytes": container.settings.upload.part_size_bytes,
        },
    }
    if state.is_application:
        assert state.application is not None
        payload["scopes"] = sorted(state.application.scopes)
        payload["dataset_ids"] = sorted(str(d) for d in state.application.dataset_ids)
    return payload


# ---------------------------------------------------------------------------
# 应用与 Token
# ---------------------------------------------------------------------------


@router.get("/applications")
async def list_applications(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    apps = await container.access.list_applications(scope.tenant_id)
    return [
        {
            "id": str(a.id),
            "name": a.name,
            "description": a.description,
            "status": a.status,
            "allowed_scopes": a.allowed_scopes,
            "auth_revision": a.auth_revision,
        }
        for a in apps
    ]


@router.post("/applications", status_code=201)
async def create_application(
    payload: ApplicationCreate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    app = await container.access.create_application(
        scope.tenant_id,
        name=payload.name,
        description=payload.description,
        allowed_scopes=payload.allowed_scopes,
        created_by_id=scope.actor_id or uuid.uuid4(),
    )
    return {"id": str(app.id), "auth_revision": app.auth_revision}


@router.get("/applications/{application_id}")
async def get_application(
    application_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    app = await container.access.get_application(scope.tenant_id, application_id)
    return {
        "id": str(app.id),
        "name": app.name,
        "description": app.description,
        "status": app.status,
        "allowed_scopes": app.allowed_scopes,
        "auth_revision": app.auth_revision,
    }


@router.patch("/applications/{application_id}")
async def patch_application(
    application_id: uuid.UUID,
    payload: ApplicationPatch,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    app = await container.access.update_application(
        scope.tenant_id,
        application_id,
        name=payload.name,
        description=payload.description,
        allowed_scopes=payload.allowed_scopes,
        status=payload.status,
    )
    return {"id": str(app.id), "auth_revision": app.auth_revision, "status": app.status}


@router.get("/applications/{application_id}/grants")
async def get_grants(
    application_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    grants = await container.access.list_grants(scope.tenant_id, application_id)
    return {"items": [{"dataset_id": str(g.dataset_id), "actions": g.actions} for g in grants]}


@router.put("/applications/{application_id}/grants")
async def put_grants(
    application_id: uuid.UUID,
    payload: GrantPut,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    entries = [
        GrantEntry(
            dataset_id=uuid.UUID(str(item["dataset_id"])),
            actions=[str(a) for a in item.get("actions", [])],
        )
        for item in payload.grants
    ]
    await container.access.replace_grants(scope.tenant_id, application_id, entries)
    app = await container.access.get_application(scope.tenant_id, application_id)
    return {"auth_revision": app.auth_revision}


@router.get("/applications/{application_id}/tokens")
async def list_tokens(
    application_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    tokens = await container.access.list_tokens(scope.tenant_id, application_id)
    return [
        {
            "token_id": t["token_id"],
            "public_id": t["public_id"],
            "name": t["name"],
            "scopes": t["scopes"],
            "dataset_ids": t["dataset_ids"],
            "expires_at": t["expires_at"],
            "revoked_at": t["revoked_at"],
            "last_used_at": t["last_used_at"],
            "status": t["status"],
        }
        for t in tokens
    ]


@router.post("/applications/{application_id}/tokens", status_code=201)
async def issue_token(
    application_id: uuid.UUID,
    payload: TokenIssue,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> Response:
    """签发 Token；原始明文只在本次响应出现（no-store）。"""
    import json

    container = get_container(state.request)
    issued = await container.access.issue_token(
        scope.tenant_id,
        application_id,
        name=payload.name,
        scopes=payload.scopes,
        dataset_ids=payload.dataset_ids,
        expires_in_seconds=payload.expires_in_seconds,
        created_by_id=scope.actor_id or uuid.uuid4(),
    )
    body = json.dumps(
        {
            "token_id": str(issued.token_id),
            "public_id": issued.public_id,
            "token": issued.raw_token,
            "expires_at_epoch": issued.expires_at_epoch,
            "scopes": issued.scopes,
            "dataset_ids": issued.dataset_ids,
            "secret_available": True,
        },
        ensure_ascii=False,
    )
    return Response(
        content=body,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/applications/{application_id}/tokens/{token_id}/revoke")
async def revoke_token(
    application_id: uuid.UUID,
    token_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, str]:
    container = get_container(state.request)
    await container.access.revoke_token(scope.tenant_id, application_id, token_id, actor_id=scope.actor_id)
    return {"status": "revoked"}


@router.get("/applications/{application_id}/usage")
async def application_usage(
    application_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """调用记录（脱敏聚合；不含请求正文与凭据）。"""
    from sqlalchemy import select

    from owli_parser.modules.access.models import ApplicationUsageRow

    container = get_container(state.request)
    await container.access.get_application(scope.tenant_id, application_id)  # 存在性校验
    async with container.uow.read_only() as session:
        rows = (
            (
                await session.execute(
                    select(ApplicationUsageRow)
                    .where(
                        ApplicationUsageRow.tenant_id == scope.tenant_id,
                        ApplicationUsageRow.application_id == application_id,
                    )
                    .order_by(ApplicationUsageRow.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "operation": r.operation,
                    "method": r.method,
                    "path": r.path,
                    "status_code": r.status_code,
                    "decision": r.decision,
                    "request_id": r.request_id,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
        }


# ---------------------------------------------------------------------------
# Pipeline 与 adapter 注册表（只读 + 校验/试验）
# ---------------------------------------------------------------------------


async def _adapter_snapshot(container: Any) -> list[dict[str, Any]]:
    """合并进程内注册表与外部注册行；同名时以内置为准（内置不可被遮蔽）。"""
    by_name: dict[str, dict[str, Any]] = {}
    for name, adapter in default_registry().items():
        by_name[name] = {
            "name": name,
            "version": str(getattr(adapter, "version", "0")),
            "mode": getattr(adapter, "mode", "local"),
            "health": "ready",
            "reported_health": "ready",
            "heartbeat_at": None,
            "stale": False,
            "source": "builtin",
            "config_ref": None,
            "capabilities": adapter.capabilities(),
        }
    for info in await container.configuration.list_adapters():
        by_name.setdefault(
            info.name,
            {
                "name": info.name,
                "version": info.version,
                "mode": info.mode,
                "health": info.health,
                "reported_health": info.reported_health,
                "heartbeat_at": info.heartbeat_at,
                "stale": info.stale,
                "source": "registered",
                "config_ref": info.config_ref,
                "capabilities": info.capabilities,
            },
        )
    return [by_name[name] for name in sorted(by_name)]


@router.get("/adapters")
async def list_adapters(state: ActorState = Depends(resolve_actor)) -> list[dict[str, Any]]:
    """adapter 清单：内置实例恒 ready，外部实例按心跳时效判定生效健康度。"""
    container = get_container(state.request)
    return await _adapter_snapshot(container)


@router.get("/adapters/health")
async def adapter_health(state: ActorState = Depends(resolve_actor)) -> dict[str, Any]:
    """健康聚合：unavailable 优先，其次 degraded/unknown → degraded，否则 ok。"""
    container = get_container(state.request)
    entries = await _adapter_snapshot(container)
    counts = dict.fromkeys(("ready", "degraded", "maintenance", "unavailable", "unknown"), 0)
    stale: list[str] = []
    for entry in entries:
        counts[entry["health"]] = counts.get(entry["health"], 0) + 1
        if entry["stale"]:
            stale.append(entry["name"])
    if counts["unavailable"]:
        status = "unavailable"
    elif counts["degraded"] or counts["maintenance"] or counts["unknown"]:
        status = "degraded"
    else:
        status = "ok"
    return {"status": status, "total": len(entries), "counts": counts, "stale": stale}


class AdapterUpsert(_Strict):
    version: str = Field(min_length=1, max_length=32)
    mode: str = Field(default="remote", pattern="^(local|remote|composite)$")
    capabilities: dict[str, Any] = Field(default_factory=dict)
    config_ref: str | None = Field(default=None, max_length=128)
    health: str = Field(default="ready", pattern="^(ready|degraded|maintenance|unavailable)$")


class AdapterHeartbeatPayload(_Strict):
    health: str = Field(default="ready", pattern="^(ready|degraded|maintenance|unavailable)$")
    version: str | None = Field(default=None, min_length=1, max_length=32)


def _registered_adapter_payload(info: Any) -> dict[str, Any]:
    return {
        "name": info.name,
        "version": info.version,
        "mode": info.mode,
        "health": info.health,
        "reported_health": info.reported_health,
        "heartbeat_at": info.heartbeat_at,
        "stale": info.stale,
        "source": "registered",
        "config_ref": info.config_ref,
        "capabilities": info.capabilities,
    }


@router.put("/adapters/{name}")
async def register_adapter(
    name: str,
    payload: AdapterUpsert,
    state: ActorState = Depends(require_platform_admin),
) -> dict[str, Any]:
    """注册/更新外部 adapter（平台管理员）；内置同名实例拒绝，避免被外部实现遮蔽。"""
    container = get_container(state.request)
    info = await container.configuration.register_adapter(
        name=name,
        version=payload.version,
        mode=payload.mode,
        capabilities=payload.capabilities,
        config_ref=payload.config_ref,
        health=payload.health,
    )
    return _registered_adapter_payload(info)


@router.post("/adapters/{name}/heartbeat")
async def adapter_heartbeat(
    name: str,
    payload: AdapterHeartbeatPayload,
    state: ActorState = Depends(require_platform_admin),
) -> dict[str, Any]:
    """外部 adapter 心跳/readiness 上报（平台管理员）；未注册实例返回 404。"""
    container = get_container(state.request)
    info = await container.configuration.heartbeat_adapter(
        name=name, health=payload.health, version=payload.version
    )
    return _registered_adapter_payload(info)


@router.get("/pipelines")
async def list_pipelines(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    compiled = container.submit_parsing.compile_default_pipeline()
    by_name: dict[str, dict[str, Any]] = {
        compiled.document.name: {
            "name": compiled.document.name,
            "version": compiled.document.revision,
            "active": True,
            "digest": compiled.digest,
            "source": "builtin",
        }
    }
    # 租户配置同名覆盖内置（提交解析将消费租户配置）
    for config in await container.configuration.list_active_pipelines(scope.tenant_id):
        by_name[config.name] = {
            "name": config.name,
            "version": config.version,
            "active": config.active,
            "digest": config.digest,
            "source": "tenant",
        }
    return sorted(by_name.values(), key=lambda item: item["name"])


@router.get("/pipelines/{name}")
async def get_pipeline(
    name: str,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    config = await container.configuration.get_active_pipeline(scope.tenant_id, name)
    if config is not None:
        compiled = pipeline_module.compile_pipeline(config.yaml_text, set(default_registry().keys()))
        return {
            "name": config.name,
            "version": config.version,
            "digest": config.digest,
            "source": "tenant",
            "yaml": config.yaml_text,
            "execution_plan": compiled.execution_plan(),
        }
    compiled = container.submit_parsing.compile_default_pipeline()
    if name != compiled.document.name:
        raise NotFoundError(f"pipeline {name} 不存在")
    return {
        "name": compiled.document.name,
        "version": compiled.document.revision,
        "digest": compiled.digest,
        "source": "builtin",
        "yaml": compiled.yaml_text,
        "execution_plan": compiled.execution_plan(),
    }


class PipelinePut(_Strict):
    yaml_text: str = Field(min_length=1, max_length=100_000)


def _parse_if_match(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    try:
        return int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise InvalidInputError("If-Match 格式不合法") from exc


@router.put("/pipelines/{name}")
async def put_pipeline(
    name: str,
    payload: PipelinePut,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    """版本化保存解析管道（不覆盖历史）；首次创建免 If-Match，更新须 If-Match=当前版本。"""
    container = get_container(state.request)
    actor_id = scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None)
    if actor_id is None:
        raise RevisionConflictError("缺少执行主体")
    info = await container.configuration.save_pipeline(
        scope.tenant_id,
        name=name,
        yaml_text=payload.yaml_text,
        expected_version=_parse_if_match(if_match),
        actor_kind=str(scope.kind),
        actor_id=actor_id,
    )
    return {
        "name": info.name,
        "version": info.version,
        "digest": info.digest,
        "active": info.active,
    }


class PipelineValidate(_Strict):
    yaml_text: str = Field(min_length=1, max_length=100_000)


class PipelineDryRun(_Strict):
    """草稿优先；否则租户 active 配置；最后内置 default。

    来源二选一：version_id（已入库文档版本）或 artifact_id + filename（新上传样本）。
    """

    yaml_text: str | None = Field(default=None, max_length=100_000)
    version_id: uuid.UUID | None = None
    artifact_id: uuid.UUID | None = None
    filename: str | None = Field(default=None, min_length=1, max_length=512)


def _known_adapters() -> set[str]:
    return set(default_registry().keys())


async def _compile_for_dry_run(
    container: Any, scope: CatalogScope, name: str, yaml_text: str | None
) -> tuple[Any, str, int]:
    known = _known_adapters()
    if yaml_text is not None:
        compiled = pipeline_module.compile_pipeline(yaml_text, known)
        if compiled.document.name != name:
            raise ValidationFailedError("YAML name 与路径 name 不一致")
        return compiled, "draft", compiled.document.revision
    config = await container.configuration.get_active_pipeline(scope.tenant_id, name)
    if config is not None:
        compiled = pipeline_module.compile_pipeline(config.yaml_text, known)
        return compiled, "tenant", config.version
    compiled = container.submit_parsing.compile_default_pipeline()
    if name != compiled.document.name:
        raise NotFoundError(f"pipeline {name} 不存在")
    return compiled, "builtin", compiled.document.revision


async def _resolve_dry_run_source(
    container: Any, scope: CatalogScope, payload: PipelineDryRun
) -> tuple[uuid.UUID, str]:
    if (payload.version_id is None) == (payload.artifact_id is None):
        raise ValidationFailedError("version_id 与 artifact_id 必须且只能提供一个")
    if payload.artifact_id is not None:
        if not payload.filename:
            raise ValidationFailedError("artifact_id 需同时提供 filename（用于格式判定）")
        return payload.artifact_id, payload.filename
    async with container.uow.read_only() as session:
        row = (
            await session.execute(
                select(DocVersionRow.source_artifact_id, DocumentRow.filename)
                .join(DocumentRow, DocumentRow.id == DocVersionRow.document_id)
                .where(
                    DocVersionRow.id == payload.version_id,
                    DocVersionRow.tenant_id == scope.tenant_id,
                    DocumentRow.tenant_id == scope.tenant_id,
                )
            )
        ).first()
    if row is None:
        raise NotFoundError("文档版本不存在")
    return row[0], row[1]


async def _probe_artifact(
    container: Any, tenant_id: uuid.UUID, artifact_id: uuid.UUID, filename: str
) -> ProbeFeatures:
    """把样本复制到隔离临时目录后预检（不执行任何 adapter）。"""
    location, profile = await container.artifacts.resolve_location(tenant_id, artifact_id)
    suffix = Path(filename).suffix.lower()
    with tempfile.TemporaryDirectory(prefix="owli-dry-run-") as tmp_dir:
        dest = Path(tmp_dir) / (f"sample{suffix}" if suffix else "sample.bin")

        def _copy() -> None:
            reader = container.artifacts.open_reader(location, profile)
            try:
                with open(dest, "wb") as out:
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
            finally:
                reader.close()

        await asyncio.to_thread(_copy)
        handle = SourceHandle(
            path=dest, mime=None, filename=filename, content_hash="", artifact_id=str(artifact_id)
        )
        return await asyncio.to_thread(probe_module.probe, handle)


def _features_payload(features: ProbeFeatures) -> dict[str, Any]:
    return {
        "mime": features.mime,
        "extension": features.extension,
        "size_bytes": features.size_bytes,
        "text_layer_ratio": features.text_layer_ratio,
        "page_count": features.page_count,
        "encrypted": features.encrypted,
        "has_zip_container": features.has_zip_container,
        "language_hint": features.language_hint,
        "extra": features.extra,
    }


def _dry_run_warnings(compiled: Any, fmt: str, features: ProbeFeatures, decision: Any) -> list[str]:
    warnings: list[str] = []
    if features.encrypted:
        warnings.append("输入已加密：解析阶段可能失败")
    if fmt == "pdf" and features.text_layer_ratio is not None and features.text_layer_ratio < 0.5:
        warnings.append("文本层覆盖率低于 0.5：默认分支要求 text_layer_ratio>=0.5（OCR 未启用）")
    if compiled.pipeline_for(fmt) is None:
        warnings.append(f"pipeline 未定义格式 {fmt} 的分支")
    if decision.adapter_name is None and decision.skipped:
        warnings.append("所有候选均不可用，查看 route.skipped 的逐项原因")
    return warnings


@router.post("/pipelines/{name}/validate")
async def validate_pipeline(
    name: str,
    payload: PipelineValidate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """草稿校验（不保存、不执行）：编译通过且 name 一致才算 ok。"""
    del state, scope  # 仅借助依赖完成租户 admin 鉴权
    try:
        compiled = pipeline_module.compile_pipeline(payload.yaml_text, _known_adapters())
    except ConfigInvalidError as exc:
        return {"ok": False, "errors": [str(exc)]}
    errors: list[str] = []
    if compiled.document.name != name:
        errors.append(f"YAML name={compiled.document.name!r} 与路径 name={name!r} 不一致")
    if errors:
        return {"ok": False, "errors": errors}
    return {
        "ok": True,
        "errors": [],
        "name": compiled.document.name,
        "version": compiled.document.revision,
        "digest": compiled.digest,
        "execution_plan": compiled.execution_plan(),
    }


@router.post("/pipelines/{name}/dry-run")
async def dry_run_pipeline(
    name: str,
    payload: PipelineDryRun,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """路由解释：预检 + 编译 + first_success 降级链；不执行 adapter、不计模型费用。"""
    container = get_container(state.request)
    artifact_id, filename = await _resolve_dry_run_source(container, scope, payload)
    compiled, source, version = await _compile_for_dry_run(container, scope, name, payload.yaml_text)
    features = await _probe_artifact(container, scope.tenant_id, artifact_id, filename)
    fmt = pipeline_module.detect_format(features)
    registry = default_registry()
    supports = {
        adapter_name: adapter.supports(features).supported for adapter_name, adapter in registry.items()
    }
    decision = pipeline_module.explain_route(compiled, fmt, features, supports)
    return {
        "ok": decision.adapter_name is not None,
        "pipeline": {
            "name": compiled.document.name,
            "version": version,
            "digest": compiled.digest,
            "source": source,
        },
        "format": fmt,
        "features": _features_payload(features),
        "route": {
            "mode": decision.mode,
            "adapter": decision.adapter_name,
            "reason": decision.reason,
            "skipped": [{"adapter": adapter, "reason": reason} for adapter, reason in decision.skipped],
        },
        "warnings": _dry_run_warnings(compiled, fmt, features, decision),
    }


# ---------------------------------------------------------------------------
# 管线 A/B 实验（按 artifact 稳定分桶；变体 YAML 冻结进任务快照）
# ---------------------------------------------------------------------------


class PipelineExperimentPut(_Strict):
    name: str = Field(min_length=1, max_length=128)
    variant_a_yaml: str = Field(min_length=1, max_length=100_000)
    variant_b_yaml: str = Field(min_length=1, max_length=100_000)
    traffic_percent: int = Field(ge=0, le=100)
    active: bool = True


def _experiment_payload(info: Any) -> dict[str, Any]:
    return {
        "id": str(info.id),
        "pipeline_name": info.pipeline_name,
        "name": info.name,
        "traffic_percent": info.traffic_percent,
        "active": info.active,
        "revision": info.revision,
        "variant_a_digest": info.variant_a_digest,
        "variant_b_digest": info.variant_b_digest,
        "variant_a_yaml": info.variant_a_yaml,
        "variant_b_yaml": info.variant_b_yaml,
        "created_at": info.created_at,
        "updated_at": info.updated_at,
    }


@router.put("/pipelines/{name}/experiment")
async def put_pipeline_experiment(
    name: str,
    payload: PipelineExperimentPut,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """创建/更新管线实验（同名 pipeline 唯一，revision 递增）。"""
    container = get_container(state.request)
    actor_id = scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None)
    if actor_id is None:
        raise ValidationFailedError("缺少执行主体")
    info = await container.configuration.upsert_pipeline_experiment(
        scope.tenant_id,
        pipeline_name=name,
        name=payload.name,
        variant_a_yaml=payload.variant_a_yaml,
        variant_b_yaml=payload.variant_b_yaml,
        traffic_percent=payload.traffic_percent,
        active=payload.active,
        actor_kind=str(scope.kind),
        actor_id=actor_id,
    )
    return _experiment_payload(info)


@router.get("/pipelines/{name}/experiment")
async def get_pipeline_experiment(
    name: str,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    info = await container.configuration.get_pipeline_experiment(scope.tenant_id, name)
    if info is None:
        raise NotFoundError("管线实验不存在")
    return _experiment_payload(info)


_TERMINAL_JOB_STATES = {"DONE", "PARTIAL_DONE", "FAILED", "DEAD_LETTER", "CANCELLED"}
_EXPERIMENT_REPORT_JOB_LIMIT = 2000
# 质量聚合：只取回归对比中可归因到变体产物的 case；计数类指标（pairs）不求均值
_EXPERIMENT_QUALITY_RUN_LIMIT = 200
_QUALITY_METRIC_KEYS = ("text_cer", "reading_order", "table_structure_f1", "location_iou")


def _empty_variant_stats() -> dict[str, Any]:
    return {
        "jobs": 0,
        "done": 0,
        "partial_done": 0,
        "failed": 0,
        "cancelled": 0,
        "in_progress": 0,
        "avg_coverage": None,
        "avg_elapsed_seconds": None,
        "quality_cases": 0,
        "quality_metrics": {},
    }


@router.get("/pipelines/{name}/experiment/report")
async def pipeline_experiment_report(
    name: str,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    """A/B 对照：按任务快照归属聚合成功/失败/覆盖率/耗时与质量指标。

    - 任务侧：仅扫描最近 _EXPERIMENT_REPORT_JOB_LIMIT 条租户任务；
    - 质量侧：从最近回归运行中取 cases，按 hypothesis_version_id 归因到变体产物，
      只聚合比例类指标（text_cer / reading_order / table_structure_f1 / location_iou）。
    """
    container = get_container(state.request)
    info = await container.configuration.get_pipeline_experiment(scope.tenant_id, name)
    if info is None:
        raise NotFoundError("管线实验不存在")
    async with container.uow.read_only() as session:
        rows = (
            (
                await session.execute(
                    select(ParsingJobRow)
                    .where(ParsingJobRow.tenant_id == scope.tenant_id)
                    .order_by(ParsingJobRow.created_at.desc())
                    .limit(_EXPERIMENT_REPORT_JOB_LIMIT)
                )
            )
            .scalars()
            .all()
        )
    stats = {"A": _empty_variant_stats(), "B": _empty_variant_stats()}
    coverage_values: dict[str, list[float]] = {"A": [], "B": []}
    elapsed_values: dict[str, list[float]] = {"A": [], "B": []}
    # 本实验任务产物版本 → 变体；供黄金集逐 case 指标归因
    variant_by_version: dict[str, str] = {}
    total = 0
    for row in rows:
        assignment = (row.execution_plan or {}).get("experiment")
        if not isinstance(assignment, dict):
            continue
        if assignment.get("experiment_id") != str(info.id):
            continue
        variant = assignment.get("variant")
        if variant not in stats:
            continue
        total += 1
        variant_by_version[str(row.version_id)] = variant
        bucket = stats[variant]
        bucket["jobs"] += 1
        if row.state == "DONE":
            bucket["done"] += 1
        elif row.state == "PARTIAL_DONE":
            bucket["partial_done"] += 1
        elif row.state in ("FAILED", "DEAD_LETTER"):
            bucket["failed"] += 1
        elif row.state == "CANCELLED":
            bucket["cancelled"] += 1
        else:
            bucket["in_progress"] += 1
        if row.coverage is not None:
            coverage_values[variant].append(float(row.coverage))
        if row.state in _TERMINAL_JOB_STATES:
            elapsed = (ensure_utc(row.updated_at) - ensure_utc(row.created_at)).total_seconds()
            elapsed_values[variant].append(max(elapsed, 0.0))
    for variant in ("A", "B"):
        coverage_samples = coverage_values[variant]
        elapsed_samples = elapsed_values[variant]
        stats[variant]["avg_coverage"] = (
            round(sum(coverage_samples) / len(coverage_samples), 4) if coverage_samples else None
        )
        stats[variant]["avg_elapsed_seconds"] = (
            round(sum(elapsed_samples) / len(elapsed_samples), 1) if elapsed_samples else None
        )

    # 质量指标：从回归运行 cases 中按 hypothesis_version_id 归因到变体。
    # 仅统计本租户（或全局 CLI 集）的黄金集；counts 类指标不入均值。
    quality_values: dict[str, dict[str, list[float]]] = {"A": {}, "B": {}}
    quality_cases = {"A": 0, "B": 0}
    golden_runs = await container.knowledge.list_golden_runs(limit=_EXPERIMENT_QUALITY_RUN_LIMIT)
    for golden_run in golden_runs:
        if golden_run.tenant_id is not None and golden_run.tenant_id != scope.tenant_id:
            continue
        cases = (golden_run.metrics or {}).get("cases")
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, dict):
                continue
            variant = variant_by_version.get(str(case.get("hypothesis_version_id")))
            if variant is None:
                continue
            metrics = case.get("metrics")
            if not isinstance(metrics, dict):
                continue
            matched = False
            for key in _QUALITY_METRIC_KEYS:
                value = metrics.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    quality_values[variant].setdefault(key, []).append(float(value))
                    matched = True
            if matched:
                quality_cases[variant] += 1
    for variant in ("A", "B"):
        stats[variant]["quality_cases"] = quality_cases[variant]
        stats[variant]["quality_metrics"] = {
            key: round(sum(values) / len(values), 4)
            for key, values in sorted(quality_values[variant].items())
            if values
        }
    return {
        "experiment": {
            "id": str(info.id),
            "pipeline_name": info.pipeline_name,
            "name": info.name,
            "revision": info.revision,
            "traffic_percent": info.traffic_percent,
            "active": info.active,
        },
        "total_jobs": total,
        "scanned_jobs": len(rows),
        "variants": stats,
    }


@router.get("/experiments")
async def list_pipeline_experiments(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(resolve_scope),
) -> dict[str, Any]:
    container = get_container(state.request)
    items = await container.configuration.list_pipeline_experiments(scope.tenant_id)
    return {"items": [_experiment_payload(item) for item in items]}


# ---------------------------------------------------------------------------
# 存储 profile 与对象导入位置（租户 admin；密钥只写引用）
# ---------------------------------------------------------------------------


class StorageProfileCreate(_Strict):
    name: str = Field(min_length=1, max_length=128)
    provider: str
    config: dict[str, Any]


class StorageProfilePatch(_Strict):
    config: dict[str, Any] | None = None


class StorageProfileFlags(_Strict):
    read_disabled: bool | None = None
    write_disabled: bool | None = None


class StorageTestRequest(_Strict):
    include_write: bool = False


class SourceLocationCreate(_Strict):
    dataset_id: uuid.UUID
    profile_revision_id: uuid.UUID
    bucket: str = Field(min_length=1, max_length=128)
    inbox_prefix: str = Field(min_length=1, max_length=256)


class SourceLocationPatch(_Strict):
    enabled: bool


def _storage_payload(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "provider": row.provider,
        "revision": row.revision,
        "state": row.state,
        "config": row.config,
        "read_disabled": row.read_disabled,
        "write_disabled": row.write_disabled,
        "created_at": row.created_at,
    }


@router.get("/storage-profiles")
async def list_storage_profiles(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    rows = await container.configuration.list_storage_profiles(scope.tenant_id)
    return [_storage_payload(r) for r in rows]


@router.post("/storage-profiles", status_code=201)
async def create_storage_profile(
    payload: StorageProfileCreate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    row = await container.configuration.create_storage_profile(
        scope.tenant_id,
        name=payload.name,
        provider=payload.provider,
        config=payload.config,
        actor_id=scope.actor_id or uuid.uuid4(),
    )
    return _storage_payload(row)


@router.get("/storage-profiles/{profile_id}")
async def get_storage_profile(
    profile_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    row = await container.configuration.get_storage_profile(scope.tenant_id, profile_id)
    return _storage_payload(row)


@router.patch("/storage-profiles/{profile_id}")
async def patch_storage_profile(
    profile_id: uuid.UUID,
    payload: StorageProfilePatch,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """修改创建新草稿 revision；发布仅影响新写入，旧对象按原位置读取。"""
    container = get_container(state.request)
    row = await container.configuration.update_storage_profile(
        scope.tenant_id,
        profile_id,
        config=payload.config,
        actor_id=scope.actor_id or uuid.uuid4(),
    )
    return _storage_payload(row)


@router.post("/storage-profiles/{profile_id}/flags")
async def set_storage_profile_flags(
    profile_id: uuid.UUID,
    payload: StorageProfileFlags,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    row = await container.configuration.set_storage_profile_flags(
        scope.tenant_id,
        profile_id,
        read_disabled=payload.read_disabled,
        write_disabled=payload.write_disabled,
    )
    return _storage_payload(row)


@router.post("/storage-profiles/{profile_id}/validate")
async def validate_storage_profile(
    profile_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    report = await container.configuration.validate_storage_profile(scope.tenant_id, profile_id)
    return {"ok": report.ok, "errors": report.errors, "warnings": report.warnings}


@router.post("/storage-profiles/{profile_id}/test")
async def test_storage_profile(
    profile_id: uuid.UUID,
    payload: StorageTestRequest,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """显式 IO 测试；include_write 会在隔离前缀创建并清理临时对象。测试不会激活配置。"""
    container = get_container(state.request)
    report = await container.configuration.test_storage_profile(
        scope.tenant_id, profile_id, include_write=payload.include_write
    )
    return {"ok": report.ok, "steps": report.steps, "notes": report.notes}


@router.post("/storage-profiles/{profile_id}/publish")
async def publish_storage_profile(
    profile_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    row = await container.configuration.publish_storage_profile(scope.tenant_id, profile_id)
    return _storage_payload(row)


@router.get("/source-locations")
async def list_source_locations(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
    dataset_id: uuid.UUID | None = Query(default=None),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    rows = await container.configuration.list_source_locations(scope.tenant_id, dataset_id=dataset_id)
    return [
        {
            "id": str(r.id),
            "dataset_id": str(r.dataset_id),
            "profile_revision_id": str(r.profile_revision_id),
            "bucket": r.bucket,
            "inbox_prefix": r.inbox_prefix,
            "enabled": r.enabled,
        }
        for r in rows
    ]


@router.post("/source-locations", status_code=201)
async def create_source_location(
    payload: SourceLocationCreate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    row = await container.configuration.create_source_location(
        scope.tenant_id,
        dataset_id=payload.dataset_id,
        profile_revision_id=payload.profile_revision_id,
        bucket=payload.bucket,
        inbox_prefix=payload.inbox_prefix,
    )
    return {
        "id": str(row.id),
        "dataset_id": str(row.dataset_id),
        "bucket": row.bucket,
        "inbox_prefix": row.inbox_prefix,
        "enabled": row.enabled,
    }


@router.patch("/source-locations/{location_id}")
async def patch_source_location(
    location_id: uuid.UUID,
    payload: SourceLocationPatch,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    row = await container.configuration.set_source_location_enabled(
        scope.tenant_id, location_id, enabled=payload.enabled
    )
    return {"id": str(row.id), "enabled": row.enabled}


# ---------------------------------------------------------------------------
# 模型连接（连接与凭据 / 显式测试 / 发布严格分离）
# ---------------------------------------------------------------------------


class ModelConnectionCreate(_Strict):
    name: str = Field(min_length=1, max_length=128)
    protocol: str = "openai_compatible"
    base_url: str = Field(min_length=1, max_length=512)
    vendor_label: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=64)
    credential_ref: str | None = Field(default=None, max_length=256)


class ModelProfileCreate(_Strict):
    connection_id: uuid.UUID
    model_name: str = Field(min_length=1, max_length=255)
    capabilities: list[str] = Field(min_length=1)
    context_window: int | None = Field(default=None, ge=1)
    tokenizer_ref: str | None = Field(default=None, max_length=128)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    supported_parameters: list[str] = Field(default_factory=list)
    pricing_ref: str | None = Field(default=None, max_length=128)
    embedding_dimensions: int | None = Field(default=None, ge=1)
    embedding_normalization: str | None = Field(default=None, max_length=32)
    embedding_distance: str | None = Field(default=None, max_length=32)
    output_kinds: list[str] = Field(default_factory=list)


class ModelProfileStatePatch(_Strict):
    state: str


class ModelBindingUpsert(_Strict):
    primary_profile_id: uuid.UUID
    fallback_profile_ids: list[str] = Field(default_factory=list)
    # 自由 JSON（目前支持 max_tokens_per_run：单次问答 token 上限）
    budget: dict[str, Any] | None = None


class ModelConnectionPatch(_Strict):
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    vendor_label: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=64)
    credential_ref: str | None = Field(default=None, max_length=256)


def _connection_payload(row: Any) -> dict[str, Any]:
    """入参为 ConfigurationService 的 ModelConnectionInfo（DTO），只回显引用存在性。"""
    return {
        "id": str(row.id),
        "name": row.name,
        "protocol": row.protocol,
        "vendor_label": row.vendor_label,
        "base_url": row.base_url,
        "region": row.region,
        "revision": row.revision,
        "state": row.state,
        "credential_configured": row.credential_configured,
        "last_test_report": row.last_test_report,
        "created_at": row.created_at,
    }


@router.get("/model-connections")
async def list_model_connections(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    rows = await container.configuration.list_model_connections(scope.tenant_id)
    return [_connection_payload(r) for r in rows]


@router.post("/model-connections", status_code=201)
async def create_model_connection(
    payload: ModelConnectionCreate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    row = await container.configuration.create_model_connection(
        scope.tenant_id,
        name=payload.name,
        protocol=payload.protocol,
        base_url=payload.base_url,
        vendor_label=payload.vendor_label,
        region=payload.region,
        credential_ref=payload.credential_ref,
        actor_id=scope.actor_id or uuid.uuid4(),
    )
    return _connection_payload(row)


@router.patch("/model-connections/{connection_id}")
async def patch_model_connection(
    connection_id: uuid.UUID,
    payload: ModelConnectionPatch,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    row = await container.configuration.update_model_connection(
        scope.tenant_id,
        connection_id,
        base_url=payload.base_url,
        vendor_label=payload.vendor_label,
        region=payload.region,
        credential_ref=payload.credential_ref,
        actor_id=scope.actor_id or uuid.uuid4(),
    )
    return _connection_payload(row)


@router.post("/model-connections/{connection_id}/validate")
async def validate_model_connection(
    connection_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    report = await container.configuration.validate_model_connection(scope.tenant_id, connection_id)
    return {"ok": report.ok, "errors": report.errors, "warnings": report.warnings}


@router.post("/model-connections/{connection_id}/test")
async def test_model_connection(
    connection_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """显式连接/认证测试（GET /models）；可能计入配额；报告脱敏且不改变发布状态。"""
    container = get_container(state.request)
    report = await container.configuration.test_model_connection(scope.tenant_id, connection_id)
    await container.configuration.record_connection_test(scope.tenant_id, connection_id, report)
    return {
        "ok": report.ok,
        "steps": report.steps,
        "latency_ms": report.latency_ms,
        "notes": report.notes,
    }


@router.post("/model-connections/{connection_id}/publish")
async def publish_model_connection(
    connection_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    container = get_container(state.request)
    row = await container.configuration.publish_model_connection(scope.tenant_id, connection_id)
    return _connection_payload(row)


# ---------------------------------------------------------------------------
# 模型能力档案与用途绑定（三层：连接 → 档案 → 绑定）；档案不可变
# ---------------------------------------------------------------------------


def _profile_payload(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "connection_id": str(row.connection_id),
        "model_name": row.model_name,
        "revision": row.revision,
        "capabilities": list(row.capabilities or []),
        "context_window": row.context_window,
        "tokenizer_ref": row.tokenizer_ref,
        "max_input_tokens": row.max_input_tokens,
        "max_output_tokens": row.max_output_tokens,
        "supported_parameters": list(row.supported_parameters or []),
        "pricing_ref": row.pricing_ref,
        "embedding_dimensions": row.embedding_dimensions,
        "embedding_normalization": row.embedding_normalization,
        "embedding_distance": row.embedding_distance,
        "output_kinds": list(row.output_kinds or []),
        "state": row.state,
    }


def _binding_payload(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "purpose": row.purpose,
        "primary_profile_id": str(row.primary_profile_id),
        "primary_model_name": row.primary_model_name,
        "fallback_profile_ids": list(row.fallback_profile_ids or []),
        "revision": row.revision,
        "budget": dict(row.budget or {}),
    }


@router.get("/model-profiles")
async def list_model_profiles(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    rows = await container.configuration.list_model_profiles(scope.tenant_id)
    return [_profile_payload(row) for row in rows]


@router.post("/model-profiles", status_code=201)
async def create_model_profile(
    payload: ModelProfileCreate,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """能力档案不可变：能力/模型名变更请新建档案（避免在途索引语义混淆）。"""
    container = get_container(state.request)
    actor_id = scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None)
    if actor_id is None:
        raise RevisionConflictError("缺少执行主体")
    row = await container.configuration.create_model_profile(
        scope.tenant_id,
        connection_id=payload.connection_id,
        model_name=payload.model_name,
        capabilities=payload.capabilities,
        context_window=payload.context_window,
        tokenizer_ref=payload.tokenizer_ref,
        max_input_tokens=payload.max_input_tokens,
        max_output_tokens=payload.max_output_tokens,
        supported_parameters=payload.supported_parameters,
        pricing_ref=payload.pricing_ref,
        embedding_dimensions=payload.embedding_dimensions,
        embedding_normalization=payload.embedding_normalization,
        embedding_distance=payload.embedding_distance,
        output_kinds=payload.output_kinds,
        actor_id=actor_id,
    )
    return _profile_payload(row)


@router.post("/model-profiles/{profile_id}/state")
async def set_model_profile_state(
    profile_id: uuid.UUID,
    payload: ModelProfileStatePatch,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """停用/恢复；仍被绑定引用时停用会被拒绝（409）。"""
    container = get_container(state.request)
    row = await container.configuration.set_model_profile_state(
        scope.tenant_id, profile_id, state=payload.state
    )
    return _profile_payload(row)


@router.get("/model-bindings")
async def list_model_bindings(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    rows = await container.configuration.list_model_bindings(scope.tenant_id)
    return [_binding_payload(row) for row in rows]


@router.put("/model-bindings/{purpose}")
async def upsert_model_binding(
    purpose: str,
    payload: ModelBindingUpsert,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    """用途绑定（租户级）；首次创建免 If-Match，更新须 If-Match=当前 revision。"""
    container = get_container(state.request)
    actor_id = scope.actor_id or (scope.user_ctx.user_id if scope.user_ctx else None)
    if actor_id is None:
        raise RevisionConflictError("缺少执行主体")
    row = await container.configuration.upsert_model_binding(
        scope.tenant_id,
        purpose,
        primary_profile_id=payload.primary_profile_id,
        fallback_profile_ids=payload.fallback_profile_ids,
        budget=payload.budget,
        expected_revision=_parse_if_match(if_match),
        actor_id=actor_id,
    )
    return _binding_payload(row)


# ---------------------------------------------------------------------------
# 配置发布审计与密钥引用登记（不存明文；引用字符串本身非秘密）
# ---------------------------------------------------------------------------


@router.get("/config-revisions")
async def list_config_revisions(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
    kind: str | None = Query(default=None),
    resource_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    container = get_container(state.request)
    rows = await container.configuration.list_config_revisions(
        scope.tenant_id, kind=kind, resource_name=resource_name, limit=limit
    )
    return [
        {
            "id": str(row.id),
            "kind": row.kind,
            "resource_id": str(row.resource_id) if row.resource_id else None,
            "resource_name": row.resource_name,
            "revision": row.revision,
            "state": row.state,
            "validated_payload": row.validated_payload,
            "payload_digest": row.payload_digest,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.get("/secret-refs")
async def list_secret_refs(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> list[dict[str, Any]]:
    """密钥引用清单（provider/reference/版本/用途；不含明文）。"""
    container = get_container(state.request)
    rows = await container.configuration.list_secret_refs(scope.tenant_id)
    return [
        {
            "id": str(row.id),
            "provider": row.provider,
            "reference": row.reference,
            "key_version": row.key_version,
            "purpose": row.purpose,
            "bound_endpoint": row.bound_endpoint,
            "state": row.state,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.post("/secret-refs/{secret_id}/rotate")
async def rotate_secret_ref(
    secret_id: uuid.UUID,
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """轮换标记：key_version 递增；实际值由部署侧更新 env/file 后重启生效。"""
    container = get_container(state.request)
    row = await container.configuration.bump_secret_ref_version(scope.tenant_id, secret_id)
    return {
        "id": str(row.id),
        "provider": row.provider,
        "reference": row.reference,
        "key_version": row.key_version,
        "purpose": row.purpose,
        "bound_endpoint": row.bound_endpoint,
        "state": row.state,
        "created_at": row.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


@router.get("/audit-events")
async def audit_events(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    from sqlalchemy import select

    from owli_parser.infrastructure.crosscut_models import AuditLogRow

    container = get_container(state.request)
    async with container.uow.read_only() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLogRow)
                    .where(AuditLogRow.tenant_id == scope.tenant_id)
                    .order_by(AuditLogRow.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "action": r.action,
                    "actor_kind": r.actor_kind,
                    "actor_id": str(r.actor_id) if r.actor_id else None,
                    "resource_type": r.resource_type,
                    "resource_id": r.resource_id,
                    "decision": r.decision,
                    "reason": r.reason,
                    "trace_id": r.trace_id,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
        }


@router.get("/settings/effective")
async def effective_settings(
    state: ActorState = Depends(resolve_actor),
    scope: CatalogScope = Depends(require_tenant_admin),
) -> dict[str, Any]:
    """配置来源摘要；绝不返回完整 settings 对象或密钥。"""
    container = get_container(state.request)
    s = container.settings
    return {
        "tenant_id": str(scope.tenant_id),
        "sources": ["defaults.yaml", "OWLI_CONFIG_FILE", "OWLI_ env"],
        "summary": {
            "environment": s.app.environment,
            "network_mode": s.network.mode,
            "retrieval_enabled": s.retrieval.enabled,
            "default_pipeline": s.parsing.default_pipeline,
            "chunk": {
                "target_tokens": s.chunk.target_tokens,
                "max_tokens": s.chunk.max_tokens,
                "overlap_tokens": s.chunk.overlap_tokens,
                "tokenizer_ref": s.chunk.tokenizer_ref,
            },
            "jobs": {
                "max_attempts": s.jobs.max_attempts,
                "lease_seconds": s.jobs.lease_seconds,
                "heartbeat_seconds": s.jobs.heartbeat_seconds,
            },
            "storage_default_profile": s.storage.default_profile_ref,
            # 获准的本地存储 root 池（租户不能提交任意本机路径；设置页只展示批准项）
            "local_roots": [
                {"id": r.id, "path": r.path, "quota_bytes": r.quota_bytes} for r in s.storage.local.roots
            ],
        },
    }


__all__ = ["router"]
