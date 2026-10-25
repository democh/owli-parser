"""ORM 列类型与列助手（各模块 models 共用）。

- JSON 列统一使用可移植 JSON（PG 侧为普通 json，兼容 sqlite 开发）。
- 所有时间列使用带时区的时间戳，值统一 UTC。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from owli_parser.infrastructure.db import utcnow


def pk_column() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def fk_column(target: str, *, nullable: bool = False, **kwargs: Any) -> Mapped[uuid.UUID]:
    # 类型由目标列推断，避免显式重复声明
    return mapped_column(ForeignKey(target), nullable=nullable, **kwargs)


def json_column(**kwargs: Any) -> Mapped[Any]:
    return mapped_column(JSON, **kwargs)


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
