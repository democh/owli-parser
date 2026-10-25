# 运维手册：升级、回滚、备份与恢复演练

适用部署：[deploy/compose.yaml](../deploy/compose.yaml)（Linux 生产基线）。
演练脚本：[scripts/ops_drill.sh](../scripts/ops_drill.sh)（默认只读；恢复演练只写 `owli_drill_*` 临时库）。

## 1. 拓扑与数据权威

| 组件 | 作用 | 是否权威 |
|---|---|---|
| PostgreSQL | 文档/版本/任务/配置/审计/黄金集 | **是**（状态权威） |
| artifacts 卷 | 原件、IR、预览等对象 | 对象本体（登记在 PG） |
| Valkey | Celery broker | 否（outbox + PG 可重放） |
| Qdrant / OpenSearch | 检索通道 | 否（可由 PG 重建，检索侧有权威校验） |

关键不变量：

- 提交解析时 pipeline YAML 与 A/B 变体**冻结进任务快照**，配置变更只影响新任务；
- 检索结果返回前按 PG `current_revision/enabled/删除` 复核，索引残留不会召回旧内容；
- 软删文档超过 `retention.deleted_document_retention_days` 后由 scheduler 回收索引/对象/行；
- 问答历史答案来自 `evidence_snapshots` 冻结正文，不从可变 chunk 重建。

## 2. 发布前检查

```bash
# 代码质量与契约（发布门禁）
cd backend && uv run pytest && uv run ruff check src tests && uv run lint-imports
uv run owli contracts check

# 部署与运行时
scripts/ops_drill.sh check          # compose/服务/健康/迁移版本/备份目录
scripts/ops_drill.sh backup         # 每次发布前必须备份
```

发布前确认：

1. 迁移只做 **expand**（新增列/表/索引），破坏性变更拆到后续版本；
2. `.env` 中 `POSTGRES_PASSWORD / OWLI_SESSION_PEPPER / OWLI_TOKEN_PEPPER / OWLI_SOURCE_URL_KEY` 已注入，且 pepper 轮换有独立方案；
3. 备份 `manifest.txt` 记录 migration head 与镜像 tag；
4. 当前告警无 critical（见 [docs/alerts.md](alerts.md)）。

## 3. 升级流程

```bash
# 1) 构建/拉取新镜像（tag 必须可追溯）
docker compose -f deploy/compose.yaml --profile base build api worker scheduler

# 2) 迁移（一次性容器；失败即停止发布）
docker compose -f deploy/compose.yaml --profile base run --rm migrate

# 3) 滚动重启（先 api，再 worker/scheduler；worker 在途任务按冻结快照继续）
docker compose -f deploy/compose.yaml --profile base up -d api
docker compose -f deploy/compose.yaml --profile base up -d worker scheduler

# 4) 冒烟
scripts/ops_drill.sh check
curl -fsS http://127.0.0.1:8000/readyz
```

冒烟建议覆盖：登录 → 上传小文件 → 任务 DONE → 召回测试 → 问答（含引用）→ `/metrics` 出现新版本流量。

**回滚边界**：升级期间旧 worker 可能仍在跑旧代码；任务快照保证配置一致，但二进制行为差异仍在，因此滚动窗口内避免修改 pipeline，等全部 worker 重启完成。

## 4. 回滚流程

```bash
scripts/ops_drill.sh rollback-check   # 只读：镜像/服务/迁移版本/健康
```

决策树：

1. **仅应用回滚（迁移向后兼容）**：切回上一镜像 tag，重启 api/worker/scheduler；不执行 `alembic downgrade`；
2. **迁移为 expand/contract 中间态**：应用可回滚，DB 保持新结构（旧代码忽略新列）；
3. **破坏性迁移**：禁止 downgrade，按第 5 节恢复备份到新实例/新库，再切流量；
4. **索引异常**：回滚应用后按 `POST /v1/index/actions` 或版本重建流程重发索引，不直接回滚索引代。

回滚后必须重新执行 `scripts/ops_drill.sh check` 与一次端到端冒烟。

## 5. 备份与恢复演练

### 5.1 备份

```bash
scripts/ops_drill.sh backup                 # ./backups/<UTC>/
scripts/ops_drill.sh backup /backup/owli-1  # 指定目录
```

产物：

- `db.dump`：`pg_dump -Fc`（含 schema + 数据 + `alembic_version`）；
- `artifacts.tgz`：容器内 Python `tarfile` 打包 `/data/artifacts`；
- `manifest.txt`：migration head、镜像、sha256、字节数。

一致性说明：DB dump 与 artifacts 归档**顺序执行、非原子**。建议在低峰期备份；GC 窗口内避免同时执行 backup。恢复后以“抽样对象可读 + 引用计数”校验，不承诺跨秒级一致。

### 5.2 恢复演练（不触碰生产）

```bash
scripts/ops_drill.sh restore-drill ./backups/<UTC>
# 指定演练库名（必须以 owli_drill_ 开头）
scripts/ops_drill.sh restore-drill ./backups/<UTC> owli_drill_20261017
```

