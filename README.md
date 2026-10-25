# owli-parser

企业知识库文件解析与知识库工作台（自研编排 · 分级部署 · 插件化解析框架）。

设计基线见 [kb_parser_delivery_document.md](kb_parser_delivery_document.md)。本仓库为按该文档
第五部分实施规范生成的工程：模块化单体 + 独立 worker，Python 3.12 由 uv 管理，
前端 React 19 + Vite + TypeScript strict + Tailwind。

## 目录结构

```text
backend/     Python 工程（src/owli_parser 包；命令 `owli`；环境变量前缀 OWLI_）
  src/owli_parser/
    bootstrap/      应用工厂、容器、配置、CLI、worker/scheduler 入口
    workflows/      跨模块事务编排（提交解析、执行、发布、导入下载）
    core/           ID、时钟、上下文、错误、加密工具
    contracts/      IR 2.0、事件、scope 注册表（前后端单一事实源）
    modules/        identity/access/catalog/artifacts/ingestion/jobs/parsing/
                    knowledge/configuration/chat/retrieval（含索引发布与黄金集评估）
    ports/          Storage/Model/Queue/Secret/SafeFetcher/Outbox/Audit/Idempotency/
                    Retrieval（Dense/Lexical 索引协议）
    adapters/       local/s3/aliyun_oss 存储、SafeFetcher、来源 URL 加密存储、
                    内存检索索引（M3 换真实引擎，同一 contract 套件验收）
    infrastructure/ DB/UoW、outbox、审计、幂等、broker、遥测
  migrations/       Alembic 统一版本链
  tests/            unit/契约/安全/多租户/端到端
frontend/   React 19 工作台（登录/租户切换/数据集/文档/任务/召回测试/工作台）
contracts/  OpenAPI 与 IR/事件 JSON Schema 快照（owli contracts export/check）
deploy/     Dockerfile 与 Compose profiles（base / retrieval）
config/     无密钥环境覆盖示例（OWLI_CONFIG_FILE）
scripts/    跨平台开发入口（scripts/dev.sh）
```

## 快速开始（本地开发）

```bash
# 1. 后端依赖（uv 自动管理 Python 3.12）
uv sync --project backend --extra cpu

# 2. 生成测试密钥（生产必须使用独立密钥管理）
export OWLI_SESSION_PEPPER=$(openssl rand -hex 32)
export OWLI_TOKEN_PEPPER=$(openssl rand -hex 32)
export OWLI_SOURCE_URL_KEY=$(openssl rand -base64 32)

# 3. 校验配置 + 迁移 + 初始化管理员
uv run --project backend owli config validate
uv run --project backend owli db upgrade
uv run --project backend owli admin bootstrap --username admin

# 4. 启动进程（3 个终端）
uv run --project backend owli serve        # API: http://127.0.0.1:8000
uv run --project backend owli worker --pool cpu
uv run --project backend owli scheduler

# 5. 前端
pnpm --dir frontend install
pnpm --dir frontend dev                    # http://127.0.0.1:5173
```

本地默认使用 sqlite（仅开发）；生产为 PostgreSQL + Valkey，见 `deploy/compose.yaml`
（复制 `.env` 所需变量：POSTGRES_PASSWORD、OWLI_SESSION_PEPPER、OWLI_TOKEN_PEPPER、
OWLI_SOURCE_URL_KEY）。

## 检查与测试

```bash
uv run --project backend ruff check backend
uv run --project backend ruff format --check backend
uv run --project backend lint-imports --config backend/pyproject.toml
uv run --project backend mypy --config-file backend/pyproject.toml backend/src
uv run --project backend pytest -c backend/pyproject.toml backend/tests
pnpm --dir frontend check                  # typecheck + lint + vitest + build
```

后端测试包含一条从空库执行 Alembic `upgrade head` 的迁移链测试；CI 另外在
PostgreSQL 16 上执行升级幂等校验。GitHub Actions 还运行 Ruff、mypy strict、
架构契约、前后端测试、构建和契约快照检查，任一项失败都会阻断合并。

## 契约工作流（OpenAPI / IR / 事件 Schema）

