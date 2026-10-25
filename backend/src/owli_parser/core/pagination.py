"""不透明 cursor 分页工具。

列表使用绑定过滤条件与稳定排序 (created_at, id) 或 (ordinal, id) 的
不透明 cursor；不使用 offset 深翻页。
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from owli_parser.core.errors import InvalidInputError

_MAX_CURSOR_BYTES = 1024


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    if not cursor or len(cursor) > _MAX_CURSOR_BYTES:
        raise InvalidInputError("cursor 不合法")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidInputError("cursor 不合法") from exc
    if not isinstance(data, dict):
        raise InvalidInputError("cursor 不合法")
    return data


def cursor_datetime(data: dict[str, Any], key: str = "ts") -> datetime | None:
    value = data.get(key)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise InvalidInputError("cursor 不合法") from exc
