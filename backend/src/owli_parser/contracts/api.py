"""API 通用契约：统一错误体与列表容器。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """统一错误体 {code, message, detail, trace_id}。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    trace_id: str


class ListPage[T](BaseModel):
    """列表固定 {items, next_cursor, has_more}。"""

    model_config = ConfigDict(extra="forbid")

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


class AcceptedTask(BaseModel):
    """异步任务 202 响应：资源 ID 与 status_url（本系统相对路径）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    state: str
    status_url: str


class RuntimeConfig(BaseModel):
    """登录前可公开配置；不包含租户、模型连接或密钥。"""

    model_config = ConfigDict(extra="forbid")

    app_name: str
    environment: str
    schema_version: str = "1.0"