```bash
cd backend
uv run owli contracts export          # 导出快照到仓库根 contracts/
uv run owli contracts check           # 与代码对比；漂移以非零退出码失败（CI 门禁）
uv run owli eval-retrieval --file golden.json   # 检索黄金集评估（退出码即回归护栏）
uv run owli compare-ir --tenant <tid> --reference <v1> --hypothesis <v2>\
    --max-cer 0.05 --min-reading-order 0.95   # 版本解析质量对比（落 golden_runs）
uv run owli parsing-regression --file cases.json --tenant <tid>  # 批量解析回归（聚合+阈值）

cd ../frontend
pnpm gen:api                          # openapi-typescript 从快照生成 src/api/generated.ts
pnpm test                             # 含契约漂移测试：request 路径逐段对比快照
```

变更 API 后：先 `contracts export` 并提交快照，再 `pnpm gen:api` 更新生成类型；
前后端任一侧漏提交都会被 `contracts check` / vitest 契约测试拦住。

## 当前里程碑状态（对照文档 §18）

- **M1（进行中，纵向闭环可用）**：多租户/固定角色/成员；用户 Session（CSRF、限流、
  即时撤销）；应用 Token（scope + 数据集 grant + 撤销传播 + 集中式 scope 强制）；
  Local 存储与数据集绑定；存储 profile 生命周期（草稿/校验/隔离前缀 IO 测试/发布/
  读写停用开关，按 name+revision 不可变）；对象导入位置（inbox 前缀不得与受管
  前缀重叠）；模型连接管理（连接与凭据 write-only、显式 /models 测试与发布分离、
  报告脱敏）；小文件 multipart 直传与分片上传 → artifact；URL 导入
  （SSRF 防护、加密保存、脱敏展示）；解析任务状态机（租约/fence/CAS、事件流、
  取消/重试）；任务事件 WebSocket（一次性 ws-ticket、快照+seq 增量、撤权即断、
  HTTP 事件查询降级）；TXT/MD/CSV/数字 PDF/DOCX/PPTX/XLSX 解析（提交时携带
  原始文件名影响预检与 adapter 路由）→ **M2 后处理**（文本语言检测（脚本级：
  zh/ja/ko/ru/ar/latin/mixed/unknown，短文本与混合不强制归类，hint 优先；
  拉丁细分留待 fastText/准入替代）、精确去重（空白折叠后相等且 ≥8 字符的块
  仅留首个，比例入质量报告）、**近重复提示**（字符 bigram Jaccard ≥0.7，
  只报告不删除；块数>800 跳过并记 warning 诚实降级；明细写入 quality.notes）、
  乱码率（U+FFFD 占比））→ IR 2.0（quality-v3）
  → 结构化分块（父块/表格行组）→
  版本发布；**解析质量指标骨架**（离线评估纯函数：CER/WER（编辑距离归一化，
  空参考边界写死）、表格单元格结构 P/R/F1 与文本精确率（TEDS 需树编辑距离，
  留待专门评估包，当前如实标注为基线）、阅读顺序 LCS 覆盖、bbox IoU（来源
  定位误差））；**解析质量对比**（候选 IR vs 基准 IR：text_cer/阅读顺序/
  表格结构 F1 均值/定位 IoU 均值；无表格对或定位对时为 None 不伪造；
  支持从版本 IR artifact 直接自对比为基线；**版本级对比运行**：
  `owli compare-ir`（读两版本 IR artifact → 指标 + 阈值判定（max-cer/
  min-reading-order）→ 落 golden_runs（质量页可见，state=passed/failed）；
  跨租户/不存在版本 404 不泄露存在性）；**批量解析回归**：
  `owli parsing-regression`（cases=版本对列表 + expectations 阈值；宏平均仅对
  存在值计算（缺失不按 0 计入）；逐 case 明细与聚合一并落 golden_runs；
  空集/字段缺失明确拒绝）；文档工作台（版本/分块/来源锚点 + 分页 blocks 接口，
  含进程内 IR 缓存）；
  任务中心；复核队列（低置信块与缺失单位随解析自动登记（分 reason、幂等重建，
  未评估置信度不误报）；列表/处置 CAS（仅 open 可处置，重复 409）；前端复核页
  `/review`（状态切换、动作白名单、备注、409 冲突提示与刷新、document_id
  存在的项直接“打开工作台定位”；支持 URL 驱动的 `version_id`/`reason` 过滤
  （后端队列接口同名 query 参数）；工作台版本“待复核 N”徽标即链接到过滤视图）；
  **版本质量摘要**：versions 接口返回
  `pending_review_count`（单次聚合防 N+1），工作台版本选择器显示“待复核 N”
  徽标（0/缺失不渲染）；选中版本展示**质量摘要**（空块率/乱码率/去重率/
  近重复对/平均置信度中文 chips，-1 哨兵显示“未评估”不伪造成 0，
  notes 与近重复明细折叠展示））；**解析管道版本化配置**：`GET/PUT /v1/pipelines/{name}`
  （PUT 首次免 If-Match、更新须 If-Match=当前版本否则 412；保存前编译校验
  （非法 YAML/name 不一致 422）；保存生成新版本不覆盖历史，旧版自动失活；
  租户配置同名覆盖内置 default）；**提交冻结 + 快照执行**：提交时把 pipeline
  YAML 冻结进任务快照（execution_plan.yaml_text + digest 入 version），worker
  仅按快照重建执行（不再重新编译默认——修复快照与执行可能不一致）；前端
  `/settings/pipelines`（列表/查看/编辑保存/执行计划预览，admin）；
  runtime-config、capabilities 与
  Prometheus 文本指标（/metrics）；**平台监控 gauge**（拉取时现算）：任务积压
  （pending/最老年龄）、活跃租约（worker 心跳）、outbox 滞后（pending 数/最老
  lag）；Token 拒绝计数（invalid/disabled 粗粒度，不为枚举探测提供信号）；
  应用调用记录（脱敏聚合）；
  前端设置页：应用与 Token（创建向导/scope 注册表/数据集授权矩阵/签发一次性
  弹窗/撤销确认/调用记录）、存储设置（获准 root 池/连接测试步骤展示/读写停用）、
  模型设置（凭据 write-only/显式测试与发布分离）；设置路由仅 admin 可见；
  任务详情页（WS 事件流 + 快照轮询降级、seq 去重、阶段视图、取消/重试）；
  工作台 IR 块视图（类型过滤、cursor 分页、来源锚点/未评估置信度/provenance；
  支持 `?version_id=&block_id=` 直达定位，复核队列可一键跳入）；
  文档预览器（preview descriptor + PDF（pdf.js 懒加载分包）/图片/文本/L4 兜底，
  来源锚点点击 → 翻页 + bbox 百分比高亮，预览与解析/索引状态相互独立）；
  **预览 ↔ 块列表联动**：预览翻页事件对外播报（含来源锚点驱动的页切换），
  包含当前页来源锚点的块高亮标记（data-page-match），可选“跟随预览翻页”
  自动滚动到首个匹配块；文本预览无分页不参与联动（不伪造滚动映射）；
  **近似来源标注**：来源锚点按定位质量分级标注（区域高亮 / 仅到页 / 无页面
  坐标不可定位）；预览版本与列表版本不一致（如未发布版本回退预览原件）时
  显式提示“锚点可能偏移”。
  检索地基（IndexDocument/IndexFilter 端口契约、RRF 融合严格语义、内存 dense（余弦）
  与 BM25 contract 实现、RetrievalService 混合管线（前置过滤 → RRF → PG 权威校验 →
  TopK + 低相关警告）、`/v1/retrieval/test` 在 enabled=true 时真实走管线；
  前端召回测试台 `/retrieval`：通道 rank/原始分/RRF 贡献展示、低相关显式警告、
  结果跳工作台；未启用时如实提示 capability_not_ready；
  **召回参数可调**：TopK / RRF k / 每通道上限 / channel_weights / 低相关阈值
  可逐次覆盖（缺省取配置，candidate_limit 不可覆盖防绕过），响应回显
  applied_params 供对比实验；非法权重键 400、越界值 422；
  **A/B 参数对比**：同 query 两组参数顺序执行（top_k 共享保证可比），
  并排 diff 展示仅 A/仅 B/共有项、逐项排名与 RRF 变化（Δ 正数=B 组前移）
  与平均排名变化。
  **索引发布写入端（lexical 通道真实受控闭环）**：解析发布时按 manifest 冻结集合
  （chunk_id:revision + digest）写入已启用通道并逐通道确认，全部确认后
  `retrieval_ready=true`；并发 revision 漂移时确认被拒（任务失败，安全方向优先）；
  编辑后旧索引条目由权威校验自然剔除（不召回旧内容），`/v1/chunks/{id}/reembed`
  按 PG 当前 revision 重建单块（写权限经数据集授权判定）；配置要求 dense 而无
  embedding 绑定时任务显式 capability_not_ready（不静默降级）；
  **编辑 → 重索引闭环**：PATCH chunk 与编辑同事务登记 index_actions + outbox
  （index.apply）→ scheduler 派发 → worker 消费（幂等，仅 pending；按 PG 最新
  revision 重建单块）；失败自动重派（上限 3 次后 failed，可手动 reembed 补救）；
  禁用变更不登记行动（权威层即时生效）；工作台分块详情提供编辑正文（If-Match）
  与重建索引入口；
  **检索质量基线（golden runner）**：Recall@K / P@K / MRR / nDCG@K（分级增益）
  纯函数指标 + 宏平均聚合 + 按 groups 分层（缺失组不填充 0）；黄金集同名幂等
  更新（golden_sets），运行固定 adapter_ref/config_digest 落 golden_runs
  （state=passed/failed）；`owli eval-retrieval --file cases.json` 退出码即 CI
  回归护栏；`GET /v1/quality/regression-runs` 只读回查（仅指标不含内容）；
  **回归运行触发**：`POST /v1/quality/regression-runs`（tenant admin，同步执行，
  用例数超 200 需走 CLI；跨租户 404、检索未启用 503）；质量页可一键触发；
  CLI 与 API 共用同一 search_fn 工厂（评估口径一致）；
  **质量回归面板 `/quality`**：运行历史（state 徽标/指标 chips/违规项）与
  任选两次运行的指标 diff（仅单侧存在的指标记 —，不伪造 0）；**多黄金集**：
  列表可按 `golden_set_id` 过滤（URL 驱动）并回传集名（outer join，孤立历史
  不静默隐藏），过滤下拉从运行历史派生；**结果导出**：召回 A/B diff 与质量
  指标 diff 可导出 JSON/CSV（纯前端下载，单侧缺失留空、时间戳文件名，
  受限环境不静默返回失败）；**指标趋势图**：零依赖 SVG 折线（不引图表库；
  单点/同值/空序列均有明确行为；failed 运行以警示色点标记；min/max 如实标注，
  不伪造 0 基线）；
  **契约快照与防漂移**：`owli contracts export/check` 导出版本化 OpenAPI + IR/事件
  JSON Schema（确定性序列化），漂移以退出码拦截；前端 `pnpm gen:api`
  （openapi-typescript）生成类型镜像，vitest 契约测试扫描全部 request 路径
  逐段对比快照（含模板参数/query 归一化）；**生成类型渐进采用**：写入体经
  `satisfies`/契约守卫函数（identity，零运行时开销）接 generated.ts，
  字段漂移由 tsc 拦截（已覆盖 DatasetCreate/ChunkPatch/RetrievalTestRequest/ReviewResolve）；
  **Office 转换地基**：ports/conversion 端口 + soffice 适配器（二进制探测/
  隔离 profile/超时/错误映射/产物校验，runner 可注入）+ fake 契约套件；
  **检索审计与观测**：每次检索记结构化日志（query 仅存 sha256 指纹与长度，
  不落原文；含通道命中数/融合候选/权威剔除数/返回数/耗时 ms/low_relevance）；
  Prometheus 计数 `owli_retrieval_queries_total`（按 low_relevance 标签）、
  `owli_retrieval_channel_queries_total`、`owli_retrieval_dropped_total`；
  高频读不写 audit_logs 表（避免写放大与污染操作审计）；
  **存储 contract suite**：Local / S3 / OSS 三后端跑同一套语义断言（client
  可注入 fake SDK，不触网）：roundtrip、范围读、校验和不符拒绝、多分片
  提交/中止/编号校验、缺失对象语义、presign 按 capabilities 分支。