脚本行为：

1. 重建 `owli_drill_*` 数据库（`dropdb --force` 仅作用于演练库）；
2. `pg_restore --no-owner --no-privileges`；
3. 校验 `documents/doc_versions/chunks` 计数与 `alembic_version`；
4. **只读**校验 artifacts 归档条目数/字节数（不落生产卷）；
5. 输出演练 RTO；演练库保留供检查，最后可手动 drop。

### 5.3 RPO / RTO 与保留

| 指标 | 建议 | 说明 |
|---|---|---|
| RPO | ≤ 24h（每日备份） | 关键租户可缩短到小时级；备份目录需异地副本 |
| RTO | 以演练实测为准 | `restore-drill` 输出值即恢复耗时基线；大 artifacts 卷另计 |
| 备份保留 | 本地 7 天 + 异地 30 天 | 合规保留策略按租户合同调整 |
| 演练频率 | 每季度一次 + 大版本发布前 | 记录到下方模板 |

## 6. 故障处置

| 告警/现象 | 首查 | 处置 |
|---|---|---|
| `OwliJobsQueueBacklog*` / `OwliWorkersNotLeasing` | `compose ps`、worker 日志、`owli_active_leases_total` | 恢复 worker；确认队列名与 broker；必要时扩容 cpu slots |
| `OwliOutboxLag*` | scheduler 日志、Valkey 连通性 | outbox 是“先落库再派发”，恢复 scheduler/broker 后自动追平 |
| `OwliStageP95*` | 阶段标签（parsing/indexing） | 慢在 parsing 看适配器/OCR；indexing 看检索引擎；queue 是容量问题 |
| `OwliApiP95Slow` | `trace_id` / 路径日志 | 慢查询、存储或模型端点；4xx 与 5xx 分状态定位 |
| `OwliTokenRejection*` | `reason=invalid/disabled` | 轮换泄漏凭据、停用应用残留客户端 |
| `OwliDiskLow/Critical` | artifacts 卷、PG 数据卷 | 扩容/清理；先降 `retention.deleted_document_retention_days` 前需评估合规 |
| `owli_retention_errors_total` 增长 | scheduler 日志 | 索引/对象删除失败会在下一轮重试；持续失败查存储权限与索引引擎 |
| 检索不可用 | `/readyz`、引擎健康 | 返回“索引暂不可查询”，不回落旧结果；恢复后按 manifest 重发 |
| 问答 `capability_not_ready` | 模型绑定/档案/连接 | 检查 generation 绑定与凭据；budget 超限见 `budget_exceeded` |
| `GET /v1/adapters/health` 为 `degraded/unavailable` | `stale` 列表 | 检查对应外部 adapter 进程与心跳任务；恢复上报后自动回到 ready |

### 6.1 外部 adapter 心跳与健康聚合

- 注册/更新（平台管理员）：`PUT /v1/adapters/{name}`，提交 version/mode/capabilities/config_ref/health；
- 心跳：`POST /v1/adapters/{name}/heartbeat`（可只带 `health`）；未注册返回 404，不隐式创建；
- 生效健康度为读时判定：超过 `parsing.adapter_heartbeat_ttl_seconds`（默认 90s）未上报 → `unavailable`；
  数据库保留最近一次上报值（`reported_health`），用于区分“上报 degraded”与“失联”；
- 聚合：`GET /v1/adapters/health` 返回 `status/counts/stale`；任一 unavailable → `unavailable`，
  存在 degraded/maintenance/unknown → `degraded`，全部 ready → `ok`；
- 内置 adapter 由进程内注册表提供、恒 ready；同名外部注册返回 409（不允许遮蔽内置实现）。

## 7. 容量与调优

| 配置 | 作用 | 调整建议 |
|---|---|---|
| `jobs.cpu_slots/gpu_slots` | worker 并发 | 与 CPU/内存上限同步调整；解析是 CPU 密集 |
| `jobs.lease_seconds/heartbeat_seconds` | 租约 | heartbeat 必须小于 lease；大文件解析可适度增大 |
| `jobs.stage_deadline_seconds` | 单阶段上限 | 与 P95 告警阈值联动 |
| `jobs.outbox_poll_seconds/batch_size` | 派发吞吐 | 积压时先加 batch，再查 broker |
| `retention.deleted_document_retention_days` | 软删保留 | 合规优先；GC 按 `gc_interval_seconds` 限频 |
| `retrieval.channels` | dense/lexical | 真实引擎接入后按 Recall/nDCG 基线调整 |
| `chunk.target_tokens/max_tokens` | 分块 | 变更需重建索引并按 A/B 实验验证 |
| `parsing.adapter_heartbeat_ttl_seconds` | 外部 adapter 失联判定 | 与心跳间隔联动（建议 ≥ 3 个上报周期） |

## 8. 演练记录模板

```text
日期：
版本/镜像 tag：
备份目录：             manifest sha256：
演练库：               RTO：            s
documents / versions / chunks：
artifacts entries / bytes：
发现的问题与修复：
是否通过（Y/N）：
```
