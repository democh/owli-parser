# 告警规则与阈值

指标由 `GET /metrics` 暴露（Prometheus 文本格式，无外部依赖）。当
`observability.metrics_enabled=false` 时该端点返回 404，告警规则应视为不可用而非静默通过。

规则文件：[deploy/prometheus/owli-alerts.yaml](../deploy/prometheus/owli-alerts.yaml)（PrometheusRule 兼容）。

## 指标清单

| 指标 | 类型 | 标签 | 含义 |
|---|---|---|---|
| `owli_jobs_pending_total` | gauge（拉取现算） | — | 非终态解析任务数（积压口径） |
| `owli_jobs_oldest_pending_seconds` | gauge（拉取现算） | — | 最老非终态任务的等待秒数 |
| `owli_active_leases_total` | gauge（拉取现算） | — | `lease_until` 未过期的任务数（worker 活跃度） |
| `owli_outbox_pending_total` | gauge（拉取现算） | — | 待派发 outbox 事件数 |
| `owli_outbox_oldest_pending_seconds` | gauge（拉取现算） | — | 最老待派发事件的滞后秒数 |
| `owli_stage_duration_seconds` | histogram | `stage` | 阶段耗时：`upload`/`queue`/`parsing`/`normalizing`/`chunking`/`indexing`/`publishing` |
| `owli_http_request_duration_seconds` | histogram | `method`/`status` | API 请求耗时（路径与 trace_id 只在结构化日志） |
| `owli_token_rejected_total` | counter | `reason` | 应用 Token 拒绝数（粗粒度：`invalid`/`disabled`） |
| `owli_http_requests_total` | counter | `method`/`status` | HTTP 请求计数（路径与 trace_id 只在结构化日志） |
| `owli_retrieval_queries_total` | counter | `low_relevance` | 召回请求数 |
| `owli_retrieval_channel_queries_total` | counter | `channel` | 每通道召回请求数 |
| `owli_retrieval_dropped_total` | counter | — | 被权威校验丢弃的候选数 |
| `owli_retention_purged_documents_total` | counter | — | 保留期后回收的软删文档数 |
| `owli_retention_purged_artifacts_total` | counter | — | GC 回收的 source/IR artifact 数 |
| `owli_retention_index_entries_total` | counter | — | GC 删除的索引条目数 |
| `owli_retention_errors_total` | counter | — | GC 单次失败计数（索引/对象删除） |

说明：

- gauge 在每次拉取时从 PG 现算；采集失败时 gauge 缺失（计数器仍输出），规则静默是预期行为。
- 直方图桶固定为 0.05/0.1/0.25/0.5/1/2.5/5/10/30/60/120/300 秒，`le="+Inf"` 兜底。
- 标签有界：不含 job_id、doc_id、租户 ID；这些只进 JSON 日志。

## 阈值表

阈值与默认配置对齐（[settings.py](../backend/src/owli_parser/bootstrap/settings.py)）：

| 告警 | 表达式 | 阈值 | 依据 |
|---|---|---|---|
| `OwliJobsQueueBacklogWarning` | `owli_jobs_oldest_pending_seconds` | > 1800s / 10m | `queue_timeout_seconds=3600` 的一半 |
| `OwliJobsQueueBacklogCritical` | `owli_jobs_oldest_pending_seconds` | > 3600s / 5m | 超过 `queue_timeout_seconds`，任务可能永远无法被领取 |
| `OwliWorkersNotLeasing` | pending > 0 且 active leases = 0 | 5m | `lease_seconds=120`、`heartbeat_seconds=30`；持续无租约即 worker 停摆 |
| `OwliOutboxLagWarning` | `owli_outbox_oldest_pending_seconds` | > 30s / 5m | `outbox_poll_seconds=2`；正常应秒级清空 |
| `OwliOutboxLagCritical` | `owli_outbox_oldest_pending_seconds` | > 300s / 5m | `retry_backoff_max_seconds=300`，超过即派发链路故障 |
| `OwliStageP95Slow` | `histogram_quantile(0.95, …)` | > 600s / 10m | 解析阶段慢于 10 分钟需关注容量 |
| `OwliStageP95ExceedsDeadline` | 同上 | > 1800s / 5m | `stage_deadline_seconds=1800`，任务即将超时失败 |
| `OwliApiP95Slow` | `histogram_quantile(0.95, …http_request_duration…)` | > 2s / 10m | 接口级慢请求，按 method/status 定位 |
| `OwliTokenRejectionSpike` | `sum(rate(owli_token_rejected_total[5m]))` | > 0.1/s（6 次/分） | 正常轮换/误配的低频拒绝 |
| `OwliTokenRejectionFlood` | 同上 | > 1/s（60 次/分） | 疑似凭据泄露或暴力尝试 |
| `OwliHttp5xxRate` | 5xx 占比 | > 5% / 10m | 平台级错误率 |
| `OwliDiskLow` | `node_filesystem_avail_bytes / size` | < 15% / 15m | 容量类，需 node_exporter |
| `OwliDiskCritical` | 同上 | < 5% / 5m | 解析临时目录与本地存储共用磁盘 |

## 接入示例

```yaml
scrape_configs:
  - job_name: owli-parser
    metrics_path: /metrics
    static_configs:
      - targets: ["owli-api:8000"]
```

规则加载（kube-prometheus-stack）：

```bash
kubectl apply -f deploy/prometheus/owli-alerts.yaml
```

## 排障要点

- **任务积压 / worker 停摆**：先看 `owli_active_leases_total` 是否为 0；再看 worker 日志中
  `acquire_lease` 与 broker 连接；确认 `jobs.queues` 与 worker 订阅队列一致。
- **outbox 滞后**：检查 dispatcher 循环是否运行、broker 是否可达；outbox 是"先落库再派发"，
  pending 不会丢，但会延迟事件流与重试。
- **阶段 P95 超时**：`stage=parsing` 慢通常是适配器/OCR；`indexing` 慢看检索引擎；
  `queue` 慢是容量问题，不是单任务问题。
- **Token 拒绝激增**：按 `reason` 区分；`disabled` 多为应用被停用后的残留客户端，
  `invalid` 需排查泄露与重试风暴（审计日志含 fingerprint，不含明文）。
- **磁盘**：应用不采集磁盘水位；没有 node_exporter 时请以部署平台的文件系统监控替代。