- **尚未完成（按文档基线后续里程碑）**：OCR/VLM/ASR 与媒体池（M2/M4）、dense+BM25
  真实引擎接入（M3：Qdrant/OpenSearch adapter 以同一 contract suite 验收；当前
  内存索引仅开发/单进程可见，生产需真实引擎）、S3/OSS 真实云链路验收（适配器
  语义已由 contract suite 以 fake 注入锁定，凭据/网络/SDK 版本行为待独立验收）、
  Office 转 PDF 的段落→页定位映射与真实 LibreOffice 环境验收（转换端口/适配器/
  契约套件已就绪：二进制探测、隔离 profile、超时与错误映射、页数读取），
  模型能力 profile/用途绑定（三层：连接 → 能力档案 → 绑定全部就绪：
  档案不可变（能力/模型变更须新建）、embedding 能力必须声明维度（索引契约）、
  绑定校验用途能力与发布状态、If-Match 版本控制、停用被引用档案 409；
  `GET/POST /v1/model-profiles`、`POST /v1/model-profiles/{id}/state`、
  `GET /v1/model-bindings`、`PUT /v1/model-bindings/{purpose}`）；**embedding 推理接线**
  （OpenAI 兼容 /embeddings 客户端：httpx 共享 client、响应守卫（条数/维度不符显式失败
  不截断不补齐）、HTTP/超时错误映射；按租户从绑定解析 provider（绑定变更无需重启）；
  检索配置含 dense 时需绑定，否则任务 capability_not_ready 失败不静默降级）；dense 推理
  客户端真实验收待凭据（链路已由 MockTransport 端到端验证：发布写入 dense → 双通道检索命中）；**配置治理**（`config_revisions` 发布审计：模型连接/存储 profile/pipeline 保存均落不可变记录（含规范化 sha256 digest，审计载荷脱敏不含密钥引用本体）；`secret_refs` 密钥引用登记：连接“连接带 credential_ref + 存储 profile 创建”幂等登记（同引用刷新绑定端点）、轮换标记 key_version 递增；API `GET /v1/config-revisions`、`GET /v1/secret-refs`、`POST /v1/secret-refs/{id}/rotate`（绝无明文值））；**rerank 接入**（OpenAI/Cohere 兼容 /rerank 客户端：响应 index 须覆盖 [0,n) 恰好一次（缺项/越界/重复均显式失败不猜测）；rerank_model_ref 非空即启用，无 rerank 用途绑定 → 503 不静默降级；仅作用于权威校验之后的候选；正文经 PG 权威回查（索引态文本不作事实源），缺失候选不评分、保持原序置后并记 warning；响应回显 rerank_applied/rerank_score）；**来源矩阵 fixture**（reportlab 生成真实文字层 PDF + pypdf 旋转/裁剪变换：旋转页 rotation=90 如实进 IR、文本不丢失不串页（page_index 归属断言）、裁剪页文本与页归属不变、覆盖率完备）；**问答最小闭环（M4 起步）**（`POST /v1/chat/runs` SSE 流式：检索→证据冻结（evidence_snapshots 记 chunk_revision/acl_refs/正文与来源快照，历史答案不从可变 chunk 重建）→流式生成（generation 用途绑定，缺失即 capability_not_ready）→引用校验（[n] 越界即伪引用拒绝，不落 answer.done）→无证据明确拒答；事件逐条持久化（chat_events seq 唯一），`GET /v1/chat/runs/{id}/events?after_seq=` 断流重放，客户端断开→run=cancelled；应用 token 需 chat:invoke scope）；Pipeline Studio 可视化与 A/B、
  跨页/模糊块匹配的自动近似定位（当前为锚点质量分级 + 版本漂移提示，
  不自动模糊匹配以免伪造定位）。

## 安全与合规要点（实现中已落地）

- 密钥只写引用（`env:`/`file:`），数据库与快照不存明文；URL 导入的完整 URL
  经 Fernet 加密落盘、审计与前端只显示脱敏 host。
- 外部应用与浏览器用户两条认证链；同时携带两种凭据直接拒绝；X-Tenant-ID
  与 Token 绑定租户不一致拒绝。
- SafeFetcher：逐跳 DNS 全量校验（含 IPv4-mapped IPv6）、对端地址核对、
  逐跳重定向限制、HTTPS 降级拒绝、offline 模式仅放行批准的内网服务。
- 写路径：pending location → 写入 → CAS active；读路径全走应用代理
  （`Cache-Control: private, no-store`），预览/下载再次鉴权。
- 发布协议：desired/visible 版本分离、CAS 发布、fence 校验；`PARTIAL_DONE`
  仅在发布成功后产生，缺失单位在 coverage 报告中显式列出。
- 许可证与依赖版本在发布流程中逐版本核对（见文档 §17）；本仓库不构成法律意见。
