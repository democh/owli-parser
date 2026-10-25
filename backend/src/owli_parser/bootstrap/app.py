"""FastAPI 应用工厂与中间件。

- lifespan 管理容器与 DB 引擎；不在模块 import 时连接外部资源。
- 统一错误体 {code, message, detail, trace_id}；trace_id 贯穿日志。
- CORS 默认关闭（生产同源反向代理）；开启时使用显式来源白名单。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from owli_parser.bootstrap.container import Container
from owli_parser.bootstrap.routes_admin import router as admin_router
from owli_parser.bootstrap.routes_catalog import router as catalog_router
from owli_parser.bootstrap.routes_identity import router as identity_router
from owli_parser.bootstrap.routes_ingestion import router as ingestion_router
from owli_parser.bootstrap.routes_realtime import router as realtime_router
from owli_parser.core.errors import AppError, ErrorCode
from owli_parser.infrastructure.telemetry import METRICS, configure_logging, get_logger, metrics_text


def create_app(container: Container | None = None) -> FastAPI:
    configure_logging()
    logger = get_logger("owli.api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal container
        if container is None:
            container = Container()
        app.state.container = container
        logger.info("api_started", extra={"environment": container.settings.app.environment})
        try:
            yield
        finally:
            await container.dispose()
            logger.info("api_stopped")

    app = FastAPI(
        title="owli-parser API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/v1/docs",
        openapi_url="/v1/openapi.json",
    )
    app.state.container = container

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next: Any) -> Any:
        trace_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        started = time.monotonic()
        response = await call_next(request)
        response.headers["x-trace-id"] = trace_id
        elapsed_seconds = time.monotonic() - started
        elapsed_ms = int(elapsed_seconds * 1000)
        # 有界标签指标：状态码与路径模板级（job/doc 维度放日志）
        METRICS.inc(
            "owli_http_requests_total",
            {"method": request.method, "status": str(response.status_code)},
        )
        METRICS.observe(
            "owli_http_request_duration_seconds",
            elapsed_seconds,
            {"method": request.method, "status": str(response.status_code)},
        )
        logger.info(
            "http_request",
            extra={
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
        )
        # 应用调用记录（脱敏聚合：不存请求正文、Authorization、源 URL query）
        app_usage = getattr(request.state, "application_usage", None)
        live: Container | None = getattr(request.app.state, "container", None)
        if app_usage is not None and live is not None:
            try:
                await _record_application_usage(live, request, app_usage, response.status_code, trace_id)
            except Exception:
                logger.warning("usage_record_failed", extra={"trace_id": trace_id})
        return response

    async def _record_application_usage(
        container: Container,
        request: Request,
        app_usage: dict[str, Any],
        status_code: int,
        trace_id: str,
    ) -> None:
        from owli_parser.modules.access.models import ApplicationUsageRow

        segments = [seg for seg in request.url.path.split("/") if seg]
        operation_segment = segments[1] if len(segments) > 1 else (segments[0] if segments else "root")
        async with container.uow.begin() as session:
            session.add(
                ApplicationUsageRow(
                    tenant_id=app_usage["tenant_id"],
                    application_id=app_usage["application_id"],
                    token_id=app_usage.get("token_id"),
                    operation=f"{request.method} /{operation_segment}",
                    method=request.method,
                    path=request.url.path[:256],
                    status_code=status_code,
                    decision="allow" if status_code < 400 else "deny",
                    request_id=trace_id,
                )
            )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "")
        headers: dict[str, str] = {}
        if exc.http_status == 429:
            headers["retry-after"] = "30"
        return JSONResponse(status_code=exc.http_status, content=exc.to_body(trace_id), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "")
        detail: dict[str, Any] = {}
        for error in exc.errors()[:20]:
            location = ".".join(str(part) for part in error.get("loc", []) if part != "body")
            detail[location or "body"] = str(error.get("msg", "invalid"))
        return JSONResponse(
            status_code=422,
            content={
                "code": str(ErrorCode.VALIDATION_FAILED),
                "message": "输入校验失败",
                "detail": detail,
                "trace_id": trace_id,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "")
        logger.error(
            "unhandled_error",
            extra={"trace_id": trace_id, "error": exc.__class__.__name__},
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": str(ErrorCode.INTERNAL_ERROR),
                "message": "服务内部错误",
                "detail": {},
                "trace_id": trace_id,
            },
        )

    if container is not None and container.settings.http.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=container.settings.http.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["x-trace-id"],
        )

    @app.get("/healthz", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> PlainTextResponse:
        live: Container | None = getattr(request.app.state, "container", None)
        if live is None or not live.settings.observability.metrics_enabled:
            return PlainTextResponse("metrics disabled\n", status_code=404)
        import contextlib

        from owli_parser.bootstrap.observability import collect_runtime_metrics

        body = metrics_text()
        # 指标采集失败不影响既有计数（拉取方可感知缺失）
        with contextlib.suppress(Exception):
            body += await collect_runtime_metrics(live)
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    @app.get("/readyz", include_in_schema=False)
    async def readiness(request: Request) -> JSONResponse:
        live_container: Container = request.app.state.container
        try:
            from sqlalchemy import text

            async with live_container.session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse(status_code=503, content={"status": "db_unavailable"})
        if not await live_container.check_retrieval_health():
            return JSONResponse(status_code=503, content={"status": "retrieval_unavailable"})
        return JSONResponse(status_code=200, content={"status": "ok"})

    app.include_router(identity_router, prefix="/v1")
    app.include_router(catalog_router, prefix="/v1")
    app.include_router(ingestion_router, prefix="/v1")
    app.include_router(admin_router, prefix="/v1")
    app.include_router(realtime_router, prefix="/v1")
    from owli_parser.bootstrap.routes_chat import router as chat_router

    app.include_router(chat_router, prefix="/v1")
    return app
