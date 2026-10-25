"""结构化日志与基础指标（Prometheus 文本格式，无外部依赖）。

- 日志为 JSON，trace_id 贯穿；只记录安全 ID、错误码、耗时。
- 指标标签有界（adapter/格式/状态/阶段），job_id/doc_id 不放指标，放入日志。
- 直方图为固定桶（秒），供 histogram_quantile 计算 P50/P95。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import defaultdict
from typing import Any

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False
_lock = threading.Lock()


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    global _configured
    with _lock:
        if _configured:
            return
        root = logging.getLogger()
        root.setLevel(level.upper())
        handler = logging.StreamHandler(sys.stdout)
        if json_output:
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.handlers = [handler]
        # 降低三方库噪音
        for noisy in ("uvicorn.access", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class _Counter:
    """有界标签计数器（Prometheus 文本导出）。"""

    def __init__(self) -> None:
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, name: str, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._values[key] += amount

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._values.items()):
                if labels:
                    label_text = ",".join(f'{k}="{v}"' for k, v in labels)
                    lines.append(f"{name}{{{label_text}}} {value}")
                else:
                    lines.append(f"{name} {value}")
        return "\n".join(lines) + ("\n" if lines else "")


_HISTOGRAM_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)


class _Histogram:
    """固定桶直方图（Prometheus 文本导出：_bucket/_count/_sum）。"""

    def __init__(self, buckets: tuple[float, ...] = _HISTOGRAM_BUCKETS) -> None:
        self._buckets = buckets
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._values[key].append(max(float(value), 0.0))

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), values in sorted(self._values.items()):
                label_text = ",".join(f'{k}="{v}"' for k, v in labels)
                bucket_prefix = f"{label_text}," if label_text else ""
                for bound in self._buckets:
                    count = sum(1 for value in values if value <= bound)
                    lines.append(f'{name}_bucket{{{bucket_prefix}le="{bound:g}"}} {count}')
                lines.append(f'{name}_bucket{{{bucket_prefix}le="+Inf"}} {len(values)}')
                labels_block = f"{{{label_text}}}" if label_text else ""
                lines.append(f"{name}_count{labels_block} {len(values)}")
                lines.append(f"{name}_sum{labels_block} {sum(values):.6f}")
        return "\n".join(lines) + ("\n" if lines else "")


class _Registry:
    """指标注册表：计数与耗时直方图（进程内聚合，拉取时渲染）。"""

    def __init__(self) -> None:
        self._counters = _Counter()
        self._histograms = _Histogram()

    def inc(self, name: str, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        self._counters.inc(name, labels, amount)

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        self._histograms.observe(name, value, labels)

    def render(self) -> str:
        return self._counters.render() + self._histograms.render()


METRICS = _Registry()


def metrics_text() -> str:
    return METRICS.render()
