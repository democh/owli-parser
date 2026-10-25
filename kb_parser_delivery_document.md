# owli-parser · 文件解析与知识库工作台交付文档

> 版本 v1.2 实施细化版 · 2026-10-15
> 项目名称：owli-parser · 自研编排与产品层，复用开源解析引擎 · 商用合规按交付版本审查 · 分级支持 Linux / Windows / macOS
> 文档构成：总体设计方案 v2.2 ＋ 后端设计文档 v1.2 ＋ 前端设计文档 v1.4 ＋ 优化实施补充规范 ＋ AI 工程生成实施规范
> 本文是设计基线，不代表已有实现或已通过性能、兼容性及法律验收；新增数值均为初始配置或候选验收目标。

## 修订记录

| 版本 | 日期 | 内容 |
| --- | --- | --- |
| v1.0 交付版 | 2026-10-15 | 三份设计文档合订；确立 OpenAPI 与统一 IR Schema 为前后端单一事实源 |
| v1.1 优化版 | 2026-10-15 | 核验许可证与推理能力；补齐来源锚点、任务恢复、索引发布、ACL、事件契约和安全边界；收敛首期范围并统一阶段验收 |
| v1.2 实施细化版 | 2026-10-15 | 命名 owli-parser；uv 与集中配置；保留多租户并简化用户；应用 Token 授权；模块边界、URL 导入、Local/S3/阿里云 OSS、模型配置、前端与生成验收 |

## 文档导读

| 部分 | 文档 | 回答的问题 |
| --- | --- | --- |
| 第一部分 | 总体设计方案 v2.2 | 是什么、为什么——架构、插件框架、支持范围、多平台策略 |
| 第二部分 | 后端设计文档 v1.2 | 怎么实现——数据模型、状态机、IR、索引、权限与 API；许可证和里程碑唯一维护位置 |
| 第三部分 | 前端设计文档 v1.4 | 怎么用——分级预览、来源定位、分块解释和交互边界 |
| 第四部分 | 优化实施补充规范 | 怎么验收——不变量、发布协议、契约细节、容量测算、证据与待确认假设 |
| 第五部分 | AI 工程生成实施规范 | 怎么完整生成——模块、uv、配置、用户/Token、URL、多存储、多模型、接口与测试 |

本文仍是设计基线，不是已经生成的工程。前四部分说明产品和架构，第五部分落实代码与交付约束；不得只按概览生成空壳。已确认要求：owli-parser、uv、配置不硬编码、多租户、轻量用户管理、外部应用 Token、URL 文件解析、多存储及大模型配置。其他数值为配置初值，不是实测承诺。

## 核心设计基线（速览）

- **契约先行**：版本化 IR + OpenAPI + 独立事件 Schema；统一来源锚点覆盖页坐标、文本偏移、表格区域、媒体时间与附件。
- **执行可靠性**：任务至少一次投递，阶段幂等、租约与 fencing token 保证提交安全；PG 是状态权威，队列只负责唤醒。
- **插件边界**：区分优先降级、顺序增强和实验分流；配置不可变快照，插件代码通过发布流程安装，禁止上传目录自动执行。
- **权限与发布**：保留多租户隔离；用户采用固定角色与数据集权限，外部应用使用租户绑定 Token、scope 与资源授权；撤权先在权威层生效，禁止旧索引泄露。
- **配置与工程**：Python 统一使用 uv；阈值、限额、地址、模型、存储映射集中配置，业务代码只消费类型化快照，密钥独立托管。
- **统一接入**：本地上传、HTTP(S) 文件 URL、已配置存储对象先生成受权 artifact，再进入同一解析链路；URL 导入不是网页爬虫。
- **渐进部署**：首期模块化单体 + 独立 worker；Linux Compose 为生产基线，原生模式能力降级，K8s 后置。
- **可解释但不伪精确**：置信度允许未知；检索展示通道排名及 RRF 贡献，不将原始分数相加或称为正确率。

## 优化评审结论与优先级

总体判断：插件化、IR 和双向溯源值得保留；原稿是较完整的产品蓝图，但契约、可靠性与安全要求不足以直接支撑企业级交付。首期价值应是“文档上传 → 可恢复解析 → 可定位结果 → 分块 → 受控检索”，而不是同时交付所有格式、所有推理后端和所有部署形态。

| 优先级 | 原方案问题 | 影响与优化方向 |
| --- | --- | --- |
| P0 | “全部宽松协议、法务零风险”与实际依赖不符 | extract-msg、MinIO、Redis、模型权重分别审查；默认包与条件许可扩展分离，见后端 §17 |
| P0 | 同文件幂等键未覆盖数据集、文档和解析配置 | 不同业务文档可能错误共用任务；拆分请求幂等、任务身份和内容缓存 |
| P0 | PARTIAL_DONE 跳过规范化，成功分母使用未知 block 数 | 以预检确定的页/时间段等工作单元统计，全部成功路径必须规范化与索引发布 |
| P0 | IR 缺少坐标约定、跨格式锚点，前后示例不一致 | 引入 version_id、pages、source_refs、acl_ref、typed payload；未映射位置不伪造 bbox |
| P0 | PG、对象存储、向量库之间没有发布协议 | outbox + 幂等索引 + 可见版本指针 + 查询权威校验，避免半成品上线和撤权窗口 |
| P0 | “内部使用”被当作安全豁免 | 解析沙箱、解压配额、预览净化、SSRF 防护、文件外发审批与凭据隔离必须先行 |
| P1 | image 配置的降级链不能实现 OCR 后接 VLM | `first_success`、`sequence` 和实验组分开建模；错误分类后决定重试或降级 |
| P1 | faster-whisper CoreML、fastText 音频检测等能力假设错误 | CPU/CUDA 与 Metal/CoreML 分开；音频使用声学语言检测，文本才用 fastText |
| P1 | learned sparse 与 BM25 混为一谈，全量 multi-vector 成本未估算 | 默认 dense + BM25；sparse、late interaction 通过离线增益/资源门槛后启用 |
| P1 | “表格不切行”与“每块 ≤300 token”冲突 | 表格保留结构，按 token 预算作行组视图；完整表与检索表示分离 |
| P1 | OpenAPI 被视为涵盖 WebSocket/SSE 全部语义 | 事件 Schema、重连游标、版本字段和鉴权单独定义并加入 CI |
| P1 | 三套里程碑不一致，性能数字缺少测量条件 | 统一 M0–M5 验收；以固定数据集、硬件、模型版本和 P50/P95 实测替代承诺 |
| P2 | 首期同时要求全类型预览、媒体、画布、A/B 和 K8s | 分级支持，先做 PDF/Office/文本闭环；增强项独立验收 |

“P0”表示在企业试点前必须解决；“P1”表示扩展或规模运行前必须解决；“P2”按业务收益排期。合规结论是工程风险识别，不替代法律意见。

---


# 第一部分 · 总体设计方案（v2.2）

## 企业知识库文件解析服务 · 设计方案 v2.2（自研编排 · 分级部署 · 插件化解析框架）

> 适用：Linux (x86/ARM, CUDA) / Windows (原生+WSL2) / macOS (Apple Silicon)

### 0. 设计目标与约束

| 维度 | 要求 |
| --- | --- |
| 语言 | 中文、英文为主，100+ 语言为辅 |
| 文档格式 | PDF、DOCX、PPTX、XLSX、HTML、MD、TXT、EML、MSG，旧版 .doc/.ppt |
| 图片 | JPG/PNG/TIFF/BMP/WebP（扫描件、截图、图表、流程图） |
| 音频 | WAV/MP3/M4A/FLAC/OGG |
| 视频 | MP4/MOV/MKV/AVI/WebM |
| 部署平台 | Linux、Windows、macOS 三平台 |
| 硬性约束 | 默认依赖遵循商用合规准入；离线模式禁止公网、内网访问须批准；CPU/GPU 分离；可度量、可恢复、可运维 |
| 扩展性 | 解析器插件化、流水线声明式配置、支持外部接口、热更新 |

### 1. 总体架构（分层）

1. 接入层（FastAPI：REST/上传/URL 导入/WebSocket/SSE；用户 Session + 外部应用 Bearer Token，多租户鉴权）
2. 调度层（Valkey + Celery；PG 状态机、outbox、幂等重试、租约、死信、租户/应用配额、能力路由）
3. **解析器插件框架**：Adapter 注册表 · 声明式流水线配置中心 · 本地/远程/复合三类适配器 · 受控降级 · 显式实验分流
4. CPU 快通道池（Docling 流水线、Office 解析）
5. GPU 精度池（PaddleOCR；MinerU 为许可与版本准入后的可选扩展）
6. 媒体处理池（ffmpeg、ASR 双引擎、Qwen2.5-VL）
7. 统一 IR 规范化层（blocks + source_refs + provenance + 可空置信度）
8. 后处理（文本语言检测、精确去重与近重复提示、质量报告）
9. 分块（结构/token 预算、表格行组检索表示、父子双粒度、ACL 绑定）
10. 输出层（Markdown+JSON→StorageBackend；OpenSearch→BM25；Qdrant→向量；PG 元数据与发布指针；人工复核；Prometheus，Grafana 为许可审查后的可选看板）

首期采用模块化单体，API 与 worker 分进程；embedding/LLM 使用独立推理进程或服务。scheduler 与 RAG 先作为内部模块，吞吐或故障隔离数据证明有必要后再独立部署，不同时维护 ES/OpenSearch 与 Qdrant/Milvus 两套产品实现。

### 2. 解析器插件框架（本版核心设计）

#### 2.1 统一适配器接口

本地与远程解析器遵循后端 §5.1 的唯一接口定义。Adapter 输出 `ParseResult`（blocks、单位级错误、覆盖率及来源），规范化层统一生成 `DocumentIR`；不允许各 adapter 自行决定租户身份、ACL 和业务版本。

外部返回由版本化 mapper 转换。只有已支持的协议和 mapper 可纯配置接入；新鉴权、异步回调或新结果协议仍需开发与契约测试，不承诺任意服务“零代码接入”。

#### 2.2 两类基础适配器 + 复合适配器

| 类型 | 内置实现 | 说明 |
| --- | --- | --- |
| LocalAdapter | docling-local、office-local、paddleocr-local；mineru/funasr/whisper/vlm 按阶段与许可启用 | 优先直接包装成熟引擎；仅共享模型服务走按能力拆分的后端接口，不强迫 Docling/MinerU 替换内部推理实现 |
| RemoteAdapter | 对已实现协议提供 HTTP 配置；mineru-remote/docling-serve 等须独立准入与 mapper 测试 | 受控端点、熔断、限流与按租户计费；远程执行不一定等于对公网外发，按信任域区分 |
| CompositeAdapter | asr-router（语言路由）、video-composite（ffmpeg→ASR→关键帧→VLM→时间窗对齐） | 多步管线封装为单一 adapter，对调度层暴露为单任务 |

#### 2.3 声明式流水线配置（pipeline.yaml）

```yaml
schema_version: "1.0"
name: default
revision: 1
pipelines:
  pdf:
    mode: first_success
    candidates:
      - adapter: docling-local
        when: { text_layer_ratio: { gte: 0.9 } }
      - adapter: ocr-pdf-composite
  image:
    mode: sequence
    steps:
      - adapter: paddleocr-local
        required: true
      - adapter: vlm-local
        required: false
        when: { needs_visual_description: true }
  audio:
    mode: first_success
    candidates:
      - adapter: asr-router
  video:
    mode: first_success
    candidates:
      - adapter: video-composite
```

这是分阶段能力齐备后的配置示例；首期只发布已安装、通过能力与许可校验的分支。`ocr-pdf-composite` 使用 PDF 渲染 + PaddleOCR；MinerU 可插入复杂 PDF 分支。远程 adapter 通过受控 `endpoint_ref`、`secret_ref` 引用接入；候选链受 endpoint 信任域限制。`local-only` 禁止 remote，`offline` 禁止公网但可使用已批准的内网自托管服务，`hybrid` 仅允许获批外部服务；“remote”不等于“公网”。

调度语义：
- `first_success` 是优先降级；只有可降级错误或未达到质量门槛才试下一项；损坏/加密输入、权限或策略拒绝不得绕过限制继续外发。
- `sequence` 是顺序增强；必需步骤失败使阶段失败，可选步骤失败记录 warning，不覆盖此前已成功结果；预处理步骤传递类型化中间产物。
- `when` 使用带类型的运算符白名单，禁止 eval；缺失特征记为 unknown，条件不命中，并记录原因。
- A/B 是显式 `experiment_id` 下的候选组，不与降级列表混用；稳定 hash 加版本化 salt 分桶，shadow 对照另计成本且不发布。
- 热更新 = 校验后发布不可变配置快照，仅影响新任务；既有任务钉住 effective_config_digest。配置改变不会自动遍历全库重解析，需显式建立重处理批次。

#### 2.4 插件生命周期
- 注册：受信任包的 Python entry_points + 配置引用；插件只能由管理员通过部署流程安装并锁定版本，禁止扫描上传目录加载代码
- 健康检查：定时 ping（本地查进程/显存，远端调 health 接口）
- 可观测：按 adapter 维度的成功率/延迟/成本看板，外部接口费用按租户核算

### 3. 关键设计决策

#### 3.1 默认流水线（开箱配置）

- 数字版 PDF → 预检字体、文本覆盖、乱码率和页级特征，再选 docling-local；文字覆盖阈值 0.9 仅为待校准启发式，不是质量保证。
- 扫描件 → ocr-pdf-composite；复杂版式可选经准入的 MinerU；远端不是无 GPU 时的强制兜底，必须通过外发政策与成本限额检查。
- 混排 PDF → 逐页分流，按阅读序合并
- DOCX→docling/office-local；旧版 .doc/.ppt→隔离的 LibreOffice 转换；PPTX 以 slide 为结构单位，检索块按预算拆分；XLSX 保留 sheet、单元格区域与合并关系。
- 图片：OCR 后按需使用 VLM 描述；JSON Schema 约束结构而不保证事实正确，OCR 交叉核验也不能消除幻觉。保留视觉来源、派生标记和不确定性，不覆盖原始 OCR 文本。
- 音频：用户语言提示 → ASR 声学语言检测/短片段探测 → 中文优先 FunASR，混合/其他语言走 Whisper；fastText 只分析转写后的文本，不直接检测波形语言。
- 视频：video-composite（字幕轨优先直抽 > ASR；PySceneDetect+感知哈希抽关键帧 → VLM 描述，按时间窗对齐）

#### 3.2 统一 IR Schema（核心契约）

唯一规范见后端 §7；总体设计不再维护另一套字段示例。业务身份、不可变版本、页几何、来源锚点、结构关系、置信度来源和 ACL 引用均为核心契约。前端类型和校验器由同一 Schema 生成；API 错误和实时事件另有明确契约。

#### 3.3 任务状态机（统一摘要）

PENDING → ROUTING → PARSING → ENRICHING → NORMALIZING → CHUNKING → INDEXING → PUBLISHING → DONE / PARTIAL_DONE；失败、等待重试与取消走独立分支，详见后端 §4。

- job_id 为独立 UUID；请求幂等、业务文档身份与内容缓存分开设计。
- 三级缓存必须带租户域、引擎/模型版本及有效配置摘要；内容相同不代表权限相同。
- 租户资源配额、带租约的执行槽位、持久化排队与入口背压共同约束负载。

#### 3.4 分块与检索衔接
- 先结构与 token 预算，再按需启用语义断点；完整表格作为结构化产物保留，检索表示可按行组切分；父块展开仍受 ACL 和上下文预算约束。
- BGE-M3 默认不加 query 指令前缀；默认 dense + BM25，learned sparse 与 multi-vector 是不同的可选增强，不与 BM25 混称。
- tenant_id / dataset_id / ACL 随索引写入；所有召回通道前置过滤，返回内容前再次校验权威权限与可见版本。

### 4. 多平台部署

#### 4.1 三种形态
- A Docker Compose：Linux 为首个生产支持平台；按 base / retrieval / gpu / media 分 profile，只有通过合规准入的组件进入默认包。Windows WSL2 是 Linux 运行环境，另核对 Docker Desktop 使用条款。
- B 原生开发：API/worker 通过 uv 运行，依然连接 PostgreSQL + Valkey，保留相同多租户与状态机；依赖服务可由 Compose 启动。SQLite/内嵌执行器仅为 M5 可选单机简化包，首期不维护第二套持久化实现。Mac 的 faster-whisper 默认 CPU；whisper.cpp/Metal/CoreML 和视觉模型按平台另行验收。
- C K8s：在队列、恢复、发布协议稳定后实施；GPU 资源声明与独立 worker 池，HPA 同时参考积压工作量、最老任务等待时间与资源水位。

原生 Windows 不承诺 Celery worker 支持；首期使用 WSL2，内嵌执行器须待可选单机包验收。离线模式失败时保留任务或明确报不支持，禁止静默调用外部服务。

#### 4.2 推理后端抽象（跨平台关键）
按 OCR / ASR / Embedding / Rerank / Vision / Generation 拆分能力接口，后端只实现自身支持的能力；远程文件解析与远程模型推理是不同 adapter，禁止混成一个必须实现所有方法的大接口。
节点上报 os、arch、device、可用内存/显存、模型 revision、量化方式、最大输入和 readiness；路由同时满足能力、版本、许可、外发政策与成本预算，不能只判断“有 GPU”。

#### 4.3 平台能力矩阵
以下为候选运行路径，不是本项目已经完成的兼容性认证；平台、架构、模型与依赖组合逐项验收，未测不得标记“已支持”。

| 组件 | Linux+CUDA | Linux CPU | Win原生 | Win WSL2 | macOS |
| --- | --- | --- | --- | --- | --- |
| Docling/Office解析 | CPU 路径 | CPU 路径 | 原生单独验收 | Linux 路径 | CPU 路径 |
| MinerU | 可选，锁定模型与运行时 | 依后端与资源验证 | 不列默认支持 | GPU 透传需验证 | 不列默认支持 |
| PaddleOCR | 对应 CUDA 构建 | CPU 路径 | wheel/模型兼容验证 | Linux 路径 | CPU，架构兼容验证 |
| FunASR | 按模型验证 | 按模型验证 | 不列默认支持 | Linux 路径 | 不列默认支持 |
| faster-whisper / CTranslate2 | ✅CUDA | ✅ | ✅CPU/CUDA，需验收 | ✅CUDA，需验收 | ✅CPU；不按 CoreML 设计 |
| Qwen2.5-VL | vLLM，锁定模型组合 | llama.cpp，需实测资源 | llama.cpp，需验收 | Linux 后端，需验收 | llama.cpp/Metal，需验证视觉模型支持 |
| ffmpeg/PySceneDetect | ✅ | ✅ | ✅ | ✅ | ✅ |

### 5. 协议合规策略
许可证清单统一维护于后端 §17，证据见第四部分 §8。默认包优先宽松代码许可；模型权重、系统二进制、前端资源和外部服务条款独立审核。GPL/AGPL 不等于禁止商用，也不等于无条件适合闭源交付；附加商业限制不能直接称为标准 Apache-2.0。

### 6. 性能设计
分层缓存、CPU/GPU 分池、动态批处理、模型按预算驻留、页区间并行和受控跨页合并；以端到端延迟、峰值资源和单位成本实测选择参数，不以模型宣传吞吐代替系统吞吐。

### 7. 质量保障闭环
Golden Dataset（~300 篇人工标注）回归、低置信度 block 人工复核回流、badcase 归因看板（adapter×格式×语言）、A/B 分流持续对比

### 8. 实施路线图
统一采用后端 §18 的 M0–M5 与退出条件；前端同阶段验收。原有多套按周排期取消，待团队规模、目标硬件和格式样本确定后估算工期。

---

# 第二部分 · 后端设计文档（v1.2）

## 企业知识库文件解析服务 · 后端设计文档


### 0. 设计目标与约束

| 维度 | 要求 |
| --- | --- |
| 语言 | 中英为主，100+ 语言为辅 |
| 格式 | PDF/DOCX/PPTX/XLSX/HTML/MD/TXT/EML/MSG/旧版 Office + 图片/音频/视频 + 可扩展（CSV/JSON/压缩包/IPYNB/EPUB/DXF…） |
| 多模态 | 图中文字、图表语义、音视频转写 + 关键帧描述 |
| 部署 | Linux(x86/ARM, CUDA) / Windows(原生+WSL2) / macOS(Apple Silicon)；Docker Compose / 单机原生 / K8s 三形态；离线可跑 |
| 扩展性 | 解析器插件化 + pipeline 声明式配置 + 外部接口适配 + 热更新 |
| 约束 | 分层合规准入、离线禁止公网、内网受控、快慢分离、可恢复执行、IR/事件契约版本化 |

### 1. 总体架构

#### 1.1 逻辑分层

```
接入层 ──► 调度层 ──► 解析器插件框架 ──► 执行层 ──► 统一IR ──► 后处理/分块 ──► 索引层 ──► 检索/对话
  FastAPI   Celery      Adapter注册表       Worker        Pydantic      分块引擎      检索索引     RAG模块
           +Valkey     +配置快照           CPU/GPU池      Schema        质量评分      BGE-M3
               └──── PG状态/租约/outbox ──── 不可变产物 ───── 索引确认与可见版本发布 ────┘
```

#### 1.2 部署单元（可合并部署，也可按池拆分）

| 单元 | 职责 | 资源画像 |
| --- | --- | --- |
| api-server | REST/WebSocket/SSE 接入、鉴权、任务登记 | 无状态，2C4G 起，可水平扩 |
| scheduler 模块 | PG 租约、outbox 派发、状态机恢复、路由与配额；首期与控制进程合并部署 | 以积压、锁竞争与恢复耗时实测 |
| worker-cpu | Docling/Office/文本类 adapter、后处理、分块 | CPU 密集，按吞吐扩 |
| worker-gpu | 已准入的 OCR/VLM/ASR/复杂 PDF adapter，按模型分池 | 按权重、KV cache、图像分辨率、批大小实测，不承诺单张 24GB 卡共驻全部模型 |
| embedding-svc | 默认 dense；sparse/multi-vector 按配置启用 | CPU 可运行，吞吐与延迟按 token 长度实测 |
| rag 模块 | 检索、重排、引用与生成编排；首期属于 API 内部模块 | 模型推理外置，达到独立扩容条件后再拆服务 |

### 2. 核心数据模型（PostgreSQL）

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| users / user_sessions | id, username, password_hash, status, is_platform_admin, auth_revision / user_id, token_digest, expires_at | 本地账号与可撤销 Session；不做公开注册、部门树或自定义角色 |
| tenants / memberships | id, name, status, quota_ref / tenant_id, user_id, role, status, auth_revision | 多租户；角色固定 admin/editor/viewer，成员关系唯一 |
| datasets | id, tenant_id, name, owner_user_id, visibility, auth_revision, chunk_policy, storage_profile_id, model_binding_revision | visibility 为 tenant/private；文档和块默认继承，不建设通用 ACL 规则引擎 |
| applications / application_tokens / application_grants | tenant_id, name, status, allowed_scopes / token_id, digest, key_version, expires_at, revoked_at / dataset_id, actions | 外部应用作为独立主体；Token 属于一个应用与租户，凭据不可读回 |
| config_revisions / secret_refs | tenant_id, kind, resource_id, revision, validated_payload / provider, reference, key_version | 普通配置不可变版本；密钥只存受控引用或密文，权限独立 |
| documents | id, dataset_id, tenant_id, filename, mime, size, content_hash, visible_version_id, desired_version_id, acl_revision, deleted_at | 业务身份与文件内容去重分离；新候选版本不得立即覆盖可见版本 |
| doc_versions | id, tenant_id, document_id, version_no, source_artifact_id, ir_artifact_id, lifecycle, effective_config_digest, quality_report | 不可变解析版本；另存 chunk_policy、模型与 tokenizer revision |
| parsing_jobs | id(UUID), tenant_id, actor_kind, actor_id, credential_id, document_id, version_id, state, attempt, stage, execution_plan, lease_owner, lease_until, fence, progress, error | PG 为任务权威，状态提交使用 CAS |
| job_events | tenant_id, job_id, seq, schema_version, event_type, from_state, to_state, ts, detail | `(job_id, seq)` 唯一；脱敏事件供重连补发与审计 |
| chunks | id, tenant_id, dataset_id, version_id, doc_id, current_revision, parent_id, ordinal, acl_ref, enabled, embedding_state | 逻辑块及当前 revision 指针；父块与检索子块有明确 kind |
| chunk_revisions | chunk_id, revision, text, token_count, source_refs, explanation, created_by, created_at | 不可变正文与来源；`(chunk_id, revision)` 唯一，保留历史引用和人工修订 |
| adapters | name, version, mode(local/remote/composite), capabilities, config_ref, health, heartbeat_at | 注册表镜像；实例带心跳与 readiness，业务操作不可只依赖内存状态 |
| pipelines | name, yaml_text, version, active, tenant_scope | 配置版本化，支持灰度 |
| review_items | id, tenant_id, version_id, block_id, revision, reason, status, reviewer, resolution | 复核对象需绑定版本；处置使用乐观锁 |
| job_steps / job_attempts | job_id, stage, unit_id, attempt, checkpoint_ref, deadline, error_code | 页/时间窗级续跑和 adapter 降级记录 |
| outbox_events / index_manifests | aggregate_id, revision, event_type, delivery_state / version_id, index_generation, expected_ids_digest, acknowledgements | 事务内登记异步动作，确认必需索引完整后发布 |
| artifacts / artifact_locations / uploads | tenant_id, content_hash, preferred_location_id, retention / storage_profile_revision, bucket, object_key, object_version / actor_kind, actor_id, part_manifest | artifact 为逻辑身份，location 为物理位置；分片、引用计数与过期回收 |
| source_imports | tenant_id, actor_kind, actor_id, dataset_id, source_kind, source_secret_ref, state, bytes_received, artifact_id, parsing_job_id, execution_snapshot | URL/对象接入任务独立于解析任务；来源 URL 脱敏，下载有租约/checkpoint |
| source_locations | tenant_id, dataset_id, profile_revision_id, bucket, inbox_prefix, enabled | 经管理员批准的对象导入源；不得与受管原件/IR 私有前缀重叠 |
| storage_profiles / model_connections / model_profiles / model_bindings | tenant_id, name, revision, enabled, config_ref, secret_ref | 存储实例、模型连接、模型能力和用途绑定；版本化且隔离租户 |
| audit_logs / idempotency_records | actor_kind, actor_id, action, resource, trace_id / tenant_id, actor_kind, actor_id, operation, key, request_hash, response_ref, expires_at | 审计与请求幂等不复用 parsing_jobs 的主键 |
| golden_sets / golden_runs | cases(jsonb), expectations / run_id, adapter_ref, metrics(jsonb), diff_report_uri | 回归体系 |
| chat_runs / chat_events / evidence_snapshots | tenant_id, actor_kind, actor_id, credential_id, run_id, state / seq, payload / source_version, chunk_revision, acl_refs | M4 引入；事件重放与固定引用映射，不从可变 chunk 正文重建历史答案 |

索引与约束：`idempotency_records(tenant_id, actor_kind, actor_id, operation, key)` 唯一；`doc_versions(document_id, version_no)` 与 `parsing_jobs(version_id)` 唯一；同一版本的重试推进 attempt，不创建并行发布任务；chunks 建 `(tenant_id, version_id, ordinal, id)` 索引；内容 hash 使用租户作用域的普通索引，不作为业务文档全局唯一键。users/user_sessions 属于全局用户身份，tenants 的 id 即租户身份；除这些表、平台级审计和明确的平台配置外，租户资源 tenant_id 非空，复合外键防止跨租户挂接；平台模板不可包含租户密钥。首期采用强制 TenantContext 的仓储与跨租户测试，RLS 是后续防线，不代替应用授权。Qdrant 存向量，PG 存当前正文、权限与可见版本。

### 3. 接入层设计

- FastAPI 应用工厂：router → application service → domain → ports/adapters；依赖注入传递不可变 `ActorContext + TenantContext`。用户使用服务端 Session；外部应用使用 Bearer Token。用户选租户后校验有效 membership，应用租户由 Token 绑定，不能信任请求体 tenant_id；OIDC/SSO 后置。详见第五部分 §4。
- API 规范：`/v1` 前缀；统一错误体 `{code, message, detail, trace_id}`；分页用 cursor（`?cursor=&limit=`），不用 offset（深翻页在 chunk 列表会爆炸）
- 上传协议：
  - 小文件使用 multipart 直传；边界读取 `upload.direct_max_bytes`（初始 100 MiB），不是业务代码常量
  - 大文件分片：POST `/v1/uploads/init` → PUT `/v1/uploads/{id}/parts/{part_no}` → POST `/v1/uploads/{id}/complete`；校验所有权、分片清单、总大小、校验和与有效期。底层按 StorageBackend 能力映射 S3 multipart、阿里云 OSS 原生分片或本地临时文件，part_no 为 1 基应用编号。
  - **内容复用**：只在调用者已获授权的租户/业务范围复用已验证对象；客户端 hash 仅是提示，不可凭 hash 返回其他文档或授予访问权。完成后服务端流式计算 SHA-256；业务文档和解析版本仍独立。
- URL/存储对象导入：POST `/v1/imports` 持久化接入任务，服务端安全流式拉取并校验 artifact，完成后幂等登记解析任务；下载状态与解析状态分离，见第五部分 §5。
- 实时协议：
  - 任务进度：WebSocket `/v1/ws/jobs/{id}`，事件包含 schema_version、job_id、seq、attempt、state、stage_progress、completed_units、total_units、ts；快照和事件分离，断线按 last_seq 补发，历史过期则重新获取快照。
  - 对话：SSE `POST /v1/chat`，使用 fetch 流读取，事件为 `meta → delta/citation → done|error`；浏览器原生 EventSource 不支持此 POST 契约。认证、取消、超时、去重和事件保留详见第四部分 §4。

### 4. 调度层设计

#### 4.1 任务状态机

```
PENDING → ROUTING → PARSING → ENRICHING → NORMALIZING → CHUNKING → INDEXING → PUBLISHING
                                                                            └→ DONE / PARTIAL_DONE
运行阶段 ──暂时性错误──→ RETRY_WAIT ──新 attempt──→ 从最后已提交 checkpoint 续跑
运行阶段 ──不可恢复错误──→ FAILED；可重试错误耗尽预算──→ DEAD_LETTER
非终态 ──取消请求──→ CANCELLING ──释放资源并阻止发布──→ CANCELLED
```

- `PARTIAL_DONE` 只在发布成功后产生。coverage 为成功工作单元/预检确认的总工作单元，PDF 按页、媒体按时长加权；不得以解析后才知道的 block 数为分母。
- 默认不自动发布部分版本；数据集显式启用 `allow_partial` 后，coverage ≥0.6 且规范化、来源、权限和索引门槛通过才允许发布；0.6 是候选阈值，缺失页/时间窗必须展示。分母未知时不自动部分发布。
- 阶段 deadline 按页数、时长和 profile 设置；30min 只能是某个 profile 的初始上限。区分排队超时、执行超时、无心跳超时与外部请求超时。
- PG 租约、heartbeat、单调 fence 和 CAS 保护状态推进；旧 worker 即使超时后继续返回也不得提交或发布。checkpoint 只引用校验通过的不可变产物，租约回收后从单位级 checkpoint 续跑。

#### 4.2 队列拓扑（Valkey + Celery，兼容性需锁版验收）

| 队列 | 消费者 | 说明 |
| --- | --- | --- |
| q.route | scheduler | 轻量，路由决策（读配置、体检） |
| q.cpu | worker-cpu ×N | 数字文档/文本类任务 |
| q.gpu | worker-gpu ×N | 扫描件/媒体任务 |
| q.post | worker-cpu | 后处理/分块/索引（GPU 任务完成后流转） |
| q.dlq | 人工 | 死信 |

- 提交任务时冻结配置/模型 revision，URL 导入沿用创建时快照；ROUTING 只读取该快照、结合预检编译候选顺序并复查当前政策，不重新读取最新业务默认值。实际节点按执行时 readiness/租约选择。GPU→CPU/remote 降级需要重新派发到匹配队列，不能让不具备能力的 worker 原地执行。
- 队列语义为至少一次：晚确认、适配长任务的 visibility timeout、限制 prefetch，并以 PG checkpoint/fence 去重。PG 事务写 job 与 outbox，派发器补偿消息丢失；broker 不是状态数据库。
- **租户配额**：上传字节、队列工作量、CPU/GPU 槽位、外部费用分别限制；槽位带租约和原子续期，worker 崩溃后可回收。公平轮转避免大租户独占；API 429 + Retry-After，已接收任务不丢弃。

#### 4.3 幂等与重试

- 请求幂等键由调用者提供，以 tenant + actor_kind + actor_id + operation 为作用域，持久化请求摘要；同 key 同请求返回原资源，同 key 异请求返回 409。重放前仍检查当前权限，不把旧响应缓存当作访问凭据。上传摘要来自已校验的 upload/artifact，不以客户端声明 hash 作证据。
- 执行身份 = document_id + version_id + effective_config_digest；摘要覆盖 pipeline、adapter/模型 revision、解析参数、chunk policy 和 tokenizer。跨数据集同内容仅可复用隔离后的解析缓存，不共用业务版本/ACL。
- 网络/限流等暂时错误按指数退避 + jitter，尊重 Retry-After；参数、加密、损坏输入、权限/政策错误不重试。adapter 内重试、降级次数、任务 attempt 共享总时长和费用预算，避免嵌套重试放大。
- 外部超时可能已经产生费用；协议支持时传远端幂等键或查询远端 job，不能将“本地没收到响应”等同于“远端没执行”。

#### 4.4 长文档拆分

PDF 按预检页清单生成工作单元，首期采用受内存预算约束的页区间；区间大小通过样本测量，不固定为 200 页。边界可重叠 1–2 页作上下文，按原始页与来源身份去重；阅读顺序在局部解析后统一合并。跨页表只有在列结构、表头和连续性证据充分时合并，否则保留关联片段并进入复核。重试只处理失败区间，不修改已提交成功产物。

### 5. 解析器插件框架（核心）

#### 5.1 Adapter 接口

```python
class ParserAdapter(Protocol):
    name: str
    version: str
    mode: Literal["local", "remote", "composite"]

    def supports(self, ctx: ParseContext) -> SupportDecision: ...
    def parse(self, artifact: ArtifactRef, options: BaseModel,
              execution: ExecutionContext) -> ParseResult: ...
    def capabilities(self) -> Capabilities: ...
    def health(self) -> HealthStatus: ...
```

这是接口草图；完整类型由实现期 Schema 定义。`SupportDecision` 含不支持原因；`ExecutionContext` 含 deadline、cancel token、受控 artifact access、checkpoint 与进度回调。重 CPU/GPU 同步调用运行在 worker 子进程，禁止阻塞 FastAPI 事件循环。

生命周期为注册/预热/readiness/排空/关闭；ParseResult 含 blocks、unit_errors、coverage 与实际模型版本，mapper 也必须版本化。插件不得从输入文档决定文件路径、租户或 ACL；不能使用不受信任 pickle 作为产物交换格式。

#### 5.2 三类适配器

| 类型 | 要点 |
| --- | --- |
| LocalAdapter | 包装成熟解析器，保留其原生推理路径；可共享模型才使用对应能力后端；预热与驻留受模型预算控制 |
| RemoteAdapter | 支持版本化同步/异步协议 mapper；endpoint/secret 受控引用、并发/速率/成本限额、明确 deadline。熔断按 endpoint + model + credential_scope 的滚动窗口统计暂时性故障；4xx 输入/鉴权失败不混入服务健康率，半开只放有限探针 |
| CompositeAdapter | 受限子步骤编排；DAG 编译时检查环、最大深度与资源/类型兼容。每步有状态、产物和错误；取消与预算向下传播，不用无限递归执行或持有 GPU 槽位等待其他 GPU 子任务 |

#### 5.3 Pipeline 配置模型（YAML → Pydantic 校验）

配置形状以总体 §2.3 为准，编译流程为：safe YAML 加载 → Pydantic 类型校验 → 引用/环/类型/资源检查 → 租户、许可与外发政策检查 → 生成不可变 execution plan 与摘要。

- `supports ∧ when ∧ policy ∧ readiness` 决定可执行性；结构在编译期固定，依赖前序输出的条件在对应步骤执行前评估并记录特征快照，不因 ROUTING 时特征缺失永久删除后续增强步骤。配置大小、嵌套与 alias 数量受限，secret 值不进入数据库快照、前端响应和日志。
- 实验分桶使用指定稳定哈希算法与 `(tenant_id, doc_id, experiment_id, salt_revision)`，禁止使用进程随机化的 Python `hash()`。A/B 是灰度策略；质量对照另跑同一 golden case 的 shadow job。
- 文件 watch 只加载已校验版本，PUT 使用 If-Match 防止覆盖；任务钉住摘要，回滚只影响新任务。模型升级改变配置摘要；全库重处理需显式提交有配额和可取消的批次。

#### 5.4 注册与发现

通过受信任发行包的 `owli_parser.parsers` entry_points 注册，发行包、传递依赖和模型 revision 锁定；禁止运行期安装用户提供的插件。注册表首期是持久化镜像 + 心跳 TTL，不单独建设服务发现系统；实例过期不可派单，恢复后重新通过 readiness。

### 6. 推理后端抽象

按接口隔离定义 `OcrBackend`、`AsrBackend`、`EmbeddingBackend`、`RerankBackend`、`VisionBackend` 和 `GenerationBackend`；每类都有 capabilities、model_revision、deadline 与 cancel 语义。生成接口显式提供 `generate/stream`，embedding 显式声明输出 dense/sparse/multivector 哪些表示，不能假设所有运行时都支持三种表示。

| 实现 | 环境 | 用途 |
| --- | --- | --- |
| VllmBackend | 首期验证 Linux + CUDA | 已被对应版本支持的 LLM/VLM；不默认假设任意 FunASR 模型可直接运行 |
| LlamaCppBackend | CPU / Metal / 经验证的其他后端 | 支持的 GGUF 模型；VLM 需匹配视觉编码器、投影文件和运行时版本 |
| CTranslate2Backend | CPU / NVIDIA CUDA | faster-whisper；macOS 默认 CPU，不提供 CoreML 假设 |
| WhisperCppBackend | 可选，macOS 原生 | Metal；支持的模型可用 CoreML 编码器，独立打包与验收 |
| PaddleOcrBackend / FunAsrBackend | 各自官方运行时支持的平台 | 两套独立依赖/进程，不将 FunASR 归入 Paddle 后端 |
| TransformersBackend | 经验证的平台/模型组合 | 兼容性路径，不保证任意模型无成本降级 |

- 节点启动向 Registry 上报 NodeCapabilities；调度路由 = `任务需求 ∩ 节点能力 ∩ adapter 候选链`。
- 模型配置区分连接、能力模型和用途绑定；OpenAI-compatible 可接入获批云服务与自托管端点，但 rerank、vision、usage 等能力必须单独探测；配置与切换协议见第五部分 §7。
- 模型包独立于应用镜像，锁定 revision、SHA-256、tokenizer、量化与许可证；离线包通过只读卷加载，禁止运行期隐式下载。模型升级采用新 worker 预热 → readiness → 切流 → 旧 worker 排空，不承诺有限显存下无成本热加载。

### 7. 统一 IR（Pydantic Schema）

下列为契约字段草图；引用类型与跨字段校验在实现期提供 JSON Schema、Pydantic 模型及合法/非法 fixture，不把草图当作完整可执行代码。

```python
class Block(BaseModel):
    id: str
    type: Literal["text", "heading", "table", "formula", "image",
                  "figure_desc", "audio_seg", "video_frame", "list",
                  "page_header", "page_footer", "footnote", "code"]
    order: int = Field(ge=0)
    parent_id: str | None = None
    text: str
    lang: str | None = None
    source_refs: list[SourceRef] = Field(min_length=1)
    payload: BlockPayload
    confidence: Confidence | None = None
    provenance: Provenance
    acl_ref: str

class DocumentIR(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    doc_id: str
    version_id: str
    dataset_id: str
    tenant_id: str
    content_hash: str
    effective_config_digest: str
    langs: list[str] = Field(default_factory=list)
    pages: list[PageGeometry] = Field(default_factory=list)
    blocks: list[Block]
    coverage: CoverageReport
    quality: QualityReport
```

- `SourceRef` 为 page_region / text_span / table_region / media_span / attachment / artifact 的判别联合，详见第四部分 §2；bbox 规范为原页面视觉方向的左上原点、右下方向、0–1 归一化，page_index 从 0 开始，UI 页码另算。
- `PageGeometry` 保存尺寸、旋转、crop_box 与坐标变换；DOCX 结构锚点和转换 PDF 坐标之间需 mapping，不可直接复用 bbox。纯媒体/表格可无 pages。
- `BlockPayload` 按 type 校验表格单元格与跨度、公式 LaTeX、媒体时间/说话人等；扩展字段必须 namespaced、有限大小，不以任意 extra 承载核心契约。
- `Confidence` 保存 raw_score、normalized_score、method 和 calibration_revision；未校准得分不能跨 adapter 比较，缺失不是 1.0。质量报告保留 coverage、乱码率、空块率等独立指标，综合分须说明权重与缺失值处理。
- `acl_ref` 首期引用数据集授权，文档/block 继承，拒绝无法表达的外部细粒度权限。未来启用更细 ACL 时只可收紧，合并取权限交集；IR 历史快照不得替代读取时的最新授权。
- 本次把原 1.0 草图的顶层 bbox/page 改为 source_refs，按破坏性升级定义 IR 2.0，与文档 v1.2、pipeline/事件版本独立；本文尚无已发布 IR 消费方，不把设计草图变更伪称线上迁移。若已有 1.0 消费方需兼容导出或迁移器，不可静默升级。
- 产物保存为不可变 artifact；对象键包含 tenant/doc/version/artifact_id 或内容摘要，`ir.json` 仅为逻辑名称，重试和迁移不得覆盖同一键。稳定 block_id 仅承诺同版本内，跨重解析依靠来源映射，不假设 ID 永久相同。

### 8. 后处理与分块引擎

#### 8.1 后处理

1. **语言检测**：对已抽取文本使用 fastText 或已准入替代方案；短文本/混合语言可返回 unknown/mixed，不强制归类；OCR 前可使用用户提示或轻量探测，ASR 前采用声学检测。
2. **去重**：精确 hash 用于受控缓存；simhash 仅提示近重复，不直接跳过解析。差异页复用需页面渲染摘要、渲染器版本和上下文一致，跨页结构仍重新合并。
3. **质量评分**：按单位 coverage、文本完整性、结构完整性、坐标有效性与经校准置信度分别报告；阈值按 adapter×格式标定。缺失评分、无法定位或关键字段异常也可进入复核，不用固定 0.8 统管所有模型。

#### 8.2 分块引擎

- **结构优先**：heading、段落、列表、表格、代码、slide、sheet 作为边界；heading_path 与 source_refs 必须保留。
- **token 预算**：锁定 tokenizer；文本初始目标 300–500、硬上限 800、相邻上下文重叠 50 token，均需数据集评测。标题与复制表头计入预算；父块初始上限 2000 token，生成时还要按 LLM 实际 tokenizer 校验。
- **语义切分**：首期非必需；只对结构切分后的超长段落按需计算句向量。阈值通过验证集标定，不把 0.55 当通用标准；持久化断点及算法版本，前端没有数据时不绘制虚构曲线。
- **表格策略**：完整表格独立保存；检索表示按 token 预算分行组并复制表头。单行超预算按单元格/文本片段拆表示，仍引用原始区域并标明截断，不静默丢列。
- **父子与多模态**：父块去重后按权限与预算展开；figure_desc 带派生声明和原图 artifact_ref；ASR、关键帧与字幕保留各自来源及时间基准，低置信度 VLM 描述不能冒充原文。
- **人工干预**：修改创建 chunk revision，If-Match 控制并发；受影响向量与词法索引都重建。禁用立即在权威层生效，重索引完成前不得用旧向量搭配新正文；合并只允许同文档版本且权限兼容的相邻块。

### 9. 检索与对话服务

#### 9.1 混合检索管线

```
query ──► 身份/数据集/权限过滤条件 + query 规范化 + BGE-M3 dense
      ──► [Qdrant dense] ──────────┐
      ──► [OpenSearch BM25] ───────┼──► RRF ──► 权威版本/ACL校验 ──► rerank ──► TopK
      ──► [可选 learned sparse] ──┘                                    │
                     可选 multi-vector 仅对有界候选集 late interaction；不足则低相关警告
```

- BGE-M3 默认 query/document 均不额外加检索指令；锁定模型 revision、归一化和 query 预处理版本，不与其他 BGE 模型的用法混用。
- 默认 dense + BM25。learned sparse 使用 BGE-M3 token 权重和 Qdrant sparse 索引，不能用 BM25 冒充；multi-vector 默认关闭，启用时仅重排有界候选，按需关闭该字段 HNSW（m=0）以控制成本。
- 每通道初始召回 50，RRF 候选最多 100，rerank 后默认 TopK=10；均从 retrieval 配置读取。`RRF(d)=Σ w_i/(rrf_k+rank_i(d))`，rrf_k 初始配置为 60，排名从 1 开始，未命中通道贡献为 0。
- 所有通道先过滤 tenant/dataset/ACL，再校验 PG 中的 visible_version、chunk revision、enabled、deleted 与当前权限；无权/陈旧候选不进入 reranker、LLM 或日志。候选不足可在预算内补取，不足则降级返回。
- 返回每通道 raw_score、rank、rrf_contribution、rerank_score、模型版本及 applied_filters；不把不同量纲 raw_score 堆叠，也不把相关性分称为正确概率。空召回和阈值使用经验证的策略处理。

#### 9.2 对话编排

- 检索授权候选 → 父块权限校验/去重 → 按生成模型 tokenizer 分配上下文预算 → 固定 evidence_id 引用映射 → GenerationBackend 流式生成。检索文档作为不可信数据，文档内指令不得改变系统权限或触发工具操作。
- 引用映射在生成前固定，绑定 doc_id/version_id/chunk_id/revision/source_refs；解析模型输出标记时只接受白名单内 evidence_id，未知引用标为无效。历史引用绑定原版本，读取时仍检查当前权限，过期或删除展示“来源不可用”。
- 零证据、低相关或索引暂不可用时明确返回无法基于知识库回答；允许一般性回答需单独产品策略，不默认为有引用结论。
- LLM 裁判默认用于离线回归或抽样异步分析，不阻塞每次在线问答、不冒充人工正确率。外部 LLM 同样受文档外发政策控制。

### 10. 存储设计

| 存储 | 用途 | 要点 |
| --- | --- | --- |
| StorageBackend | 原件 / IR / MD / 预览 / 媒体 | 内置 LocalStorage、S3Storage、AliyunOssStorage，允许多 profile 同时存在；按数据集选新写入目标，旧对象按原位置读取。阿里云 OSS 使用原生 SDK，不以 S3 兼容替代；MinIO 仍为许可准入后的 S3 服务选项，细则见第五部分 §6 |
| PostgreSQL | 元数据、任务、ACL、outbox、版本与索引发布清单 | 唯一约束、CAS、事务与迁移；是业务状态权威，不将 Celery result 当作事实源 |
| Valkey | Celery broker、配额和可重建缓存 | BSD-3-Clause；锁定客户端/Celery 兼容组合，验证 Lua、恢复与可见性超时；不得用易丢缓存替代持久状态 |
| OpenSearch | BM25，retrieval profile 启用 | 锁定分词器与词典版本；中文先建立可复现基线，IK 作为单独兼容性/许可审查插件 |
| Qdrant | dense；可选 sparse/multivector | payload 至少含 tenant_id、dataset_id、version_id、chunk_revision、acl_revision；索引字段与删除同步，原正文取权威版本 |

### 11. 多平台部署

#### 11.1 三种形态

| 形态 | 适用 | 降级替换 |
| --- | --- | --- |
| A Docker Compose | Linux 生产基线；Windows WSL2 单独验收 | base / retrieval / gpu / media；普通配置与密钥分离，secret 不打入镜像 |
| B 原生开发 | uv 运行 API/worker、Mac 原生推理 | 与生产共用 PG + Valkey、多租户及迁移；SQLite/内嵌队列不进入首期，避免重复实现可靠性协议 |
| C K8s | 规模生产，M5 后按需求引入 | 独立模型池、持久卷、资源配额、扩缩容与备份恢复演练 |

#### 11.2 跨平台推理路由

- 能力路由同时满足运行时、模型、内存、许可与数据政策；remote 是明确授权的执行地点，不是无 GPU 的隐式默认值。
- CPU/GPU/媒体镜像分开构建并锁定依赖；x86_64、ARM64、CUDA 与原生系统分别验收。业务配置模型相同不代表“同一镜像”覆盖全部平台。

#### 11.3 离线交付

应用镜像、模型包与 wheelhouse 分开版本化交付；附 SHA-256 清单、SBOM、模型许可、系统二进制构建参数和离线健康检查。以断网环境验证冷启动、预览资源、模型加载和故障路径；禁用遥测、外部字体/CDN与隐式自动下载。

### 12. API 契约（与前端文档 §6 对齐 · 补充）

- REST 以受版本控制的 OpenAPI 构建快照为准；IR/事件 JSON Schema 与 fixture 同步版本化。CI 做破坏性变更检查、生成 TS 类型并验证请求响应，不从不可控的运行中服务临时抓类型。
- 错误体 `{code, message, detail, trace_id}`；400/422 输入校验、401 身份、403/404 资源策略、409 同幂等键异请求、412 If-Match 失败、413 超限、429 背压、503 能力暂不可用。同键同请求重放原状态与资源，不返回幂等冲突。
- 上传完成获得 artifact_id；POST `/v1/parsing/jobs` 接受 artifact_id、dataset_id、可选 document_id 和 pipeline_ref；更新已有文档要求预期 document revision，防止并发覆盖；小文件 multipart 只是同一流程的快捷入口。
- IR/预览/chunks 必须支持显式 version_id；默认解析为当前 visible_version。预览返回 status、strategy、artifact_id、mapping_id、expires_at、range_supported，转换未完成返回 202 和任务引用，而非总是假定存在 URL。
- 完整 IR 用于受权导出；交互端使用分页 `/v1/documents/{id}/blocks?version_id=...&cursor=...`、页面索引和子树接口，不要求一次下载/反序列化整个千页 IR。
- 列表使用绑定过滤条件与稳定排序 `(created_at,id)` 或 `(ordinal,id)` 的不透明 cursor。创建/动作接口支持 Idempotency-Key；修改/删除另用 If-Match/revision，幂等不能代替并发控制。

### 13. 性能与容量设计

| 手段 | 设计 |
| --- | --- |
| 三级缓存 | 租户/许可域 + 原件/页/输入摘要 + adapter/模型 revision + 生效配置摘要；缓存命中后重新挂接业务版本和当前权限 |
| 批量推理 | 按总 token、图像分辨率、音频时长与峰值内存动态组批；设排队上限，不能固定 batch=128 适用于所有模型 |
| 快慢分离 | 小任务/大任务资源预算与公平调度；GPU 每模型并发受硬预算控制，过载不盲目重试 |
| 容量基线 | M0 用目标硬件实测冷/热启动、P50/P95、峰值 RSS/VRAM、吞吐与失败率；原稿的秒/页、页/分、CPU 文本/秒数据缺少条件，撤回为待验证假设 |
| 大文件 | 流式校验上传、页级调度；媒体阶段按需 HLS，避免所有视频先强制完整转码；存储与索引测算见第四部分 §6 |

### 14. 可观测性

- 指标（Prometheus）：任务吞吐/耗时/成功率、外部 adapter 延迟/失败率/费用、GPU 显存利用率、队列深度、embedding 延迟
- 追踪：OpenTelemetry，trace 贯穿 API→队列→adapter→IR→分块→索引（排查"某文档某 chunk 为什么这样"一键拉全链）
- 日志：结构化 JSON，trace_id 贯穿；adapter 级 debug 开关（按任务动态开）

### 15. 质量保障闭环

- golden runner 固定案例、adapter/模型/配置和渲染版本，比较文本、阅读顺序、表格、公式、位置及检索指标；按格式/语言/难度分层，标注集与留出评测集分开。
- 回归门槛预先定义最小有效样本与允许退化，使用配对对比并报告区间；不能把任何浮点波动都当发布阻断。权限泄露、来源错位与状态机不变量失败为零容忍。
- 复核创建带来源的修订产物，不原地覆盖原始 IR；重跑页重新走规范化与发布。badcase 经授权、脱敏和标注复审后入库，不能直接把线上修正自动混入固定留出集。

### 16. 权限、解析安全与审计

- 保留租户隔离，简化用户权限：membership 固定 admin/editor/viewer；数据集 tenant/private，文档和 block 继承。外部应用额外检查 Token scope 与显式数据集 grant，不继承签发者权限。平台运维不自动拥有租户正文权限。首期不实现部门、自定义角色和 block ACL 编辑器；`acl_ref` 指向数据集授权引用，详细授权矩阵见第五部分 §4。
- 所有 REST、WebSocket、下载、预览、引用、复核和对话分享独立鉴权。ACL 撤销先提交权威数据库并递增 revision，再异步更新各索引；检索返回前复查，缓存按授权 revision 失效。
- 首期拒绝导入无法表达的更细 ACL，不能静默放宽权限。未来启用更严格 block ACL 时，整文件原件、整页截图、完整 IR、父块和预览都可能包含受限内容。默认只有满足全部内容权限的用户可读取这些聚合产物；其他用户只能看过滤后的块，或使用另行验证的脱敏派生产物，不能仅隐藏 bbox 就开放整页。
- 解析进程无特权、只读输入、限 CPU/RAM/时长/文件数、默认无网络；Office 宏不执行，独立临时目录，禁 shell 拼接。限制解压层数、膨胀比、总字节，拒绝路径穿越与外部实体。
- RemoteAdapter、webhook 和转换器做 SSRF 防护：批准 endpoint、校验解析后的目标地址及重定向、阻止元数据/回环等危险地址；合法内网服务须显式授权。外发按租户/数据集、地域、保留策略审批，凭据放 secret store。
- HTML/SVG/Markdown/邮件/表格 HTML 均视为不可信内容，白名单净化、CSP、URL 协议限制与隔离预览；原文件使用安全 Content-Disposition。恶意软件扫描可按场景加，但“内部使用”不能豁免上述边界。
- audit_logs 记录 actor、资源版本、动作、理由和 trace_id，避免记录正文、JWT、密钥、预签名 URL；明确留存期限与导出权限。

### 17. 协议合规清单

审查对象是**具体发行版本 + 传递依赖 + 模型权重 + 二进制构建 + 使用/分发方式**，不是项目名称。以下是设计时核验，发布时仍需重新核对锁定产物；证据见第四部分 §8。

| 对象 | 核验与风险 | 交付策略 |
| --- | --- | --- |
| Docling、Office Python 库、FastAPI、Celery、Qdrant、OpenSearch 等 | 代码多为宽松许可，但模型、插件及传递依赖另计 | 可进入候选默认包，逐版本生成 SBOM 与 NOTICE |
| extract-msg | 官方仓库为 GPL-3.0，不是 MIT/BSD/Apache | 从默认进程内依赖移除；MSG 先做信息/下载兜底，解析扩展经法务准入；独立进程不自动免除义务 |
| MinIO | AGPLv3 | StorageBackend 可选实现；部署/修改/分发方式单独评估，不声称其禁止商用或完全无义务 |
| Redis | ≤7.2 为 BSD-3；7.4 系列为 RSALv2/SSPLv1；8+ 增加 AGPLv3 选项 | 默认评估 Valkey BSD-3-Clause 并做 Celery 兼容验证，不能笼统写“Redis 宽松协议” |
| MinerU | 本次官方主分支许可为 Apache-2.0 加附加条款；关联方合并 MAU>1亿或月收入>2000万美元需额外许可，向第三方提供服务还要求显著标识 | 可选扩展；固定代码 tag/commit 与权重清单，不把旧版本或主分支的许可外推到全部版本 |
| Qwen2.5-VL | 7B 官方 model card 标记 Apache-2.0；72B 标记 Qwen License，并非同一许可 | 默认只评估已核验的具体模型；72B 作为条件许可扩展，另核对全文条款与资源需求 |
| fastText lid.176 权重 | 官方语言识别权重为 CC-BY-SA-3.0，与库的代码许可证分开 | 单列模型归属、署名与分发义务；不归入“全部 MIT” |
| PaddleOCR/FunASR/Whisper/BGE 系列 | 代码与不同模型可能有不同许可；reranker 必须指定模型 ID/revision | 逐个 model card 与 LICENSE 准入，不按品牌推断全部权重 |
| FFmpeg / PyAV | FFmpeg 默认 LGPL，但启用 GPL/nonfree 组件改变许可/可分发性；PyAV wheel 关联二进制也须审查 | 锁定构建参数和编解码器，附对应源码/告知材料；独立进程不等于无需合规 |
| LibreOffice / Grafana / Docker Desktop | LibreOffice 及其依赖分别履约；Grafana 包含 AGPL 产品，Docker Desktop 有独立使用条款 | 作为已准入可选依赖或运维环境，不能遗漏于“全宽松”承诺 |

AGPL/GPL 可商用，但合规义务不同；带商业附加条款的软件不自动符合“标准宽松开源”定义。默认包、可选 copyleft 包与条件许可模型分开交付；法务签字、来源链接、版本、摘要、义务清单和模型再分发权限是发布门禁。

### 18. 统一实施路线图与退出条件

| 阶段 | 范围 | 前后端联合验收门槛 |
| --- | --- | --- |
| M0 契约与可行性 | 模块边界、uv/锁文件、配置 Schema、IR/source refs、租户/Token 权限、存储/模型端口、许可证与目标硬件 PoC | 契约 fixture、模块依赖检查通过；锁定工具/引擎；URL SSRF 和多存储风险验证；明确所有必需配置 |
| M1 最小解析闭环 | 多租户/轻量成员/应用 Token；上传与 URL/对象导入；Local/S3/阿里云 OSS；模型连接配置；TXT/MD、数字 PDF、DOCX/PPTX/XLSX；前端设置与工作台 | 三种存储共用契约并有各自集成验证；外部 Token 端到端调用成功；跨租户与 scope 越权失败；URL 安全、幂等、崩溃恢复、IR 定位通过；无语义检索承诺 |
| M2 解析质量 | 扫描/混排 PDF、图片 OCR、表格来源、Office 转 PDF、低置信复核；基础质量报告 | OCR/表格/坐标分层指标达基线；缺页可见；部分发布受控；复核版本可追溯；旧版 Office 仅在转换验收通过后启用 |
| M3 分块与受控检索 | 结构/token 分块、dense+BM25、rerank、人工编辑、双索引发布与召回测试台 | 每块可追源；撤权/禁用/删除无泄露；编辑与索引一致；Recall/nDCG 达留出集基线；Linux 离线企业试点可交付 |
| M4 媒体与问答 | ASR、视频关键帧、可选 VLM、GenerationBackend、SSE、固定引用、合规 RemoteAdapter | 声画时间轴对齐；伪引用拒绝；取消/断流可恢复；离线不访问公网；外发政策与费用预算生效 |
| M5 扩展与运营 | Pipeline 可视化、实验、扩展格式、跨平台能力包、容量调优；按需求 K8s | 升级/回滚/备份恢复演练；平台与格式逐项兼容测试；A/B 增益与成本报告；离线模型包及合规材料齐备 |

安全、审计、基础观测、回归与配置版本化从 M0/M1 贯穿，不延后到最后。无团队人数、目标吞吐和标注投入时不承诺固定周数；M3 为首个受控检索试点交付点，后续阶段独立评估预算。

---

# 第三部分 · 前端设计文档（v1.4）

## 企业知识库解析服务 · 前端设计文档


### 0. 设计原则

1. **双向溯源是第一公民**：通过带版本的 source_refs 定位页区域、结构节点、表格范围或媒体时间；无法精确定位时显式显示降级原因，不把文本近似匹配伪装为精确引用。
2. **可解释性**：切块为什么这么切、检索为什么召回它，都要可视化解释
3. **可干预**：解析和切块结果支持人工查看、编辑、调整（参考 RAGFlow 切片可视化+手动调整）
4. **运营友好**：上传、进度、失败重试、质量看板面向非技术同事设计

### 1. 信息架构（页面结构）

```
┌─ 租户切换器 ── 仅列出本人有效 membership，切换清理旧数据
├─ 知识库 (Datasets) ── 列表 / 新建 / tenant-private 可见性 / 解析与存储设置
├─ 文档中心 (Documents)
│   ├─ 列表页：上传 / URL 导入 / 已授权存储对象导入、状态、重试、版本
│   └─ 文档工作台 ★核心页面
│       ├─ 原文预览 (Preview)
│       ├─ 解析结果 (Blocks)：IR 可视化
│       ├─ 分块视图 (Chunks)：切块可视化+解释 ★
│       └─ 版本历史 (Versions)
├─ 解析配置中心 (Pipeline Studio)
│   ├─ Adapter 注册表（健康状态、能力、成本）
│   ├─ Pipeline 编排（YAML 编辑 + 表单视图 + A/B 分流配置）
│   └─ 运行日志 / 降级记录
├─ 召回测试台 (Retrieval Lab) ★
├─ 对话工作台 (Chat)：引用溯源、多模态回答
├─ 质量中心 (Quality)
│   ├─ Golden Dataset 回归报告
│   ├─ 人工复核队列（低置信度 block 审核）
│   └─ Badcase 归因看板
├─ 任务中心 (Tasks)：上传/导入/解析分阶段进度、失败与取消
├─ 设置 (Settings)
│   ├─ 模型：连接 / 模型能力 / 用途绑定 / 测试
│   ├─ 存储：Local / S3 / 阿里云 OSS，多连接与默认绑定
│   ├─ 应用与 API：应用 / Token / scope / 数据集授权 / 调用记录
│   └─ 成员：本租户成员与 admin/editor/viewer，避免复杂组织管理
└─ 系统状态 (Ops)：本租户任务与能力；节点/平台设置仅平台管理员可见
```

### 2. 核心页面设计

#### 2.1 文档工作台 · 原文预览（双栏联动）

**布局**：左侧原文预览器 ｜ 右侧解析结果（Markdown / IR 树），滚动联动。

| 格式 | 预览方案 | 层级 |
|---|---|---|
| PDF | pdf.js 渲染，bbox 高亮叠加层 | L1 原生 |
| DOCX/PPTX/旧版 Office | DOCX 可用净化后的 mammoth HTML；PPTX 首期按 slide 结构预览；M2 启用服务端转 PDF，位置映射独立产出；WPS 不默认承诺 | L2/L3 |
| XLSX/CSV | 小文件可用 SheetJS；TanStack Table + Virtual 负责展示，合并单元格需独立区域覆盖层或展开视图；大表走后端分页，不默认再引入另一套 grid | L3 结构化 |
| EPUB/电子书 | 服务端转 HTML 后阅读视图（章节导航） | L2 转换 |
| Markdown/RST/Tex | Markdown 用 react-markdown；KaTeX 只处理支持的数学表达式，不能渲染完整 TeX 文档；RST/完整 TeX 首期展示源码，转换为后续扩展 | L1/L4 |
| 代码/日志/文本 | Shiki 语法高亮（VS Code 主题，懒加载语言包） | L1 原生 |
| JSON/YAML/XML | 结构化树视图（可折叠）+ 原文切换 | L1 原生 |
| 压缩包 ZIP/RAR/7Z | ZIP 可在大小/层数配额内展开；jszip 不负责 RAR/7Z，这两类需独立后端插件与许可核验；归档目录与内部文件分别鉴权 | L3/L4 |
| Jupyter .ipynb | 按 cell 渲染：Markdown cell 走 MD 渲染，代码 cell 走 Shiki，输出区渲染文本/图片 | L3 结构化 |
| 邮件 EML/MSG | EML 在扩展阶段展示净化正文与授权附件；MSG 默认信息/下载兜底，获得解析插件许可准入后再启用 | L3/L4 |
| 图纸 DXF / Visio | 服务端转 SVG/PNG → 图片预览 + 图层说明 | L2 转换 |
| DICOM | 影像本体挂 PACS 链接，报告文本走 MD 视图 | L2 转换 |
| 图片 | 原图 + OCR 文本框叠加 | L1 原生 |
| SVG/PSD | SVG 仅净化后隔离呈现或栅格化；PSD 转 PNG 属于单独验证的扩展，不注入未经处理的 SVG DOM | L1/L2 |
| 音频 | 波形播放器（wavesurfer.js）+ 转写文本时间轴联动，点击文本跳音频位置 | L1 原生 |
| 视频 | 播放器 + 字幕轨 + 关键帧时间轴（点击关键帧跳对应画面） | L1 原生 |
| 未识别类型 | **四级兜底**：文件信息卡 + 原始文本/十六进制查看 + 下载 | L4 兜底 |

#### 2.1.1 全类型预览的四级兜底策略 ★

以下是完整产品的能力目录，不代表首期支持清单。按 MIME 预检、后端能力和安全策略选适用策略；L1/L2/L3 并非所有格式必经的串行流水线，失败最多退到 L4。L4 只保证授权用户看见元信息与明确状态，不承诺任意文件正文都可显示。

- **L1 原生渲染**：浏览器可呈现的图片/PDF/文本等；仍依赖授权资源、兼容编解码器与完整性校验，不承诺统一“秒开”。
- **L2 转换渲染**：仅调用已准入且通过样本验收的转换插件；未启用 EPUB/DXF/PSD/OFD 等转换时退回 L4。异步状态区分 pending/ready/failed/unsupported，允许重试并展示原因。
- **L3 结构化渲染**：无固定"版式"的数据类文件（XLSX/CSV/ZIP/IPYNB/EML），从 IR 或浏览器端解析产物直接生成结构化视图——这类视图天然与分块/引用联动（表格行、压缩包内文件、邮件附件都是可溯源对象）
- **L4 通用兜底**：图标 + 元信息卡（类型/大小/哈希/解析状态/质量分）+ 原始文本或 hex 查看器 + 下载按钮

**预览路由器**：前端消费后端 capabilities 与 preview descriptor，再选择已安装 renderer；后端是能力事实源，不能仅凭扩展名决定安全策略。新增格式必须有解析/预览 fixture、来源映射、资源限额和失败回退测试，不承诺只改两行配置。

预览状态、解析状态、索引状态分别展示；“可以预览”不代表“已解析”，“已解析”不代表“已发布可检索”。缓存键至少包含 tenant、doc、version 和 artifact，切换租户/退出登录必须清空。

**Block 可视化模式**：开启后按类型着色叠加 bbox 框——
- 文本/标题/列表：蓝/绿/青色；表格：橙色；公式：紫色；图片：黄色；页眉页脚：灰色虚线
- block 角标仅对已校准分显示配置化等级；未知分显示“未评估”，悬浮展示来源/方法。不能只靠颜色表达状态，复核入口同时支持文本标签与键盘操作。
- 点击 block → 右侧 IR 树定位到对应节点；反之点击 IR 节点 → 原文高亮滚动定位

#### 2.2 分块视图 · 切块可视化与解释 ★本页是差异化核心

**三级视图**：

```
① 结构树视图                ② Chunk 列表                ③ Chunk 详情
├─ 第一章                    ┌────────────────────┐      标题路径：第一章 > 1.2 节
│  ├─ 1.1 节 ──► chunk#1    │ chunk#1 · 312 tok   │      文本：[分块正文…]
│  └─ 1.2 节 ──► chunk#2    │ chunk#2 · 287 tok   │      ├─ 来源：第3页 b-0031~b-0044
├─ 第二章                    │ chunk#3 · 401 tok ⚠ │      ├─ 相似度断点曲线（ECharts）
│   └─ …          chunk#3    │ …                  │      ├─ 父块：第一章（权限与预算内）
└─ 附录表格 ──► chunk#9      └────────────────────┘      ├─ 索引：dense✓ BM25✓ sparse未启用
                                                          ├─ 权限：继承数据集 / tenant 或 private
                                                          └─ 操作：编辑 / 拆分 / 合并 / 禁用
```

**"为什么这样切"解释面板**（每个 chunk 可展开）：
- 切块依据：标题路径、结构切分点（标题树节点）、语义二次切分点
- 断点解释：展示结构/token/人工/语义等实际原因；仅在后端产生真实句间相似度数据时展示曲线，并标注算法版本与阈值。
- Token 构成条：正文 / 表格 / 公式 占比
- 父子关系图：子块挂父块的层级缩略图

**手动干预**（借鉴 RAGFlow 切片手动调整）：
- 编辑 chunk 文本、调整边界（拖动结构树节点）、合并相邻块、禁用误切块
- 修改后展示 revision、保存中/待索引/已发布/失败；后台同时更新受影响的词法与向量索引。遇到 412 保留本地草稿并提供差异比较，禁止静默覆盖他人修改。

#### 2.3 召回测试台（Retrieval Lab）

- 输入 query → 展示召回 chunk 列表，每条显示：
  - 已启用通道的 raw_score、rank 和 RRF 贡献；堆叠图仅展示可相加的 RRF 贡献，未启用通道标记“未启用”而非 0 分。
  - rerank 分与向量相似分分开标量展示；LLM 裁判仅为可选异步评估，并标记非人工结论。
  - 低相关性警告：所有召回低于阈值时顶部黄条警示（借鉴 Kotaemon 警告系统）
- 参数实验：调整 TopK/阈值/权重后显式“运行”，携带配置快照；限制频率与费用，结果按相同样本比较，不让每次滑杆移动触发推理。
- 点击任意召回 chunk → 跳转文档工作台并高亮定位

#### 2.4 对话工作台（Chat）

- 流式输出（SSE）；答案中引用标记 [1][2][3]
- **引用卡片**：hover 显示原文快照 + 页码 + 置信度 + 通道贡献（借鉴 RAGFlow 引用快照与追根溯源）
- 点击引用 → 打开文档工作台弹窗，pdf.js 定位到对应页并高亮该 chunk（借鉴 Kotaemon 浏览器内 PDF 查看器高亮+相关分数）
- 多模态回答：图片通过受权资源访问，引用卡片带版本与来源；未知引用或来源失效明确提示，不自动跳到最新版本。
- 会话分享不授予源文档权限。M4 默认关闭分享；启用前需对答案快照采用不弱于全部来源的权限交集，逐次重验，不能只隐藏引用却保留泄露正文的答案。

#### 2.5 质量中心

- **Golden Dataset 回归报告**：每次解析器/配置变更后的准确率对比表（按格式×语言）， diff 视图展示 badcase
- **人工复核队列**：低置信度 block 列表 → 原文定位 → 审核（接受/修正/重跑该页/换 adapter）
- **Badcase 归因看板**：adapter × 格式 × 语言 的失败模式热力图

#### 2.6 解析配置中心（Pipeline Studio）

- Adapter 卡片墙：名称、本地/远程、健康状态灯、支持格式、cost hint、近24h成功率/延迟
- Pipeline 编辑器：双模式——YAML 编辑（高级）/ 表单+可视化拖拽编排（参考 LangFlow 画布，React Flow 实现）
- A/B 实验配置：流量分配滑杆 + 质量对比图表
- 草稿校验 → dry-run 路由解释 → 带 If-Match 的发布；新版本仅影响新任务。回滚生成新的发布记录，不改变在途任务；secret 仅显示引用，不能读回明文。

### 3. 关键技术选型（全开源）

| 层次 | 选型 | 协议 | 说明 |
|---|---|---|---|
| 框架 | React 18 + TypeScript + Vite | MIT | 团队招聘友好 |
| 组件体系 | **shadcn/ui + Radix UI + Tailwind CSS** | MIT | 源码级组件，完全可控，商业化定制自由（详见 4.2） |
| 动效 | Framer Motion | MIT | 商业化微交互 |
| 图标 | lucide-react | ISC | 统一线性图标 |
| 分级预览 | mammoth/SheetJS/jszip/Shiki/react-markdown+KaTeX | BSD/MIT/Apache-2.0，按所选许可履约 | 浏览器端解析与渲染；mammoth 输出需净化（详见 2.1.1） |
| PDF 渲染 | Mozilla pdf.js | Apache-2.0 | 行业标准 |
| 音频波形 | wavesurfer.js | BSD-3 | 转写时间轴联动 |
| 图表 | ECharts | Apache-2.0 | 相似度曲线、看板 |
| 图谱/编排 | React Flow (xyflow) | MIT | Pipeline 画布、父子块关系图 |
| 服务端状态 | TanStack Query | MIT | 缓存、轮询、乐观更新 |
| 实时通信 | WebSocket(进度) + SSE(对话流式) | — | 与 FastAPI 原生契合 |
| API 对接 | openapi-typescript + openapi-fetch | MIT | 固定一种生成链，业务使用封装后的类型化 client |
| 路由/表单 | React Router + React Hook Form + Zod | MIT，锁版核验 | 路由 loader/错误边界、表单状态和本地即时校验；服务端是校验权威 |
| 测试 | Vitest + Testing Library + MSW + Playwright | 逐版本核验 | 组件/交互、契约 mock、真实 API 端到端，不以截图替代功能测试 |

### 4. 前端开源库详细说明


#### 4.1 React 18 + TypeScript + Vite（框架底座）

| 项 | 说明 |
| --- | --- |
| 协议 | React MIT / Vite MIT |
| 选型理由 | React 与 TypeScript 生态适合复杂工作台，Vite 提供开发构建链；M0 锁定 React/Vite/Tailwind/shadcn 兼容组合，不混用不同大版本的配置范式；包体与性能由基准验证 |
| 本项目用法 | 函数组件 + Hooks 全量；`React.lazy` 按路由代码分割（文档工作台独立 chunk，首屏只加载列表页） |
| 备选 | Vue 3（生态同样成熟，团队栈决定）；Svelte（小众，企业招聘成本高，不推荐） |

#### 4.2 shadcn/ui 组件体系与商业化视觉栈（v1.2 变更）

保留 **shadcn/ui + Radix UI + Tailwind**，理由是工作台需要精细控制布局、选区和主题。代价是团队自行承担树、复杂表格、上传和无障碍集成，不应把视觉偏好当成天然性能优势；优先完成任务恢复、定位准确性和可访问性。

##### 4.2.1 shadcn/ui + Radix UI（无头组件基座）

| 项 | 说明 |
| --- | --- |
| 协议 | shadcn/ui MIT / Radix UI MIT |
| 选型理由 | Radix 提供无障碍、受控逻辑完善的无头原语（Dialog/Popover/Tabs/Toast/Dropdown…），shadcn 在其上提供精致的默认样式；组件源码在仓库内，**任意像素都可改**，不像 AntD 要靠 theme hack |
| 本项目映射 | 表格 → TanStack Table（headless）+ shadcn table 样式；树 → react-arborist（4.2.6）；上传 → react-dropzone（4.2.5）；任务状态机 → 自定义 Stepper；Tabs/Dialog/Sheet/Command 直接用 shadcn |
| 备选 | Ant Design 5（组件现成度高但视觉同质化严重，"一眼 AntD"）、MUI（同理） |

##### 4.2.2 Tailwind CSS（样式系统）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 设计 token 即代码：颜色/圆角/阴影/间距集中在 `tailwind.config` 与 CSS 变量，换肤（浅色/深色/品牌色）只改变量 |
| 商业化视觉规范 | ① 中性色阶用 oklch 灰阶（比纯灰高级）② 圆角体系：卡片 12px / 按钮 8px / 标签 6px ③ 阴影分层（rest/hover/overlay）④ 数据密集页用紧凑密度，工作台页用舒适密度 ⑤ Inter + 系统中文黑体字体栈，数字用 tabular-nums 对齐 ⑥ 深/浅色双主题全量覆盖（含图表 ECharts 主题同步） |

##### 4.2.3 Framer Motion（动效）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 声明式动画，布局动画（LayoutGroup）适合分块视图拖拽调整的顺滑反馈 |
| 本项目用法 | 页面转场、抽屉/弹层弹簧曲线、骨架屏 shimmer、数字滚动、上传卡片队列动效；遵循"功能性动效"原则（200ms 内，尊重 prefers-reduced-motion） |

##### 4.2.4 lucide-react + Sonner + cmdk（细节件）

| 库 | 协议 | 用途 |
| --- | --- | --- |
| lucide-react | ISC | 全站统一线性图标（文件类型图标映射表按扩展名） |
| Sonner | MIT | 轻量 Toast（任务完成/失败/复核提醒） |
| cmdk | MIT | Command Palette（⌘K）：全局跳文档/搜 chunk/执行操作——商业化产品的专业感关键件 |

##### 4.2.5 react-dropzone（上传）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 拖拽/粘贴/多文件/文件夹上传的标准方案 |
| 本项目用法 | 上传队列卡片（进度条 + 解析状态机徽章 + 失败重试按钮），配合 WebSocket 实时刷新 |

##### 4.2.6 react-arborist（树组件）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 内建虚拟化的树组件，万级节点 IR 树/文档结构树流畅；支持拖拽（切块手动调整用） |
| 集成约束 | 树使用 react-arborist 自带虚拟化，不再叠加另一层 Virtual；锁定 React/拖拽库兼容版本并验证键盘导航 |

##### 4.2.7 Shiki（语法高亮）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | VS Code 同款引擎，主题统一（深浅色同步），支持懒加载语言包——代码文件/日志/JSON 源码视图全靠它 |

##### 4.2.8 react-markdown + KaTeX（Markdown 渲染）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 解析产物的阅读视图：remark/rehype 插件管线，表格/代码/数学公式（KaTeX）完整支持；与对话流式输出的 Markdown 渲染复用同一组件 |

##### 4.2.9 mammoth.js / SheetJS / jszip（浏览器端解析）

| 库 | 协议 | 用途 |
| --- | --- | --- |
| mammoth.js | BSD-2-Clause，以锁定版本 LICENSE 为准 | DOCX 转 HTML，不保证版式保真；官方明确不净化输入，输出必须安全处理 |
| SheetJS 社区版 | Apache-2.0 | XLSX/CSV 浏览器端解析为二维数组供表格渲染 |
| jszip | MIT/GPLv3 双许可，默认选择 MIT 并保留声明 | 仅处理 ZIP；限制解压大小/数量/深度，大文件走后端受限任务 |

#### 4.3 pdf.js（PDF 渲染与高亮 · 核心库）

| 项 | 说明 |
| --- | --- |
| 协议 | Apache-2.0 |
| 选型理由 | Mozilla 官方，浏览器端 PDF 渲染事实标准，坐标系/文本层 API 完整——高亮与 bbox 叠加的地基；Dify/RAGFlow 等同类产品均基于它 |
| 关键用法 | worker 与主包版本一致；Range/CORS/Content-Range 由资源层配合；canvas 页面层、TextLayer 选择层与来源高亮层分离；按 PageGeometry/mapping 转换归一化坐标并正确处理旋转、裁剪、缩放和 DPR |
| 高亮实现 | 优先直接定位 source_refs，不做全文猜测；缺乏坐标时才做保留数字、公式、代码及多语言字符的文本近似匹配并标注“近似”；等待目标页挂载后滚动，避免无条件依赖 requestIdleCallback |
| 备选 | react-pdf（pdf.js 的 React 封装，上手快但高亮层控制受限）；PDF-LIB（偏编辑生成，非渲染）；本项目高亮需求重，直接裸用 pdf.js |

#### 4.4 wavesurfer.js（音频波形与转写联动）

| 项 | 说明 |
| --- | --- |
| 协议 | BSD-3 |
| 选型理由 | 轻量波形渲染 + regions 插件，天然适合"转写文本 ↔ 音频"双向联动 |
| 关键用法 | regions 使用 media_span 的原始媒体时间；长音频由服务端预计算 peaks 并分段加载，避免浏览器完整解码耗尽内存；说话人字段仅在已启用且通过验证的 diarization 管线产生，未知说话人不强制分配身份 |
| 备选 | howler.js（纯播放，无波形，不满足联动需求）；Web Audio API 手搓（成本高） |

#### 4.5 video.js + hls.js（视频播放）

| 项 | 说明 |
| --- | --- |
| 协议 | video.js Apache-2.0 / hls.js MIT |
| 选型理由 | 统一播放器皮肤与 API（跨浏览器一致性）；字幕轨原生支持；插件生态（热键、播放速率） |
| 关键用法 | 浏览器可播放时先走原始媒体 Range；必要时异步转 HLS，首期采用 video.js 自带播放链，不重复叠加 hls.js 解码栈；manifest、key、segment 都鉴权，转码时间轴须能映射回原始媒体 |
| 备选 | 原生 `<video>` + 自写控制条（简单场景够用，但字幕轨和跨浏览器一致性要自己动手） |

#### 4.6 ECharts（图表与可视化）

| 项 | 说明 |
| --- | --- |
| 协议 | Apache-2.0 |
| 选型理由 | 中文文档与案例最丰富；一个库覆盖本项目全部图表场景 |
| 本项目用法 | 真实断点曲线、各通道 RRF 贡献、badcase 热力图、token 构成与任务指标；按需引入 echarts/core，主题切换同步；未知/缺失/未启用数据单独表示，不填充成 0 |
| 备选 | AntV G2（AntD 体系视觉统一，但学习曲线陡）、D3（自由度最高成本也最高） |

#### 4.7 React Flow（@xyflow/react）（编排画布）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | React 生态最好的节点图库，上手快 |
| 本项目用法 | Pipeline Studio：节点 = adapter 卡片（健康灯/延迟/成本），边 = 降级链与 when 条件标签；小图复用：chunk 父子层级关系图 |
| 备选 | AntV X6（MIT，图编辑引擎更强，适合做复杂图编辑器，但上手成本高）；本项目画布以展示+轻编辑为主，React Flow 足够 |

#### 4.8 TanStack Query（服务端状态）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 替代手写 useEffect 轮询，自动处理竞态、缓存、失效 |
| 本项目用法 | 任务事件优先、快照轮询降级；IR/分块按租户/版本缓存；chunk 编辑保留本地草稿，服务端确认后更新 revision 与索引状态，不乐观宣称已发布 |

#### 4.9 Zustand（客户端状态）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 约 1KB、无 Provider 样板代码 |
| 本项目用法 | 保存轻量 selection：tenant/doc/version/block/source_ref/navigation_seq；正文由 Query 缓存管理。URL 可携带版本与来源锚点，取消过期定位请求，防止快速切文档后高亮到旧内容 |

#### 4.10 TanStack Virtual（虚拟滚动）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 无样式侵入的虚拟化原语，与任意 UI 库组合 |
| 本项目用法 | 分页 chunk/文档列表和表格使用 Virtual；IR 树由 react-arborist 独立虚拟化，不叠加两层；与 PDF 分页懒加载配合 |

#### 4.11 openapi-typescript + openapi-fetch（API 类型层）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 从后端构建产出的 OpenAPI 快照生成 TS 类型，降低静态契约漂移；不能替代运行时响应校验、权限测试及 SSE/WS 事件契约 |
| 本项目用法 | CI 先导出固定 OpenAPI/事件 Schema，再生成客户端并检查未提交差异；使用合法/非法 fixture 做契约测试；接口、缓存与 mock 共用类型 |
| 备选 | orval（可额外生成 React Query hooks，团队需要时可平替） |

#### 4.12 Monaco Editor（YAML/JSON 编辑）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT（VS Code 同款编辑器内核） |
| 选型理由 | 企业运维对 VS Code 交互零学习成本 |
| 本项目用法 | Pipeline Studio 的 YAML 编辑模式：后端下发 JSON Schema 驱动自动补全与校验；IR JSON 只读查看（json 语法高亮 + 折叠） |
| 备选 | CodeMirror 6（MIT，更轻量，对编辑器需求弱时选用） |

#### 4.13 react-i18next（国际化）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 企业可能有海外团队/双语运维需求 |
| 本项目用法 | 中/英语言包，资源按命名空间懒加载；所有面向运营同事的文案走 i18n key |

#### 4.14 dayjs（时间处理）

| 项 | 说明 |
| --- | --- |
| 协议 | MIT |
| 选型理由 | 2KB 的 moment.js 替代品，API 基本一致 |
| 本项目用法 | 任务时间戳、耗时展示、监控轴刻度 |

#### 4.15 协议合规汇总

| 库 | 协议 | 商用 |
| --- | --- | --- |
| React / Vite / shadcn/ui / Radix UI / Tailwind / Framer Motion / Sonner / cmdk / react-dropzone / react-arborist / Shiki / react-markdown / KaTeX / React Flow / TanStack 系 / Zustand / openapi-typescript / Monaco / i18next / dayjs | MIT，逐版本核对 | 履约后可用 |
| mammoth.js | BSD-2-Clause，逐版本核对 | 履约后可用 |
| jszip | 双许可中选择 MIT，保留声明 | 履约后可用 |
| SheetJS 社区版 | Apache-2.0 | ✅ |
| lucide-react | ISC | ✅ |
| pdf.js / video.js / ECharts | Apache-2.0 | ✅ |
| wavesurfer.js | BSD-3 | ✅ |

前端主要候选依赖采用宽松许可，但仍需核对锁定版本、复制的 shadcn 源码、字体、图标、worker 与传递依赖并保留告知材料；本表不构成法律担保，不能据此前端表推断整个系统均为宽松许可。

### 5. 性能设计（前端专项）

优化以固定设备/浏览器基准验证，不保留无可复现条件的“提升 80%”承诺：

1. **PDF**：分页虚拟化、可见页与邻页预取、取消离屏渲染、释放 canvas/worker；设置渲染像素与内存预算。TextLayer 覆盖可见页，保留复制、搜索与无障碍能力。
2. **来源定位**：直接使用来源锚点，近似文本匹配仅兜底；坐标测试覆盖 0/90/180/270 度、裁剪、缩放和 DPR。
3. **结构数据**：服务端分页优先；树和列表按需要虚拟化。Canvas bbox 叠加必须保留可访问的选中项信息，不能只有不可操作的画布。
4. **加载策略**：PDF/Monaco/ECharts/媒体播放器按路由懒加载；大表解析放 worker，限制 JSON/代码高亮字节数；资源随路由卸载清理。
5. **访问链路**：Range、CORS、206、签名过期与刷新均验收；要求及时撤权的文件走鉴权代理，浏览器缓存不绕过权限校验。
6. **交互完整性**：测试键盘操作、焦点恢复、暗色主题、空态/失败态、部分成功、断线、权限变化和版本冲突；组件数量不作为完成度指标。

### 6. 与后端契约（关键 API）

```
GET  /v1/datasets                              数据集列表
POST /v1/parsing/jobs                          基于 artifact_id 提交解析；multipart 为快捷入口
GET  /v1/parsing/jobs/{id}                     权威任务快照；WS 按 seq 推送增量
GET  /v1/documents/{id}/ir?version_id=...       版本化 IR（含 source_refs 与质量报告）
GET  /v1/documents/{id}/preview-url?version_id=...  预览 descriptor 或 202 转换任务
GET  /v1/documents/{id}/chunks?version_id=...  分块/来源/解释；检索得分只在检索响应中
PATCH /v1/chunks/{id}                          人工编辑 chunk
POST /v1/chunks/{id}/reembed                   手动重试索引任务；正常编辑自动触发索引更新
POST /v1/retrieval/test                        召回测试（TopK/阈值/权重参数）
POST /v1/chat (SSE)                            对话（返回引用 chunk_id 列表）
GET  /v1/adapters                              adapter 注册表 + 健康状态
GET /v1/pipelines                            pipeline 列表
GET/PUT /v1/pipelines/{name}                   版本化配置；PUT 要求 If-Match
GET  /v1/quality/regression-runs               回归报告
POST /v1/review/queue/{review_item_id}/resolve  绑定文档版本和 revision 的复核处置
```

### 7. 实施排期

与后端 §18 的 M0–M5 共用里程碑，不再维护独立 F1–F5。M1 包含租户切换、轻量成员、应用 Token、模型/存储设置、URL 导入和文档工作台；M2 加强解析质量，M3 分块/检索，M4 媒体/问答，M5 可视化编排。基础权限、无障碍与异常状态测试贯穿所有阶段。

### 8. 前端工程结构与页面契约

#### 8.1 目录和组件职责

```text
frontend/src/
├── app/                    路由、Providers、登录/租户守卫、错误边界
├── features/
│   ├── auth/               Session 与登录
│   ├── tenants/            成员与租户切换
│   ├── datasets/           数据集管理
│   ├── documents/          文档中心与版本
│   ├── ingestion/          上传、URL/对象导入
│   ├── tasks/              导入/解析任务状态
│   ├── workbench/          预览、来源导航、block/chunk、编辑草稿
│   ├── models/             模型连接、能力和用途绑定
│   ├── storage/            多存储表单和导入位置
│   ├── applications/       应用、Token、scope、授权和调用审计
│   ├── retrieval/          召回测试
│   ├── chat/               对话与引用
│   ├── quality/            质量与复核
│   └── pipelines/          流水线配置
├── components/ui/          shadcn 原语，不依赖业务 feature
├── components/common/      DataTable、状态、确认框、错误/空态
├── renderers/              PDF/文本/表格/图片/媒体与注册表
├── api/                    生成类型、统一 client、Query key 工厂
├── schemas/                表单 Schema、事件/IR 运行时校验
├── stores/                 selection、布局、暂态编辑，不存服务端整库
├── styles/                 design tokens、主题和阅读排版
├── locales/                中文/英文文案命名空间
└── test/                   setup、MSW handlers、fixture
```

Feature 内部使用 pages/components/hooks/api/types 分层，简单功能不强行创建全部目录；跨 feature 依赖公开入口，不互相读取内部 store。`components/ui` 不调用业务 API，页面不直接 fetch 绕过统一 client。

核心组件契约：
- `TenantSwitcher`：只有授权租户选项；确认未保存草稿后切换，取消全部旧租户请求与订阅，重建 capability/session 上下文。
- `ImportDialog`：source 判别类型 + dataset_id + pipeline_ref，成功只返回 import/upload ID；不承担解析执行。
- `TaskProgress`：明确 import/job 类型、attempt、stage、已知/未知总量；不按定时器伪造百分比。
- `DocumentWorkbench`：接收 doc_id/version_id，组合预览与结果，不能在 renderer 内决定可见版本。
- `PreviewRenderer`：输入 preview descriptor、SourceRef 和只读授权资源 client；提供 ready/navigate/dispose，输出 selection/error，不直接读取云密钥。
- `SourceNavigator`：带 navigation_seq 的异步定位；只接受当前 tenant/doc/version，过期定位结果丢弃。
- `SecretInput`：write-only，configured 占位、替换与清除分开，不将掩码当真实值提交。
- `ScopePicker + DatasetGrantPicker`：从后端 scope registry 和可管理数据集取候选，显示最终交集；不支持通配符文本输入。

#### 8.2 路由与操作矩阵

所有租户页面统一置于 `/t/:tenantId`；路径只表达选择，不作为授权证据。路由只读元信息可提前加载，正文等到 Session/租户守卫通过后请求。

| 路由后缀 | 页面/主操作 | 最低权限与完成状态 |
| --- | --- | --- |
| `/datasets`、`/datasets/:id` | 新建/可见性/设置/文档列表 | viewer 可读，editor 创建；过滤、分页、空态可用 |
| `/documents` | 批量上传、URL 导入、筛选、删除 | 读写按数据集授权；批量操作返回逐项结果 |
| `/documents/:id` | `version/tab/page/block/chunk` URL 状态 | 版本化双栏工作台；无效来源明确提示 |
| `/tasks`、`/tasks/:id` | 接入/解析进度、取消、失败恢复 | 不把取消 HTTP 连接当任务取消；动作受权 |
| `/settings/models` | 连接、模型、用途、测试和变更影响 | admin；字段级错误、密钥不可回读 |
| `/settings/storage` | 多 provider、测试、默认/数据集绑定 | admin；旧对象位置与迁移状态可见 |
| `/settings/applications/:id?` | 应用、Token、scope、grant、调用记录 | admin；创建 Token 一次性展示与撤销闭环 |
| `/settings/members` | 列表、添加已有用户、角色/停用 | admin；阻止删最后一个 admin，不设计组织树 |
| `/retrieval`、`/chat` | 有效数据集及模型选择、证据定位 | 按 capability 启用，不显示虚构可用能力 |

平台级租户创建、全局网络准入与用户停用置于独立 `/platform`；平台权限不等价于租户内容权限。租户管理员添加已知账号可按完整用户名提交，不提供全平台用户枚举接口。

### 9. 上传、URL 导入与任务体验

- 上传弹窗分“本地文件 / 文件 URL / 存储对象”三页签；先选择当前有写权限的数据集，再显示 pipeline 和生效存储。普通用户不编辑底层 endpoint。
- URL 批量输入逐行校验，显示获准数量/格式、超限与重复项；批量由客户端有界逐项提交，每项独立幂等键，成功/失败分开展示，不生成一个无限大的请求。
- 网络校验与下载在服务端进行；前端不直接 fetch 外部 URL 来猜可用性。敏感 query 提交后仅保留脱敏显示，不存浏览器历史/localStorage/埋点。
- 本地上传分别展示上传字节、服务端校验、解析/索引状态；暂停只暂停传输，取消需要取消服务端会话。跨刷新恢复先查询已收 parts，再要求用户重新选择原文件；浏览器 File 对象不会凭空持久存在。
- 接入列表保留 import_id/job_id、原始主体、来源类型、脱敏 host、大小、配置版本和错误码。SUBMITTED 后链接到 job，不把下载 100% 当解析 DONE。
- 批量重试/删除显示确认摘要、逐项结果与失败原因；发请求期间禁重复点击但仍依赖服务端幂等。失败不清空用户填写的非敏感字段。
- 任务中心提供状态、来源、格式、创建者、时间筛选和 cursor 翻页；未启用 GPU/LLM/外网能力时展示明确原因和获权设置入口。

### 10. 模型、存储、Token 设置页细节

#### 10.1 模型设置

连接列表展示 provider/protocol、地域、状态、最近测试、已绑定用途；详情分“连接与凭据 / 模型能力 / 生成或嵌入参数 / 用途绑定”。先选择 protocol，再显示其参数 Schema，避免把所有厂商字段混成大表单。

- base_url、model_name、credential、超时/并发与费用预算都有说明、格式错误及服务端错误映射；不预填看似真实的密钥。
- 支持手动模型名和显式“获取模型列表”；“测试”说明将执行哪些请求/是否收费，测试中可取消，结果不覆盖尚未发布的草稿。
- embedding 改绑弹窗展示维度/模型差异、需要重建的数据集和旧索引保留策略；不会只弹“保存成功”却让旧索引不可查。
- 默认 generation/embedding/rerank/vision 分别选择；未实现能力 disabled 并说明阶段，不能在前端假造开关。

#### 10.2 存储设置

Local 显示获准目录池，不让租户填任意磁盘路径；S3/OSS 分别显示实际协议参数。密钥和 STS 状态单列，只能替换/清除。

- 测试划分连接/读/写/Range/分片，写测试需明确提示将创建临时对象；测试对象使用隔离前缀与 TTL 清理，不碰业务对象。
- 默认存储切换提示“只影响新写入”；已绑定数据集、历史引用数、read/write_disabled 状态可见。有引用时删除被阻止并展示原因。
- 迁移作为独立、可取消异步动作；若本阶段未实现则隐藏操作并显示历史对象仍按原位置读取，不能提交空 API 后提示成功。

#### 10.3 外部应用与 Token

应用页包含基本信息、数据集/actions、scope 上限、限额与 Token 列表；列表只显示安全 public_id/名称、过期时间、状态、最近使用、创建者。

- 创建向导：选用途与 scope → 选明确数据集 → 设到期/限额 → 确认有效权限摘要 → 签发；不默认全选。
- 原始 Token 仅成功响应后的专用弹窗可见，支持主动复制，关闭后清除内存；不写 store 持久化、分析埋点、录屏日志或导出列表。提示“关闭后无法再次查看”。
- 轮换并列显示新旧 Token 到期和在途任务；撤销/应用停用需确认影响，成功后立即刷新列表与授权状态，不能只做本地乐观删除。
- 测试调用示例默认使用环境变量占位符；浏览器不把管理员刚签发的外部 Token 自动发送到任意测试 URL。
- 调用记录按时间、接口、状态/拒绝原因、request_id 聚合；限制仅本租户，不展示请求正文、源 URL query 或 Authorization。

### 11. 状态、视觉、响应式与可访问性

#### 11.1 三类状态的归属

| 状态 | 管理方式 | 约束 |
| --- | --- | --- |
| 服务端事实 | TanStack Query，key 工厂含 tenant/resource/version/filter | 不复制到 Zustand；收到事件后按 seq 更新或 invalidate |
| 可分享导航 | React Router search params | 筛选、tab、version、source anchor；不含 Token、密钥或完整导入 URL |
| 暂态交互 | React Hook Form/Zustand/组件 state | 草稿、selection、布局；敏感值不持久化，切租户/登出清理 |

统一 client 处理 CSRF、X-Tenant-ID、trace_id、AbortSignal、错误体和幂等键；401 清会话并导航登录，403 显示无权，404 显示不可用，412 保留草稿并比较服务端版本，429 按 Retry-After，5xx 提供可恢复入口。mutation 不因网络失败自动重放非幂等动作。

事件连接集中管理，不为每一行任务创建一个 socket；首期任务列表使用 GET `/v1/parsing/jobs` 的有界分页快照轮询，详情才订阅单 job WS，import 轮询自身状态接口。相同资源一个订阅，后台 tab 降低频率，断线转快照轮询；事件主版本未知则停止消费并刷新契约。

所有远程页面必须具备：首次 skeleton、空数据、筛选无结果、加载失败、权限不足、能力未启用、部分成功、请求取消、连接重试状态；不能用无穷 loading 替代错误处理。toast 用于补充反馈，关键错误须内联保留。

#### 11.2 工作台与视觉规范

- 统一 AppShell：租户选择 + 侧栏 + 面包屑 + 主操作区；内容区以文件和任务为中心，不做大量无信息装饰卡片。复杂设置采用基础/高级折叠。
- 设计 token 覆盖颜色、字体、间距、边框、圆角、阴影、z-index、密度、动效与断点；在 CSS variables/Tailwind 配置集中维护，业务组件禁止散落十六进制色值和 magic z-index。
- 工作台桌面双栏可拖动、可折叠；中等宽度改 tabs，移动端以单栏查看为主，复杂边界拖拽提供按钮操作或明确只读。断点与最小面板宽度由设计 token 定义。
- 文档 header 固定显示名称、版本、来源、解析/索引状态；预览工具栏包含页码、缩放、适合宽度、旋转和定位状态。结果区有 Blocks/Chunks/Markdown/版本，未支持功能明确禁用。
- 滚动联动使用来源与当前可见锚点，不按两栏滚动百分比强制对齐；用户手动滚动后可暂停联动，避免反馈循环。失效/近似来源显示标签和原因。
- 所有业务状态颜色来自统一映射；颜色之外同时提供图标和文字。深/浅主题覆盖 PDF 周边、表格、Monaco、ECharts，图片/原件不被任意反色。
- WCAG 2.2 AA 为验收目标：键盘完整路径、可见焦点、Dialog 焦点锁定/恢复、表单 label/error、状态 aria-live、对比度、缩放和 reduced-motion。长列表虚拟化仍保留可读的行/选中项语义。
- 正文、文件名、URL、模型输出统一按不可信输入处理；Markdown 禁止任意 raw HTML，图像链接和外链按资源策略处理。用户登出/撤权后关闭预览、释放 object URL/canvas/worker 并清空正文缓存。

### 12. 前端交付与测试门槛

- `pnpm check` 必须执行 TypeScript、ESLint、Vitest 和生产构建；Playwright 验证真实前后端流程。MSW 仅用于开发/测试，生产构建不得带演示数据兜底。
- 必测流程：登录/切租户 → 创建数据集 → 上传或 URL 导入 → 任务恢复 → 查看版本和来源 → 修改 chunk 冲突；模型配置/测试、Local/S3/OSS 表单、应用 Token 签发/撤销各有 E2E。
- 权限 E2E 覆盖两个租户、三种角色与应用 Token：深链接、事件、下载、导出、模型/存储设置和撤权后页面均不可越界，不能只测菜单隐藏。
- 组件测试覆盖 SecretInput 不回传掩码、412 保留草稿、未知 total 不定进度、Token 弹窗关闭清理、URL 脱敏、分页末尾、快速切文档/租户取消过期定位。
- 布局基准覆盖窄屏/桌面、200% 缩放、暗色、键盘；大 PDF/万级块使用可复现 fixture，记录内存、长任务、定位耗时与渲染预算。性能阈值从配置/基线读取，不声称“无限文件流畅”。
- 每个页面交付必须有真实 API、完整状态与错误恢复；只有静态卡片、假进度、写死模型列表或无效按钮不算完成。

---

# 第四部分 · 优化实施补充规范

## 1. 支持范围与默认产品边界

“支持一种格式”拆成接收、预览、文本/结构解析、精确定位、索引五项能力，不允许只实现下载就标成完整支持。

| 范围 | 首个目标阶段 | 明确边界 |
| --- | --- | --- |
| TXT/MD、数字 PDF、DOCX/PPTX/XLSX | M1 | Office 先用结构锚点；公式、复杂版式与转换坐标进入后续质量验收；MD 中远程资源不自动抓取 |
| 扫描/混排 PDF、常见图片、Office 转 PDF | M2 | OCR 与来源定位有分层测试；旧版 Office 依赖隔离转换器，不以扩展名保证可转换 |
| 分块、人工修订、dense+BM25 检索 | M3 | 先满足租户隔离与版本一致性，不把 multi-vector 当默认上线前提 |
| 音视频、VLM、RAG 对话、远端解析 | M4 | 字幕/ASR/关键帧有统一时间映射；远端需外发授权；VLM 描述显式标记派生内容 |
| HTML/CSV/JSON/XML/EML/ZIP/EPUB/IPYNB 等 | M5 分批 | 文本类可先有安全源码预览，但结构解析/附件/递归能力分别验收；不执行 notebook 代码 |
| MSG、RAR/7Z、DXF/Visio/DICOM/OFD/PSD/WPS | 条件扩展 | 插件、二进制、许可、样本与来源映射齐备后启用；未启用只提供受权 L4 |

首期各运行形态均保留多租户，统一 PostgreSQL + Valkey；原生只表示 Python 通过 uv 在主机运行，不表示切换到 SQLite。M5 如确需单机轻量包，SQLite/持久化内嵌执行器单独设计与验收；不能用进程内列表或 FastAPI BackgroundTasks 承诺断电恢复。

## 2. 来源契约与 IR 校验不变量

所有 SourceRef 包含 `artifact_id`、`kind`；artifact 必须属于当前租户并与文档版本有授权关联。坐标与文字均相对于不可变来源，不相对于随时变化的最新文档。

| kind | 必需定位信息 | 约定 |
| --- | --- | --- |
| page_region | page_index、bbox、geometry_id | 左上原点、视觉方向、0–1 坐标，0≤x0<x1≤1、0≤y0<y1≤1；可有 polygon；跨页使用多个引用 |
| text_span | text_artifact_id、start、end、normalization_revision；结构来源另含 part/path/node_id | Unicode code point 偏移、0 基半开区间；JS UTF-16 索引显式转换；提取文本到源结构的 mapping 独立保存 |
| table_region | sheet/table 标识、row_start/end、col_start/end | 0 基半开区间；合并单元格、表头与原始 sheet 名保留，不依赖显示后的行号 |
| media_span | track_id、start_ms、end_ms、timebase_revision | 原媒体毫秒、0≤start<end；转码、VAD 切段、采样偏移都映射回原媒体，关键帧使用包含该帧的时间区间 |
| attachment | parent_artifact_id、entry_id、child_artifact_id | entry_id 不直接作为文件系统路径；嵌套引用有深度上限与独立权限 |
| artifact | artifact_id、reason | 只能定位到整个文件时的显式降级；不能伪造页坐标，UI 显示“文件级来源” |

校验不变量：
- doc/version/tenant/dataset 关系合法；block_id 唯一、order 可确定、parent 图无环；引用目标存在；时间/坐标范围合法。
- block.type 与 payload 判别类型一致；table_html 是派生展示，不替代表格结构真值；HTML 输出统一净化。
- 置信度未知可为空；来源为模型生成时必须记录 model_revision、prompt_revision 与输入引用，不把生成文本标记为 OCR 原文。
- chunk 保存覆盖到的 `block_id + text_range/source_ref` 列表；跨页、合并和表头复制仍能解释来源，不能只记“第几页”。
- 坐标映射覆盖 PDF CropBox/Rotate、图片 EXIF、Office 字体/转换版本；已归一化视觉坐标不得重复应用旋转。转换产物变化必须产生新 mapping。

## 3. 任务提交、索引发布与删除协议

不要求 PG、对象存储和检索引擎进行分布式事务；通过不可变产物、outbox、幂等动作和权威可见性门禁实现最终一致性，并明确失败时优先不返回陈旧内容。

1. **登记**：PG 事务内创建候选 doc_version、job、幂等记录与 outbox；更新 desired_version_id。上传对象已校验但未引用时由 TTL 清理，不暴露为业务文档。
2. **执行**：worker 获取租约/fence，按阶段写独立 attempt 产物；校验成功后 CAS 提交 checkpoint。旧 fence 只允许留下待清理孤儿产物，不得覆盖已提交状态。
3. **建索引**：冻结 chunk revision 集与 index_generation，outbox 驱动 upsert。向量和词法 point/document ID 含 tenant/version/chunk/revision/generation，重放同一内容幂等，过期动作不能覆盖新代。
4. **确认**：记录必需通道的 ID 清单摘要、数量、模型/schema revision 和可查询确认；不能仅凭“批量请求返回 200”宣称索引完整。部分发布的 expected set 明确排除失败单位，coverage 仍展示缺失。
5. **发布**：PUBLISHING 事务检查 job 未取消、fence 有效、desired_version 仍匹配、权限策略有效且 manifest 就绪，再 CAS 更新 visible_version/index_generation。M1/M2 的 parse-only profile 必需索引集合为空，明确 `retrieval_ready=false`，不可假报已索引。
6. **查询**：引擎先按 tenant/dataset/ACL 过滤，返回 ID/得分；服务端批量检查权威版本、generation、revision、enabled、删除与权限，再读正文和展开父块。每次只接受当前可见版本；索引补取超预算则返回较少结果或不可用，不返回旧版凑数。
7. **修订与回滚**：文本修订创建新 chunk revision，旧向量不得配新正文；新版本待发布时旧文档版本可保持可见，已撤权/禁用内容则立即拒绝。回滚只能切到仍有完整索引且权限有效的历史版本，记录审计，不能回滚权限变更。
8. **删除**：先 tombstone 并撤销读取，再取消在途发布、失效缓存、清理所有索引代；对象按引用计数与保留策略回收，避免删掉其他文档仍引用的内容。备份中的删除与合规保留例外需明确告知。

不宣称 exactly-once 执行，只保证重复投递不导致多次有效发布。远端任务在网络超时后可能仍运行；终态取消表示本系统不再发布结果，不保证第三方已经停止计费。

## 4. API、实时事件与资源访问补充

### 4.1 补齐动作契约

| 接口 | 语义 |
| --- | --- |
| GET `/v1/capabilities` | 按当前租户返回已启用格式、预览器、模型能力、配额与外发状态；不泄露节点/凭据细节 |
| POST `/v1/parsing/jobs/{id}/cancel` | 幂等取消，返回取消状态；已发布终态不能用取消代替删除 |
| POST `/v1/parsing/jobs/{id}/retry` | FAILED/DEAD_LETTER 且允许恢复时推进 attempt 并审计；DONE/PARTIAL_DONE/CANCELLED 不原地重启，新配置或补跑须新版本 |
| GET `/v1/parsing/jobs/{id}/events?after_seq=...` | 有界补发；游标过期返回需重新获取快照的明确信号 |
| GET `/v1/documents/{id}/versions` | 返回可授权访问的版本、coverage、解析/索引/发布状态 |
| POST `/v1/chunks/{id}/split`、`/v1/chunks/merge` | 请求包含预期 revision 和合法文本边界/相邻 chunk IDs；事务校验来源与 ACL |
| PATCH `/v1/chunks/{id}` | 文本或 enabled 修改；If-Match 必需，后端自动登记重索引，不依赖前端再调用 reembed |
| POST `/v1/pipelines/{name}/validate`、`/dry-run` | 只做校验/路由解释，不执行插件、不计模型推理费用 |
| POST `/v1/chat/runs/{run_id}/cancel` | 请求取消生成并释放本地资源，远端取消能力明确返回 |
| GET `/v1/chat/runs/{run_id}/events?after_seq=...` | 重放已有事件，不重复调用模型；保留期外不可无提示重生成 |

### 4.2 事件与长连接

- 通用事件 envelope：schema_version、event_id、seq、type、tenant_scope、resource_id、attempt、ts、payload；job 的 resource_id 即 job_id，进度字段位于 payload。seq 在单 job/chat run 内单调，持久化后再推送，客户端去重并忽略过期 attempt。
- 首次连接先取快照和 latest_seq，再从该序号补发，避免快照/订阅竞争；阶段进度可重置，overall_progress 只展示带 attempt 的估算，total_units 未知时显示不定进度。
- 浏览器 WS 使用安全同站会话并校验 Origin，或通过认证接口获取短时一次性 ws ticket；禁止把长期 JWT 放入 URL。权限变更、登录过期和离线超时后主动断开；GET 快照轮询作降级。
- POST SSE 使用 fetch + 流解析，处理 UTF-8/事件分片、心跳、代理缓冲与中断；run_id 在 meta 返回。断线重连读取已存事件，不重建回答；按 event_id 去重，done/error 是唯一终结事件。生成执行与单条 HTTP 连接解耦；客户端使用幂等请求键重连时先返回已有 run_id，避免 meta 丢失后重复计费；后台 worker 中断只能标记失败或返回已存片段，不承诺任意模型断点续生成。
- webhook 为至少一次通知，使用 event_id、签名、时间戳、重试预算；接收方去重，发送端做 SSRF/重定向与密钥审查。

### 4.3 权限撤销与签名 URL

预签名 URL 在过期前通常不能按应用 ACL 即时撤销。敏感租户默认通过鉴权资源代理，Range、HLS manifest/segment/key 均复验；普通低敏资源可选择短 TTL 签名访问，但产品必须告知撤权延迟，不得承诺“即时”。已经发送到客户端或第三方模型的内容无法收回。

ACL 撤销的验收语义是：权威事务提交后新发起的授权读取必须拒绝；长连接在下一敏感输出前重新检查或因撤权事件终止。缓存不永久保存带权限正文；流式答案与分享快照都受来源权限约束。

## 5. 验证矩阵与发布门禁

| 类别 | 必测场景 | 退出标准 |
| --- | --- | --- |
| 契约 | IR 合法/非法例、OpenAPI 破坏性变更、事件未知字段/版本 | fixture 与客户端生成检查通过；未知 major 明确拒绝，不静默误读 |
| 幂等/并发 | 同键同请求、同键异请求、跨租户同 hash、同文档并发重解析 | 不串数据、不重复有效发布、不覆盖较新 desired version |
| 恢复 | 每阶段 worker 退出、重复消息、outbox 派发中断、租约过期、旧 worker 迟到 | 仅从已提交 checkpoint 续跑；旧 fence 不能修改终态或当前索引 |
| 索引 | 单通道失败、部分 upsert、旧 generation 迟到、权限撤销、删除、编辑冲突 | 未就绪版本不发布；召回只用授权有效 revision；旧结果不能混入 |
| 来源 | 旋转/裁剪 PDF、跨页表、Office 转换、EXIF、Unicode 偏移、媒体转码 | 不引用错误文档/版本；精确锚点 fixture 通过，近似或文件级来源显式标注 |
| 安全 | XSS/SVG、压缩炸弹、路径穿越、外部实体、SSRF、宏、伪造文件头 | 攻击输入被隔离/拒绝，不越权访问、不执行任意代码、不泄露密钥 |
| 预览 | 大 PDF、长音频、大表、下载中撤权、签名过期、取消与断线 | 可回退、可取消、不崩溃；内存与交互延迟在目标设备预算内 |
| 检索/问答 | 无相关材料、父块含受限内容、伪引用、文档提示注入 | 无授权泄露与虚假引用；无证据时明确拒答；评分不伪称概率 |
| 离线/运维 | 断网冷启动、模型缺失、数据库恢复、版本升级回滚 | 不访问公网；内网服务受控；缺能力清晰报错；按恢复手册恢复可见版本与索引 |

质量指标按任务分开：文本 CER/WER、表格 TEDS/单元格结构 F1、阅读顺序、来源定位误差、ASR CER/WER 与时间误差、检索 Recall@K/nDCG@K、答案引用正确性与证据覆盖率。先用约 50 个高风险样本做 M0 PoC，再扩到约 300 个分层标注案例；300 不是所有格式/100+ 语言质量保证，未测语种仅标“模型声称支持”。

门禁分两类：安全/身份/版本不变量必须全通过；质量与性能在 M0 冻结基线、允许退化幅度、重复试验和统计方法。不能仅凭一次成功演示验收，也不能在看到结果后调低门槛。

## 6. 容量、SLO 与成本测量

- **记录条件**：CPU/GPU 型号与内存、OS/运行时、模型与 tokenizer revision、量化、DPI、页数/文本长度、批大小、并发、冷/热缓存、外部网络和失败样本比例。
- **延迟拆解**：上传/排队/解析/增强/分块/嵌入/索引/发布分别统计 P50/P95；问答区分首 token、生成总时长与检索延迟。吞吐按页/音频分钟/token 计，不能用“文档/秒”混合比较不同文件。
- **向量存储**：dense 原始大小约 `N × D × bytes`。100 万块、1024 维、float32 约 4.096 GB，尚未包含 HNSW、payload、副本与多版本。multi-vector 约 `N × T × D_mv × bytes`；若平均 256 个 token、128 维、float16，已经约 65.536 GB；若 D_mv=1024 则约 524.288 GB。D_mv 必须读取实际模型输出，不能套用其他 ColBERT 模型维度。
- **对象容量**：原件 + 页面图片 + 预览 + 音视频转码 + IR + 保留版本分别测量放大系数；设租户配额、临时产物 TTL、版本保留、模型缓存和磁盘低水位保护。
- **成本归因**：每 job 记录 CPU/GPU 时间、外部请求与计费单位、存储字节、重试浪费、缓存节省；A/B shadow 与 online job 分账，不让实验压垮正常任务。
- **观测与告警**：队列最老任务、outbox lag、租约回收、索引 lag、权限同步失败、磁盘水位、模型 readiness；Prometheus 只用有界 adapter/格式/状态标签，job_id、doc_id 和无限租户维度放日志/分析库。
- **恢复目标**：RPO/RTO、备份频率、故障域与恢复容量由试点需求冻结；PG 与对象为恢复事实源，检索索引可重建但必须估算重建时间。未演练前不宣称高可用。

## 7. 实施前需确认的业务假设

用户已明确要求保留多租户、简化用户管理、外部应用 Token 授权、uv、集中配置、URL 导入、多存储及模型配置，不再将这些列作待确认假设。仍需试点确认的方向：Linux 首发、M3 作为检索交付点、默认禁止未经批准的公网访问和会话分享、非必需扩展后置。公网 URL/云存储/云模型需管理员显式批准对应网络策略后启用。

立项时需要补齐：
1. 是闭源商业分发、内部部署还是对外 SaaS；是否允许 AGPL/条件许可组件，模型能否再分发。
2. 首批真实格式、语言、扫描占比、最大页数/时长、日增量、存量与查询并发，以及可公开/脱敏的评测样本。
3. 目标硬件、是否可用 GPU、外部服务准入、地域/保留限制及敏感文档撤权要求。
4. 团队人数、预算、首期交付期限、人工标注资源和 RPO/RTO；据此排期，不由组件清单反推固定周数。

## 8. 官方核验依据与证据边界

以下链接用于核验本次修订，不是未经锁版即可永久适用的保证；在线主分支与 model card 会变化。发布清单必须附对应 tag/commit/revision 的许可证正文与哈希。

- [CTranslate2 硬件支持](https://opennmt.net/CTranslate2/hardware_support.html)：预构建支持 CPU 与 NVIDIA GPU；不能据此宣称 faster-whisper 具备 CoreML 后端。
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp)：Apple Silicon 的 CoreML 编码器支持属于另一套运行时。
- [BGE-M3 官方说明](https://github.com/FlagOpen/FlagEmbedding/tree/master/research/BGE_M3)：明确说明 query 不再需要指令；dense、learned sparse、multi-vector 是不同表示。
- [Qdrant multi-vector 指南](https://qdrant.tech/documentation/tutorials-search-engineering/using-multivector-representations/)：建议在候选集上做 late interaction；无须对每个 token 向量建立 HNSW。
- [fastText 语言识别](https://fasttext.cc/docs/en/language-identification.html)：输入为 UTF-8 文本；lid.176 模型以 CC-BY-SA-3.0 分发。
- [MinerU LICENSE](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)：当前主分支附带企业门槛和对外服务标识义务；不能外推到所有历史代码/权重版本。
- [extract-msg LICENSE](https://github.com/TeamMsgExtractor/msg-extractor/blob/master/LICENSE.txt) 与 [MinIO LICENSE](https://github.com/minio/minio/blob/master/LICENSE)：分别为 GPL-3.0 与 AGPLv3；本次通过官方仓库检索结果确认标记，发布前须归档锁版正文。
- [Redis 官方许可证](https://redis.io/legal/licenses/) 与 [Valkey](https://github.com/valkey-io/valkey)：许可随版本不同；替换 broker 仍需兼容性测试。
- [Qwen2.5-VL-7B](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct) 与 [72B](https://huggingface.co/Qwen/Qwen2.5-VL-72B-Instruct)：官方 model card 分别标记 Apache-2.0 与 Qwen；本次全文拉取受限，不将附加条款视为已完成法务核准。
- [FFmpeg 法律说明](https://ffmpeg.org/legal.html)：实际许可取决于构建选项和依赖，编解码专利另计。
- [Celery FAQ](https://docs.celeryq.dev/en/stable/faq.html) 与 [broker 可见性超时](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)：不承诺 Windows 原生支持，长任务与重复投递需显式处理。
- [mammoth.js LICENSE](https://github.com/mwilliamson/mammoth.js/blob/master/LICENSE) 与 [官方说明](https://github.com/mwilliamson/mammoth.js)：许可证为 BSD-2-Clause，项目归属 Michael Williamson；官方明确不执行输入净化。
- [JSZip LICENSE](https://github.com/Stuk/jszip/blob/main/LICENSE.markdown)：MIT/GPLv3 双许可，本方案选择 MIT 路径。
- [uv 锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/) 与 [Docker 集成](https://docs.astral.sh/uv/guides/integration/docker/)：CI 使用 `--locked` 检查漂移，生产依赖显式选择，宿主机 `.venv` 不复制进镜像。
- [阿里云 OSS Python SDK V2](https://www.alibabacloud.com/help/en/oss/developer-reference/2-0-manual-preview-version/)：原生存储适配的候选 SDK；具体版本、鉴权链与分片行为仍需集成验收。
- [百炼 OpenAI-compatible 接口](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope)：endpoint 与地域、工作空间和凭据绑定；本文不硬编码示例域名为通用默认值。

---

# 第五部分 · AI 工程生成实施规范

## 1. 工程基线与模块划分

### 1.1 命名、运行形态与目录

- 仓库/产品/Python distribution 名称统一为 `owli-parser`，Python import 包为 `owli_parser`，命令为 `owli`，环境变量前缀为 `OWLI_`。现有设计文档文件名不影响项目命名。
- 采用 monorepo：后端一个 Python 项目、前端一个 Web 项目；模块化单体不是每个目录一个微服务。API、控制进程和 worker 共享领域契约，分别启动。
- Python 3.12 为首个兼容基线，补丁版本在 `.python-version` 锁定；前端固定 React 18 + TypeScript strict + Vite + Tailwind 3 兼容组合，Node 22、pnpm 的具体版本在构建配置锁定，不使用 `latest`。升级依赖须整体测试。
- 首期统一 PostgreSQL、Valkey，Local/S3/OSS 按 profile 选择；retrieval profile 才启动 OpenSearch/Qdrant。未配置 LLM 不影响文件上传和基础解析。

目标目录（这是生成目标，不表示当前目录已经存在）：

```text
owli-parser/
├── backend/
│   ├── pyproject.toml / uv.lock / .python-version
│   ├── src/owli_parser/
│   │   ├── bootstrap/           应用工厂、容器、进程入口、生命周期
│   │   ├── workflows/           跨模块提交、发布、删除等事务编排
│   │   ├── core/                ID、时钟、错误、执行上下文等小型通用类型
│   │   ├── contracts/           IR、事件、跨模块 DTO 与版本声明
│   │   ├── modules/
│   │   │   ├── identity/        用户、租户、成员、Session
│   │   │   ├── access/          应用、Token、scope、资源授权、授权决策
│   │   │   ├── catalog/         数据集、文档、版本、可见性与删除
│   │   │   ├── artifacts/       逻辑产物、物理位置记录、引用与生命周期
│   │   │   ├── ingestion/       上传、URL/对象导入、预检
│   │   │   ├── jobs/            状态机、调度、租约、checkpoint、事件
│   │   │   ├── parsing/         pipeline、解析器编排、IR 规范化
│   │   │   ├── knowledge/       分块、修订、复核、索引发布清单
│   │   │   ├── retrieval/       召回、过滤、重排与检索测试
│   │   │   ├── chat/            对话执行、事件、证据与引用
│   │   │   └── configuration/  配置版本、存储/模型连接、绑定与密钥引用
│   │   ├── ports/              Storage、Model、Queue、Secret、SafeFetcher 等协议
│   │   ├── adapters/           local/s3/aliyun_oss、模型协议、解析器、网络传输
│   │   ├── infrastructure/     DB/UoW、outbox、broker、遥测与资源代理
│   │   └── resources/          defaults.yaml、Schema、内置配置模板
│   ├── migrations/            Alembic 迁移，统一版本链
│   └── tests/                 unit/contract/integration/security/fixtures
├── frontend/
│   ├── package.json / pnpm-lock.yaml
│   ├── src/                   详见前端 §8 的 feature 结构
│   └── tests/                 组件、契约和 Playwright 测试
├── contracts/                 构建导出的 OpenAPI、IR/Event JSON Schema
├── config/                    无密钥的环境覆盖与示例
├── deploy/                    Dockerfile、Compose profiles、健康检查
└── scripts/                   跨平台开发/检查入口，不重复业务逻辑
```

### 1.2 模块责任与依赖方向

| 模块 | 独占的数据/职责 | 对外契约与禁止事项 |
| --- | --- | --- |
| identity | users、tenants、memberships、sessions | 输出已认证用户与租户上下文；不包含文件、模型逻辑 |
| access | applications、tokens、grants、决策矩阵 | 输出授权结果；不实现解析、下载或存储 SDK；不得把 Token 当用户模拟登录 |
| catalog | datasets、documents、doc_versions、删除/可见指针 | 负责资源身份与版本；不拉取 URL，不直接调用推理 SDK |
| artifacts | artifacts、artifact_locations、引用与回收 | ArtifactService 管理逻辑产物/位置，StorageRouter 调用存储端口；不处理网页或模型协议 |
| ingestion | uploads、source_imports、输入预检 | 输出经过校验的 artifact；调用安全下载/存储端口，不自行建索引 |
| jobs | jobs/attempts/steps/events、租约与派发 | 驱动状态和恢复；不包含 PDF/OSS/OpenAI 专属逻辑 |
| parsing | adapters 注册、pipeline 编译、IR 规范化 | 接收 artifact 与 execution plan，返回可验证 ParseResult；不决定租户或 ACL |
| knowledge | chunks/revisions、review、index_manifests | 管理修订和完整索引代；发布通过 catalog 服务完成，不越过权限门禁 |
| retrieval / chat | 检索流水线 / runs/events/evidence | 检索先授权再取正文；chat 只消费授权证据和模型能力端口 |
| configuration | 各类配置 revision、source_locations 与 secret 元信息 | 负责 validate/publish/rollback；不得在请求期间安装插件或执行 YAML 代码 |
| infrastructure / adapters | artifact 物理 IO、DB、broker、SDK、遥测 | 实现端口；不得反向调用 HTTP router，也不承担应用权限判定 |

依赖方向：`router/worker handler → application service → domain + ports`；adapter 实现 port，bootstrap 注入实现。模块内部按 `api.py / service.py / domain.py / schemas.py / repository.py` 起步，复杂后再拆子包，不生成没有逻辑的多层转发类。

跨模块只依赖公开 service/DTO/port，不导入他模块 ORM 私自改表；跨模块事务由 `workflows` 共享一个 UoW，网络 IO 在事务外。模块不得反向依赖 workflows；例如 knowledge 产出 manifest、catalog 负责发布，PublishVersion workflow 协调两者，避免相互 import。`core` 不成为存放所有业务规则的万能目录。用 import-linter 或同等静态规则检查反向依赖和循环依赖。

### 1.3 代码落地约束

- FastAPI lifespan 管理 DB/HTTP/SDK client；资源池按连接 revision 建立并可排空，不在模块 import 时读取配置、连接 DB 或加载模型。
- API 与 DB IO 使用有界异步调用；CPU/GPU 解析在隔离 worker 子进程。同步 SDK 必须进入有并发上限的线程池或 worker，不能直接阻塞事件循环。
- `ActorContext` 至少含 `kind(user/application/system)、subject_id、tenant_id、credential_id、auth_revision、trace_id`；system worker 的执行身份不免除对原始调用主体的权限复查。
- 仓储租户操作必须显式接收 TenantContext；禁止默认 `tenant_id=None` 表示全库。后台全租户维护使用独立、可审计入口，逐租户执行。
- 使用 SQLAlchemy 2 + Alembic + Pydantic 2/pydantic-settings；API DTO、领域对象、ORM 模型分开，ORM 不直接作为外部响应。
- 时间统一 UTC aware timestamp；金额采用 Decimal + 币种；ID 不携带文件路径；日志只记录安全 ID、错误码、耗时与 trace_id。
- 长事务不包住下载/推理。成功副作用通过 outbox 登记；重试由一层预算控制。业务错误使用稳定 ErrorCode，禁止吞异常后返回空成功结果。
- 可注入 Clock、IDGenerator、SecretResolver、SafeFetcher 及模型/存储端口以支持确定性测试；不为每个简单函数创建抽象接口。

## 2. Python/uv 与可复现构建

- `backend/pyproject.toml` 是 Python 依赖唯一声明，`uv.lock` 和 `.python-version` 提交版本管理；不同时手写维护 requirements.txt/Poetry/Conda。兼容导出文件只能从锁文件生成。
- 项目设置正式 build-system（选用锁定版本的 hatchling），声明 `src/owli_parser`、资源包和 `[project.scripts] owli = "owli_parser.bootstrap.cli:main"`；测试不能依靠手工 PYTHONPATH 才能 import。
- 业务依赖分 core 与固定用途 extras：cpu、storage-s3、storage-oss；dev group 放 Ruff、mypy、pytest、pytest-asyncio、coverage、import-linter。生产不用 `--all-extras` 强装互斥 GPU 引擎。
- CUDA/Paddle/复杂视觉运行时如不能统一求解，使用独立 worker 镜像与各自 uv 项目/锁文件；通过版本化协议交换任务与产物，不让重模型依赖污染 API 镜像。
- 生产镜像锁定 Python/uv/系统依赖版本或 digest；先验证锁文件，再装明确 extras。镜像构建使用 `--no-default-groups`，不打入测试工具、密钥、宿主机 `.venv`。

以下命令是必须实现的 CLI/脚本契约，执行目录为仓库根目录；不是声称当前已能运行：

```bash
uv sync --project backend --locked --extra cpu --extra storage-s3 --extra storage-oss
uv run --project backend --locked owli config validate
uv run --project backend --locked owli db upgrade
uv run --project backend --locked owli admin bootstrap
uv run --project backend --locked owli serve
uv run --project backend --locked owli worker --pool cpu
uv run --project backend --locked owli scheduler
uv run --project backend --locked ruff check backend
uv run --project backend --locked ruff format --check backend
uv run --project backend --locked mypy --config-file backend/pyproject.toml backend/src
uv run --project backend --locked pytest -c backend/pyproject.toml backend/tests
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend check
```

以上路径均相对仓库根目录；`--project` 只选择 Python 项目，不假设它改变 cwd。CLI 自行解析已配置资源/迁移路径，不依赖当前 shell 目录。`frontend check` 固定执行 typecheck、lint、unit 和 build；E2E 独立命令运行。

- 管理员初始化从受保护终端输入/部署 secret 读取密码，不提供默认 admin/admin；命令重复执行不得重置已有密码。迁移单独执行，API 副本不能各自启动时竞争迁移。
- `--locked` 用于验证锁文件与项目声明一致；`--frozen` 不能替代此验证。生产入口使用已同步虚拟环境内命令，禁止启动时联网自动更新依赖。
- 离线包须覆盖目标平台 wheels、系统库、模型与前端 worker/font 资源；依赖源和私有索引凭据分开配置，断网冷启动必须测试。

## 3. 集中配置、常量与密钥

### 3.1 分类与优先级

| 类型 | 事实源 | 示例与变更方式 |
| --- | --- | --- |
| 协议不变量 | 集中的 Enum/Schema/协议注册表 | 状态码、状态转换、IR 主版本、scope 名称、算法安全下限；不是任意管理员可修改的运行参数 |
| 启动配置 | defaults.yaml → 环境覆盖 YAML → `OWLI_` 环境变量 | DB/broker 地址、监听、可信代理、日志级别、密钥源；修改需滚动重启 |
| 平台策略 | 配置 revision + 平台管理入口 | 网络准入、硬资源上限、可安装适配器；租户不能放宽 |
| 租户/数据集配置 | DB 中不可变 revision | 存储、模型、pipeline、chunk、配额、用途绑定；validate 后发布 |
| 单次任务选项 | API 类型化字段白名单 | 语言提示、获准 pipeline、分块参数；只可在已授权范围和平台上限内调整 |
| 机密 | SecretResolver | DB 密码、云密钥、模型 key、Token pepper、Session 密钥；不进入明文 YAML/日志/前端配置 |
| 前端公开配置 | `/v1/runtime-config` 与设计 token 文件 | 产品名称、能力提示、主题、客户端限额；后端仍重新校验全部限制 |

“配置不写死”指可调参数不得散落在业务代码；枚举、字节/时间单位转换、合法坐标范围等协议不变量集中定义，不把程序正确性变成可随意配置的开关。

启动合并使用嵌套对象深合并、数组整体替换；未知键、重复 YAML key、非法单位和未解析的引用报错。动态有效配置为平台策略约束下的租户 → 数据集 → 任务覆盖；禁用能力不能由下层重新启用，限额取不超上限的结果，不做无条件字典覆盖。

### 3.2 配置目录与字段清单

`AppSettings` 使用 `env_prefix="OWLI_"`、`env_nested_delimiter="__"`；`OWLI_CONFIG_FILE` 只定位管理员指定文件，禁止用户请求指定任意配置路径。YAML 文件由显式 settings source 加载，不假设 pydantic-settings 自动读取。

| 配置域 | 至少覆盖的字段 |
| --- | --- |
| app/http/database/broker | app_name、environment、bind_host/port、public_base_url、DB/broker secret_ref、pool、statement_timeout、trusted_proxies、CORS |
| auth/tokens | Session idle/absolute TTL、password 策略、登录限流、Token TTL 上限、轮换窗口、pepper_ref、授权检查策略 |
| upload/imports | 直传/总大小、part 大小、并行数、URL 重定向/超时/下载字节上限、临时文件 TTL、MIME 白名单 |
| network | offline/hybrid、按用途区分的 endpoint policy、允许端口、内网 allowlist、TLS CA、出口代理 |
| jobs/parsing | 队列名称、并发、lease/heartbeat/deadline、attempt、退避、CPU/RAM、页区间、OCR DPI、coverage |
| chunk/retrieval | tokenizer_ref、target/max/overlap、父块预算、召回上限、RRF k/weights、TopK、rerank_model_ref |
| storage/models | profile/connection 引用、默认绑定、能力与费用上限；详见 §6/§7 |
| retention/observability/ui | 孤儿产物/事件/审计保留、清理批量、磁盘水位、采样、轮询/重连、页面大小、渲染预算 |

无密钥的配置形状示例（完整生成时 defaults.yaml 必须覆盖字段清单，不能仅复制这个节选）：

```yaml
schema_version: "1.0"
app:
  name: owli-parser
  environment: development
network:
  mode: offline
upload:
  direct_max_bytes: 104857600
  total_max_bytes: 1073741824
  part_size_bytes: 8388608
imports:
  max_redirects: 3
  connect_timeout_seconds: 10
  total_timeout_seconds: 600
  allowed_ports: [80, 443]
jobs:
  max_attempts: 3
  lease_seconds: 120
  heartbeat_seconds: 30
chunk:
  target_tokens: 400
  max_tokens: 800
  overlap_tokens: 50
retrieval:
  per_channel_limit: 50
  candidate_limit: 100
  top_k: 10
  rrf_k: 60
storage:
  default_profile_ref: local-primary
```

这些数字仅定义默认配置，不再在函数参数默认值、前端校验和部署脚本中重复。交叉校验至少覆盖 `heartbeat < lease`、`overlap < target <= max`、`top_k <= candidate_limit`、part 与总大小约束，以及模型 context/维度和用途匹配。

### 3.3 生效、审计与前端读取

- 实现 `load_boot_settings()`、`resolve_effective_config()`、`validate_config()`、`publish_revision()`；配置解析一次后通过依赖注入传递，禁止业务模块随处 `os.getenv()`。
- 新任务绑定所有有效 revision、解析后规范化配置的 digest、来源说明与授权上下文；canonical JSON 序列化使用确定性键序和数字规则。secret 仅记录 ref/version，不把明文纳入快照。
- 配置发布使用 If-Match；校验与连接测试分开。保存配置不自动产生付费调用；“测试连接/模型”是显式动作，记录测试范围与费用提示。
- 新配置仅影响新执行，旧任务钉住旧版本；密钥轮换与紧急撤销例外：允许安全替换仍有效的凭据引用或停止任务，但不能复活已撤销密钥。模型/端点改变不能偷偷替换旧任务的执行语义。
- `/v1/runtime-config` 只返回登录前可公开内容；租户能力、配额、已授权模型选择来自鉴权后的 `/v1/capabilities`，禁止把整份 settings 序列化返回浏览器。
- 本地开发可用 gitignored `.env`/受限 secret 文件；生产采用 SecretProvider 或加密存储 + 外部主密钥。`SecretStr` 仅防意外打印，不代表落盘加密。前端写入密钥后只回显 `configured/last_rotated_at`，空值不表示清除，清除走独立操作。

## 4. 多租户、轻量用户与外部应用 Token

### 4.1 用户与租户边界

- users 为全局登录身份，memberships 将用户关联到多个租户；登录后可切换已有成员关系的租户。租户由平台管理员创建/停用，租户管理员管理本租户成员，不开放匿名自助注册。
- 三种租户角色固定为 `admin/editor/viewer`；不实现角色编辑器、部门树、用户组、复杂 ABAC、计费套餐或企业 SSO。平台管理员为单独平台权限，不自动获得所有租户文档读取权。
- 数据集可见性为 `tenant`（本租户有效成员可按角色访问）或 `private`（创建者与本租户 admin）。文档/block 继承数据集权限；`acl_ref` 是数据集引用，读取时查询当前规则，不信任历史 IR 权限快照。
- 数据集创建者为 user；外部应用首期不能创建租户、用户或数据集，只在获授数据集中操作文档。用户停用保留审计身份和数据所有权记录，可由管理员移交；不能移除最后一个有效租户 admin。

| 操作 | admin | editor | viewer | 外部应用 |
| --- | --- | --- | --- | --- |
| 读取文档、预览、检索、问答 | 本租户全部数据集 | tenant + 自己的 private | tenant + 自己已有的 private | scope 与显式数据集 grant 同时允许 |
| 新建数据集 | 允许 | 允许 | 拒绝 | 首期拒绝 |
| 上传/URL 导入、解析、编辑/删除文档 | 允许 | 有数据集访问权时允许 | 拒绝 | 对应写 scope + grant |
| 改数据集可见性/设置/删除数据集 | 允许 | 仅自己创建的数据集 | 拒绝 | 首期拒绝 |
| 模型、存储、pipeline 发布与密钥 | 允许，受平台策略限制 | 只选择已发布配置，不读密钥 | 拒绝管理 | 首期拒绝管理 |
| 成员、应用、Token、授权与审计 | 本租户管理 | 拒绝 | 拒绝 | 不可自助提权 |

应用的 private 数据集授权必须由 tenant admin 显式授予并在数据集页可见，不因拥有一个有效 Token 自动访问。对象资源不存在或属于其他租户时统一按 404 处理；scope 不足按 403，避免枚举其他租户资源。

### 4.2 两条认证链，复用授权服务

**浏览器用户**：账号密码采用 Argon2id；服务端生成不透明 Session，数据库只存摘要。Cookie 使用 HttpOnly、Secure、SameSite，生产同源反向代理；状态变更校验 CSRF 与 Origin，登录也校验来源并限流。修改密码、停用账号或登出撤销对应 Session。Session 不放 localStorage。

用户请求使用 `X-Tenant-ID` 选择租户，服务端以当前有效 membership 验证；登录/查询本人租户不要求该头。浏览器事件连接用租户绑定的一次性 ticket，不能仅靠不受验的 query 参数切租户。每个 Query key、WebSocket 订阅和缓存都含 tenant_id；切换租户必须取消旧请求、关闭旧连接、清除敏感正文与草稿。

**外部应用**：只接受 `Authorization: Bearer <token>`，走同一个 `/v1` 业务 API。应用 Token 自身绑定 tenant_id；无需 X-Tenant-ID，若提供且不一致直接拒绝。Token 不进入 URL、Cookie、webhook query 或日志。请求同时携带用户 Session 和应用 Token 时拒绝歧义认证，不悄悄选择权限更大的身份。

```text
认证主体 → 验证租户有效 → 校验接口 scope/用户角色 → 校验资源所属租户
        → 校验数据集权限或应用 grant → 校验配额/网络政策 → 应用服务
```

OpenAPI 每个 operation 显式声明 user/app 支持范围与所需 scope，公共登录/健康接口使用明确白名单；默认不是“有 Token 就能调用全部接口”。浏览器隐藏按钮仅为交互提示，服务端始终鉴权。

### 4.3 应用和 Token 数据契约

| 对象 | 字段与规则 |
| --- | --- |
| Application | id、tenant_id、name、description、status、allowed_scopes、rate_limit_ref、created_by、auth_revision；一个应用归属一个租户 |
| ApplicationGrant | tenant_id、application_id、dataset_id、actions；联合外键阻止跨租户；授权新增与撤销递增 revision |
| ApplicationToken | id/public_id、application_id、tenant_id、name、token_digest、digest_key_version、scopes、dataset_ids、expires_at、revoked_at、last_used_at、created_by |
| Token 限权 | token.scopes 是 application.allowed_scopes 子集，token.dataset_ids 是 application_grants 子集；最终权限取当前应用、Token、资源授权三者交集 |
| 审计 | actor_kind、actor_id、tenant_id、credential_id、operation、resource_id、decision/reason、trace_id；不保存原始 Token |

- Token 格式为协议前缀 + 随机 public_id + 高熵 secret，使用 CSPRNG、secret 安全下限 256 bit；前缀和格式集中在协议定义，不作为业务散落字符串。
- 只在创建成功时展示原始 Token 一次，响应 `Cache-Control: no-store`；数据库存 HMAC-SHA-256 摘要与 pepper key version，pepper 在外部 secret provider。校验常量时间比较；不存在 token_id 也使用统一错误与受限响应时序。
- 安全随机 Token 不使用人类密码，也不需要用慢密码哈希替代高熵 secret 摘要；用户密码仍使用 Argon2id。Token 摘要不能用于还原原值。
- 必填到期时间，默认与最大 TTL 来自配置；首期不签发永不过期 Token，不实现自动 refresh token/OAuth 授权服务器。
- 创建 Token 的幂等记录不得缓存明文。若首个响应丢失，同 key 重放只返回已创建 token_id 和 `secret_available=false`；管理员撤销后重新签发，不为“再次展示”长期保存可解密 Token。
- 轮换创建新 Token，不原地改旧 secret；重叠有效窗口受配置约束，页面展示旧 Token 在途任务，再显式撤销旧 Token。吊销/禁用立即写权威库并审计。
- 每次入口鉴权读取权威 Token/应用状态，不依赖 TTL 正缓存承诺即时撤销；长连接在敏感输出前重验。DB 授权查询不可用时 fail closed；last_used_at 可异步节流，不影响吊销检查。
- 用户 Session 自然到期/主动登出不取消已接收任务；账户停用、membership 或资源权限撤销仍阻止新的敏感副作用与发布。Token 自然到期只阻止新请求，不取消之前已授权接收的任务；手动撤销、应用停用或资源 grant 收回时，未执行/在途任务在下一检查点停止新的外发与发布，原因 `AUTHORIZATION_REVOKED`。已发送第三方的工作不能保证撤回。
- 后续使用同应用的新 Token 仍可读取其被授权任务；所有权绑定 application_id，不绑定单个 Token，但原始 credential_id 保留用于撤销传播与审计。

### 4.4 scope 注册表与资源范围

| scope | 开放接口/动作 | 额外资源约束 |
| --- | --- | --- |
| `datasets:read` | 数据集列表/详情 | 仅 Token 数据集集合；列表不得返回其他 ID/数量 |
| `documents:read` | 文档/版本/blocks/chunks/IR 元数据 | 所属数据集 read grant；完整 IR/正文仍按资源权限检查 |
| `documents:write` | 文档命名、创建版本、chunk 编辑/禁用 | write grant；危险变更用 If-Match |
| `documents:delete` | 删除文档 | 独立 delete grant，不由 write 隐含授予 |
| `imports:create` | 上传 init/part/complete、POST imports | write grant；上传会话从 init 绑定 dataset_id，不能完成后换库 |
| `imports:read` | 导入/上传状态 | 同应用创建且数据集仍授权 |
| `jobs:create` | POST parsing/jobs；导入时自动提交解析 | write grant；同时检查 artifact 与 pipeline 可用性 |
| `jobs:read` | 任务快照与事件 | 同应用任务；共享文档状态通过 documents:read 获取 |
| `jobs:control` | 取消/重试任务 | 同应用任务 + write grant；不能取消其他应用任务 |
| `artifacts:read` | 原件下载、预览内容、导出文件流 | read grant；不等于列举整个 bucket |
| `retrieval:query` | POST retrieval/test | read grant；提交数据集必须全部获授权，不静默扩大范围 |
| `chat:invoke` | POST chat、本人应用 run 事件/取消 | read grant、模型用途授权及费用预算；固定引用仍受资源校验 |

POST imports 自动提交解析，因此同时需要 `imports:create + jobs:create`；缺少任一权限必须在拉取文件之前拒绝。没有 scope 字符串通配符、隐式超级 Token 或默认“全部数据集”；新增数据集需要显式授权。Token 不开放系统设置、模型密钥、Token 签发和成员管理 scope。

配额按 tenant + application 限制请求速率、上传/导入字节、排队量、并发、模型成本；计数原子更新，任务准入先预留预算再按实际结算。429 返回 Retry-After，禁止限流重试风暴；客户端 IP 只从受信代理头获取，可选 IP allowlist 不替代 Token。

### 4.5 外部调用示例与生命周期

```http
POST /v1/imports
Authorization: Bearer <仅通过安全渠道提供的应用Token>
Idempotency-Key: <调用方生成的唯一请求键>
Content-Type: application/json

{
  "dataset_id": "<已授权数据集ID>",
  "source": {"kind": "url", "url": "https://files.example.org/manual.pdf"},
  "pipeline_ref": "default"
}
```

返回 202 `{id, state, parsing_job_id: null, status_url}`；客户端轮询 import，获得 parsing_job_id 后轮询 job，DONE/PARTIAL_DONE 后读取指定 version_id 的 IR/blocks 或发起检索。`status_url` 是本系统相对路径，不能由源 URL 控制。接口版本、错误码、scope 和示例从 OpenAPI 生成集成文档，Python/TypeScript 示例不嵌入真实密钥。

## 5. 文件 URL 与统一接入链路

### 5.1 输入模型与状态

`ImportRequest.source` 为 discriminator 联合：`url` 与 `storage_object`；本地文件走 upload 会话，最终统一为 artifact。URL 首期仅用于获取单个文件，不做网页递归、登录网页自动化或网页内资源抓取。

| source.kind | 字段 | 校验 |
| --- | --- | --- |
| url | url、可选 filename_hint、可选 credential_ref | 只允许 HTTP(S)；凭据必须是获准的数据集 source_download 用途并绑定目标域，不能借用模型/存储密钥；不允许用户自填任意请求头 |
| storage_object | source_location_id、relative_key、可选 object_version | location 绑定当前数据集和获准 profile/bucket/inbox_prefix；只接收规范化相对 key，不允许任意 bucket/key |

通用字段：dataset_id、pipeline_ref、可选 document_id/expected_revision、解析选项；tenant_id 取认证上下文。请求中不能传本地路径、任意 storage endpoint、模型 API key 或待安装插件。source_location 由租户 admin 预先绑定到数据集；有该数据集导入权限才可使用，不允许把输出存储中另一个 private 文档当作导入源绕过 ACL。

```text
PENDING → FETCHING → VERIFYING → READY → SUBMITTED
              └→ RETRY_WAIT → 新 attempt
非终态 → CANCELLING → CANCELLED；不可恢复/预算耗尽 → FAILED
```

- source_imports 的进度是下载 bytes_received/total_bytes；未知 Content-Length 使用不定进度。SUBMITTED 表示解析任务已登记，不等于解析成功。
- 登记时冻结获准配置和源引用；在 FETCHING/READY/提交解析/发布前复查当前主体、资源及网络授权。
- READY 后以 PG 事务创建 doc_version/job/outbox 并记录 parsing_job_id；`source_imports.parsing_job_id` 唯一，重复消息不能登记第二个解析任务。
- 下载/校验失败不得暴露 artifact 为可解析文件；临时文件和未完成 multipart 按 TTL 清理。取消 import 在已 SUBMITTED 时返回冲突与 job_id，解析取消走 job 接口。
- URL 重复并不表示文件相同；同 Idempotency-Key 同请求返回同一导入。换 URL、签名参数或选项需新 key；源内容以实际 SHA-256 建立身份。

### 5.2 SSRF、网络与内容安全

1. URL 解析拒绝 userinfo、控制字符、非 HTTP(S) scheme、歧义编码、非法端口与过长输入；fragment 不用于 HTTP 请求，签名 query 原样保留以免破坏签名。
2. 每一跳 DNS 解析均检查所有候选 IPv4/IPv6；阻止 loopback、link-local、元数据、私网、组播、未指定/保留地址与 IPv4-mapped IPv6 绕过。获准内网源必须使用单独受控 policy，不能全局 `allow_private=true`。
3. 连接使用已验证的目的 IP 并核对实际 peer，同时保持正确 Host/TLS SNI 和证书校验；DNS 检查与请求不能再次自由解析造成 rebinding。HTTP 客户端难以可靠实现时使用具备同等约束的出口代理，不以一次域名校验充数。
4. 禁止 HTTP 库自动无检查重定向；逐跳验证，受 max_redirects 限制。跨 origin 不转发 Authorization/Cookie/secret，HTTPS 降级默认拒绝。禁用隐式环境代理，显式代理也必须落实目标地址策略。
5. GET 流式下载；HEAD 仅作可选提示，不因不支持 HEAD 直接判失败。检查实际字节数、解压后字节、MIME、文件 magic 与可用磁盘，Content-Length/文件名均不可信；超限即中止。
6. 网络压缩与归档解包配额分开；限制 connect/read/total timeout、最低进度/停滞时间、下载并发和每租户总字节。下载器无云环境元数据凭据权限。
7. 服务端生成路径与对象键；Content-Disposition/URL 路径只作清洗后的展示名，不允许目录穿越、覆盖文件或 HTML 注入。下载到隔离存储后复用与上传一致的 MIME、恶意输入与解析沙箱检查。
8. 获取文件与解析内容分进程：安全 downloader 可访问获准网络，parser 默认无网络。URL 内含的远程图片/字体/Office 外链不得被 parser 二次自由请求。

`offline` 禁止公网 URL、云 OSS 与云模型，但允许明确批准的内网服务；`hybrid` 也必须满足各用途政策。URL 获取、存储和模型推理的 allowlist 独立，批准模型 endpoint 不等于批准该主机的任意文件路径。

### 5.3 私密 URL、重试与可追溯

- 预签名 URL 的 query 可能等价于密码；完整 URL 加密保存或放 SecretResolver，API/事件/访问日志只显示清洗后的 host/path，文件名也先净化。APM、反向代理日志和错误堆栈须覆盖脱敏规则。
- 请求幂等摘要包含完整 URL 的受保护摘要，不能把去掉 query 后的 URL 当唯一键；加密源引用在导入结束后按保留策略回收。审计保留安全来源摘要、最终 host、获取时间、大小、hash、校验结果，不持久返回完整签名 URL。
- 重试仅针对可恢复网络/限流故障，遵守总预算；签名已过期、403、内容非法等不盲目重试。用户提供新 URL 创建新导入，可关联原失败记录。
- 首期重试从头拉取；断点续传只在服务端持久化 checkpoint、强 ETag/对象版本与正确 Range/If-Range 验证齐备后启用，不能拼接两个版本的文件。ETag 只用于条件请求，不当 SHA-256。
- 首期不承诺同步 HTTP 请求内完成大文件解析；所有来源均走可恢复任务。更新已有文档必须校验 expected_revision，处理期间发生更新不得覆盖新版本。

## 6. 多对象存储适配与数据迁移

### 6.1 provider 与 profile 分离

`provider` 是代码实现类型，`profile` 是租户可引用的连接实例；同一租户可同时拥有多个 S3/OSS profile，不使用一个全局 `STORAGE_TYPE` 限制全系统。平台提供无密钥模板，实际连接和凭据归属明确租户；共享物理 bucket 时强制独立租户前缀。

| provider | 实现与必需配置 | 支持边界 |
| --- | --- | --- |
| local | LocalStorage；平台批准的 root_ref、配额、临时目录、fsync 策略 | 服务端流式读写、Range、持久分片；无对象云端签名，下载走应用代理；租户不能提交任意本机 root |
| s3 | S3Storage；endpoint、region、bucket、addressing_style、TLS、credential_ref | 使用锁定 boto3/botocore；AWS S3 与各兼容服务分别验收，兼容协议不保证全部特性相同 |
| aliyun_oss | AliyunOssStorage；region、endpoint、bucket、credential_ref、可选 STS | 原生 `alibabacloud-oss-v2` 候选 SDK；支持 OSS 分片、Range、签名及错误映射，不依赖 S3 兼容开关 |
| 后续 provider | COS/OBS/Azure 等独立 adapter | 复用端口与测试，不在业务代码新增 provider 分支 |

配置示例为管理 API 的无密钥 DTO，tenant_id 由上下文填入：

```yaml
name: aliyun-primary
provider: aliyun_oss
region: "<实际地域>"
endpoint: "<该地域获准的OSS endpoint>"
bucket: "<目标bucket>"
credential_ref: "<本租户的受控凭据引用>"
key_prefix_template: "tenants/{tenant_id}/artifacts/"
read_access: proxy
multipart_enabled: true
```

模板变量只允许服务端认可的 tenant_id/artifact_id 等，不执行表达式。内部 endpoint 与公开访问域名分开配置，禁止把云内网地址直接返回外部浏览器；自定义域名/TLS/签名也必须单独验证。

### 6.2 端口与调用链

以下为需实现的类型化接口契约，不是可直接运行的完整 adapter：

```python
class StorageBackend(Protocol):
    def capabilities(self) -> StorageCapabilities: ...
    def put(self, target: ObjectTarget, chunks: Iterable[bytes],
            options: PutOptions) -> StoredObject: ...
    def stat(self, locator: ObjectLocator) -> ObjectStat: ...
    def open_reader(self, locator: ObjectLocator,
                    byte_range: ByteRange | None) -> BinaryReader: ...
    def delete(self, locator: ObjectLocator) -> DeleteResult: ...

class MultipartStorage(Protocol):
    def initiate(self, target: ObjectTarget) -> MultipartHandle: ...
    def upload_part(self, handle: MultipartHandle, part_no: int,
                    chunks: Iterable[bytes]) -> PartReceipt: ...
    def complete(self, handle: MultipartHandle,
                 parts: list[PartReceipt]) -> StoredObject: ...
    def abort(self, handle: MultipartHandle) -> None: ...

class SignedReadStorage(Protocol):
    def presign_read(self, locator: ObjectLocator,
                     options: SignedReadOptions) -> SignedRead: ...
```

- `StorageCapabilities` 明确 range、multipart、presigned_read、versioning、server_side_copy 等；可选能力用独立协议，不能所有方法都返回 NotImplemented 后还标记 provider 已支持。
- `ObjectLocator` 至少含 profile_revision_id、bucket/root_ref、key、object_version；StoredObject 含字节数、已知 checksum、provider ETag。BinaryReader 必须支持显式 close/context manager，异常/取消也释放连接。
- `ArtifactService` 负责租户/主体授权、逻辑引用与选择 location，`StorageRouter` 按 profile revision 返回 adapter；业务模块只持 artifact_id，不拼接 `s3://`、OSS URL 或本地路径。
- 同步 SDK 通过有界执行器接入 API；流式下载支持背压，不先 `read()` 全文件再发送。Range 超界返回 416，支持 206/Content-Range/Accept-Ranges，首期拒绝多段 Range 的复杂组合并记录明确错误。
- 统一错误：StorageNotFound、StoragePermissionDenied、StorageUnavailable、StorageQuotaExceeded、ChecksumMismatch、UnsupportedCapability；日志保留脱敏 provider code，不向前端暴露云凭据或完整内部 URL。

### 6.3 物理位置、分片与一致性

- artifact 是不可变逻辑对象；artifact_locations 保存每个副本的 profile_revision、bucket/key、version、size、checksum、状态。SourceRef 只引用 artifact_id，不把临时签名 URL当永久来源。
- 新写入按数据集绑定的存储 profile 选址；已存在对象按 location 的原 revision 读取。修改默认 profile 只影响新对象，不会把旧 key 当成新 bucket 中的对象。
- 对象键由服务端生成并包含租户域；写入不覆盖已提交产物。Local 防止 symlink/路径穿越，以临时文件 + 完整校验 + 同文件系统原子发布实现不完整文件不可见；文件创建与重命名需防并发覆盖。
- 上传会话绑定 tenant、actor、dataset、profile revision、总大小、parts 与有效期；不同云有各自 part 大小/数量限制，初始化时从 provider 能力和平台配额计算，不能统一硬编码一套 S3 限制。
- complete 校验顺序、遗漏/重复 part 与总字节，成功后再生成 artifact；原始字节 SHA-256 在服务端接收/校验时计算。multipart ETag、CRC64 等不能当作 SHA-256；浏览器直传云端是后续可选能力，需额外校验未信任对象。
- SDK 自动重试纳入全局预算；失败的 multipart 由记录驱动中止与清理，清理任务只能操作本系统登记且过期的对象，不扫描并删除整个 bucket。
- 默认资源代理下载以保证撤权语义；短 TTL presign 仅为明确选择的低敏模式。OSS/S3 不设 public-read，最小 RAM/IAM 权限限定 bucket/prefix；STS 由 provider 受控刷新，不记录临时 secret。
- profile 停用分 `write_disabled` 与 `read_disabled`，关闭新写入不能意外破坏历史读取；存在引用时不允许硬删 profile/所需凭据。紧急密钥撤销可造成不可读，必须显式告警，不悄悄切错位置。

### 6.4 迁移、备份与验收

迁移流程：登记批次 → 读取原 location → 流式复制至目标 profile → 校验字节数/SHA-256 → 登记新 location → CAS 切 preferred_location → 保留旧位置至保留期结束。逐对象可重试/取消；复制不改变 artifact_id/source_refs，不以修改数据库中的 bucket 字符串假装完成迁移。

跨地域/外部云迁移受租户政策约束，估算请求/流量成本；归档冷存储恢复单独呈现状态。备份同时包含 PG 引用、对象及密钥恢复材料，不能只备份 PG 后宣称原文可恢复。

三种内置实现运行同一 contract suite：流式读写、空文件、Unicode 文件名、Range、分片失败恢复、checksum、取消清理、跨租户、旧 profile 读取。S3 测试服务不能代替真实 OSS 验收；无云测试凭据时明确标记该集成未验证，禁止在 capabilities 宣称已验证支持。

## 7. 大模型连接、能力模型与用途绑定

### 7.1 三层配置模型

| 对象 | 必需字段 | 作用 |
| --- | --- | --- |
| ModelConnection | id、tenant_id、protocol、vendor_label、base_url/endpoint_ref、region、credential_ref、TLS/CA、network_policy_ref、enabled、revision | 解决“连到哪里、如何认证”；不把厂商品牌等同于协议能力 |
| ModelProfile | connection_id、model_name、revision、capabilities、context_window、tokenizer_ref、max_input/output、supported_parameters、pricing_ref | 解决“调用哪个模型、能做什么”；embedding 另含 dimensions、normalization、distance、output_kinds |
| ModelBinding | tenant/dataset、purpose、primary_profile_id、fallback_profile_ids、prompt_revision、parameter_overrides、budget、revision | 解决“哪个业务使用哪个模型”；generation/vision/embedding/rerank/ASR 分开绑定 |

- 支持多个连接和模型同时存在；同一厂商可有不同地域、凭据与用途。用户只能选择已获准 profile，不能在业务请求传任意 base_url/key。
- M1 实现 OpenAI-compatible 连接/能力探测及最小调用，M3 接入 dense embeddings，M4 接入完整 chat completions/stream 编排；可配置百炼兼容接口、OpenAI、vLLM，以及已验证的 Ollama 兼容端点。不能因为 chat 可用就声明该端点支持 vision、rerank、sparse、tools 或全部 OpenAI API。
- 原生 Ollama/DashScope、Anthropic 等协议按独立 adapter 扩展，不用一套 JSON mapper 假装任意厂商通用。rerank 按独立能力后端实现，不能偷偷拿 chat completion 代替。
- 云模型的名称、地域 endpoint、请求头、超时、并发、生成参数与价格均来自配置；厂商例子不是可永久写死的 URL。默认不开 tools/function calling，文档内容不能启用工具。

### 7.2 类型与能力校验

```python
class GenerationBackend(Protocol):
    async def generate(self, request: GenerationRequest,
                       context: ModelCallContext) -> GenerationResult: ...
    def stream(self, request: GenerationRequest,
               context: ModelCallContext) -> AsyncIterator[GenerationEvent]: ...

class EmbeddingBackend(Protocol):
    async def embed(self, request: EmbeddingRequest,
                    context: ModelCallContext) -> EmbeddingResult: ...

class RerankBackend(Protocol):
    async def rerank(self, request: RerankRequest,
                     context: ModelCallContext) -> RerankResult: ...
```

这些引用类型必须在实现中补齐：请求含模型 profile revision、输入和参数；context 含 tenant/actor、deadline、取消和预算；结果含 provider_request_id、实际模型、usage、finish_reason 与 warnings。输出用 Pydantic/JSON Schema 校验，不把外部 JSON 原样贯穿业务层。

- context_window 与 tokenizer 必须明确；云端无法取得同 tokenizer 时采用显式标注的保守估算和服务端超限错误处理，不宣称本地计数绝对准确。
- temperature、top_p、max output、seed、结构化输出等按模型支持列表映射，未知参数拒绝或在保存前提示不支持，不静默丢字段；不同协议的 max_tokens/max_completion_tokens 由 adapter 负责转换。
- embedding 响应校验输入输出数量、对应顺序、维度、有限数值和 normalization；维度/模型/tokenizer/distance 任一改变必须新 index_generation/集合并重建，不能向旧集合混写。
- generation 模型切换仅影响新 run；embedding 改绑后，查询继续使用当前已发布索引绑定的旧 embedding profile，待新代全部就绪才切换。旧模型不可用时返回索引暂不可查询，不使用新模型查询旧向量空间。
- fallback 只在同一用途、能力/隐私/预算允许时显式设置；embedding 默认不跨不同模型降级；generation 已输出 delta 后不无痕切模型拼接回答。

### 7.3 连接测试、密钥与调用稳定性

- 测试拆为“配置验证”“网络/认证验证”“最小能力调用”；无标准健康接口时不能仅靠 GET `/models` 判定全部能力。模型列表可手动录入，也可选择拉取，列表失败不代表有效模型一定不可用。
- 最小调用使用内置非敏感样本，明确可能计费、输入输出上限、超时与预估预算；保存报告包含 profile revision、测试项、延迟、脱敏错误与测得维度，不记录密钥。
- key 只写不读；更换/清除单独操作。连接日志、HTTP debug、APM tracing 默认过滤 Authorization 和输入正文；模型端点也执行 SSRF/证书/出口政策检查。
- 超时细分 connect/read/total，重试只针对未开始有效输出的可恢复故障；429 遵守 Retry-After。超时可能已计费，无法证明幂等时不自动无限重发生成请求。
- 支持流式 UTF-8/分片、finish_reason、usage 缺失、取消和 provider_request_id；usage 未返回时标 `unknown/estimated`，不是 0 费用。价格配置注明版本、币种与计费单位，实际账单为准。
- 模型调用按 tenant/application/purpose 归因；外发须同时满足源数据、模型地域/保留政策与凭据授权。仅关闭平台 UI 按钮不能代替服务端检查。
- capability 报告区分 configured/validated/ready/degraded/disabled，不能凭“已保存 key”展示“模型可用”；所需模型缺失时给出明确设置入口，普通文件解析仍可运行。

## 8. API 完整清单与持久化细节

### 8.1 新增管理与接入接口

以下补充第二/三部分的业务 API；所有列表支持 cursor、过滤和稳定排序，所有可修改资源带 revision/ETag。创建/异步动作支持幂等，PATCH/DELETE 使用 If-Match；Token 一次性 secret 的重放例外见 §4.3。

| 接口 | 语义与授权 |
| --- | --- |
| GET `/v1/runtime-config` | 公开非敏感配置；不包含租户、模型连接或密钥 |
| POST `/v1/auth/login`、`/logout`；GET `/v1/auth/me` | Session 生命周期与本人信息；logout 可重复调用 |
| POST `/v1/auth/change-password` | 校验原密码并撤销旧 Session；新会话显式返回 |
| GET `/v1/auth/tenants` | 仅返回本人有效 memberships |
| POST `/v1/auth/ws-ticket` | 用户 Session 换租户/资源绑定、短时一次性 ticket；应用 Token 首期用事件 HTTP 查询 |
| GET/POST `/v1/platform/tenants`；PATCH `/v1/platform/tenants/{id}` | 平台管理员创建/修改/停用租户；不授予正文读取权 |
| POST `/v1/platform/users`；PATCH `/v1/platform/users/{id}` | 平台账号创建/停用；用户名规范化唯一，密码走安全流程 |
| GET/POST `/v1/members`；PATCH/DELETE `/v1/members/{id}` | 本租户成员管理；按已有完整用户名添加，最后 admin 防护 |
| GET/POST `/v1/datasets`；GET/PATCH/DELETE `/v1/datasets/{id}` | 数据集管理；删除异步清理并立即撤销读取 |
| GET `/v1/documents`；GET/PATCH/DELETE `/v1/documents/{id}` | 文档列表、命名、删除；正文/版本独立接口 |
| POST `/v1/uploads/init`；PUT `/v1/uploads/{id}/parts/{part_no}` | 绑定数据集/主体的持久上传会话与分片 |
| GET `/v1/uploads/{id}`；POST `/v1/uploads/{id}/complete`、`/cancel` | 返回已收 parts、完整校验或取消；完成只产 artifact |
| GET/POST `/v1/imports`；GET `/v1/imports/{id}` | 创建/查询 URL 或对象导入；POST 同时要求 imports:create/jobs:create |
| POST `/v1/imports/{id}/cancel`、`/retry` | imports:create + 本主体 + write grant；重试原输入/配置，换 URL 创建新 import |
| GET `/v1/parsing/jobs` | 当前租户有权任务列表，应用仅自身任务；分页支持 updated_after |
| GET `/v1/artifacts/{id}/content` | 受权流式资源，要求 document_id/version_id 关联；应用需 artifacts:read，非“知道ID即可下载” |
| GET `/v1/scopes` | 本租户管理员读取版本化 scope 元信息，前端不另造名单 |
| GET/POST `/v1/applications`；GET/PATCH `/v1/applications/{id}` | 本租户 admin 管理应用，停用不删除历史审计 |
| GET/PUT `/v1/applications/{id}/grants` | 数据集/actions 与 scopes 上限；PUT 原子校验并递增 auth_revision |
| GET/POST `/v1/applications/{id}/tokens` | Token 元信息列表/签发；只在首次签发返回 secret |
| POST `/v1/applications/{id}/tokens/{token_id}/revoke` | 幂等撤销；轮换采用签发新 Token + 显式撤销旧 Token |
| GET `/v1/applications/{id}/usage`、`/audit-events` | 调用量、限额与脱敏审计；不返回请求原文 |
| GET/POST `/v1/storage-profiles`；GET/PATCH `/v1/storage-profiles/{id}` | 管理存储配置，修改创建草稿 revision |
| GET/POST `/v1/source-locations`；PATCH `/v1/source-locations/{id}` | admin 配置数据集的对象导入 inbox；普通用户仅看有权使用的位置，不读凭据 |
| POST `/v1/storage-profiles/{id}/validate`、`/test`、`/publish` | 校验/显式 IO 测试/发布，测试不会隐式变成激活 |
| GET/POST `/v1/model-connections`；GET/PATCH `/v1/model-connections/{id}` | 管理模型连接；凭据 write-only |
| POST `/v1/model-connections/{id}/validate`、`/test`、`/publish` | 校验、显式连接/认证测试与发布分开，受预算与外发策略约束 |
| GET/POST `/v1/model-profiles`；GET/PATCH `/v1/model-profiles/{id}` | 模型能力及参数草稿；POST `/{id}/test`、`/publish` 分别测试与发布 |
| GET/PUT `/v1/model-bindings` | 租户默认用途绑定；数据集覆盖走数据集设置，带预期 revision |
| POST `/v1/secrets`；POST `/v1/secrets/{id}/rotate`、`/revoke` | 管理受控密钥，仅返回元信息；绑定租户/用途/端点，不提供读取明文接口 |
| GET `/v1/audit-events`、`/v1/settings/effective` | 本租户 admin 看脱敏审计与配置来源；绝不直接返回完整 settings 对象 |

完整 IR、独立原件/导出/可获取下载地址的 preview descriptor，应用同时需要 `documents:read + artifacts:read`；普通 blocks/chunks 正文由 documents:read 保护。不存在 document/version 关联的缓存产物不能通过通用资源接口读出；上传未发布对象仅供受控校验/解析内部流程使用。

SSE 的 chat:invoke 只授予本应用 run 的数据，不授予用户或其他应用 run。浏览器文档资源 URL 仍携带明确 document/version 上下文，代理以 Session 重验；响应 `private/no-store` 策略与撤权需求一致，不能被公共 CDN 缓存。

### 8.2 请求/响应一致性

- JSON DTO 使用 snake_case，时间为带时区 ISO 8601，大小用字节、时长用明确后缀单位；前端转换展示，不猜测 MB/MiB 或秒/毫秒。
- 列表固定 `{items, next_cursor, has_more}`；异步任务返回 202 与资源 ID/status_url。错误固定 `{code, message, detail, trace_id}`，detail 的字段错误使用路径，不塞原始 SDK 异常。
- 授权失败与输入失败不得创建对象、触发下载或扣模型预算；异步授权失效写明取消原因，不能归类为普通解析成功。
- 同一请求中的 dataset/document/artifact/pipeline/model/storage 引用都必须绑定正确租户和业务关联；逐个 ID 存在并不等于允许组合。
- 上传 multipart shortcut 与 URL API 复用 `SubmitParsingCommand`，禁止两套 document/job 创建逻辑。API 校验字段范围，application service 校验权限和业务不变量。
- 管理配置 revision 的发布须检查连接/模型/密钥均属同租户；平台准入上限由服务端施加，不能通过替换 secret_ref 绕过。

### 8.3 数据约束、事务与工作流

除前述表外，所有可变管理资源有 `revision、created_at、updated_at`；所有业务表按需记录 `created_by_kind/created_by_id`。应用和用户 ID 不混放在仅约束 users 的外键里。

必需约束：
- memberships `(tenant_id,user_id)` 唯一；应用名称 `(tenant_id,name)` 唯一，Token public_id 全局唯一但不泄露授权；grant `(tenant_id,application_id,dataset_id)` 唯一。
- idempotency_records 唯一域为 `(tenant_id,actor_kind,actor_id,operation,key)`；轮换 Token 不改变 application actor_id，同一个 key 不因换 Token 重复提交。
- artifacts/locations、profile/revision、document/version、dataset/application_grant 均使用租户一致的联合外键或等价事务约束，不允许跨租户关联。
- source_imports 的 artifact/job 绑定使用 CAS、唯一性与租约，发布和删除复用既有 fence/outbox 协议；API 不以进程内字典保存任务或 Token。
- 应用凭据状态、成员状态与 dataset.auth_revision 在副作用前重新检查；授权缓存必须有明确失效与 fail-closed 策略。

跨模块 workflow 示例：

```text
SubmitParsingCommand
  1. 事务外：解析输入、校验现有 artifact 的安全状态、解析生效配置引用
  2. PG 事务内：检查幂等与当前授权/预期文档 revision
  3. 调用 catalog 登记候选 version，jobs 登记任务，artifacts 建立引用
  4. 同事务写 outbox、幂等响应引用和 desired_version，提交
  5. 返回任务 ID；派发器独立投递，失败由 outbox 重试

PublishVersionCommand
  1. 读取已验证 manifest 和不可变产物引用，不在事务中进行网络推理
  2. PG 事务内锁定所需状态，复查 fence、取消、权限、desired_version
  3. CAS 切 visible_version/generation，登记事件/outbox，提交
```

上传/下载产生但未关联的对象由孤儿回收处理，不尝试让对象存储参与 PG 事务。应用服务只返回 DTO，不把 DB session 或 SDK client 放进 Celery 消息；队列只传 ID/有限元信息，worker 从权威库加载。

## 9. AI 生成顺序、测试与完成定义

### 9.1 纵向生成顺序

每一阶段以“契约 → 迁移/领域服务 → adapter → API → 页面 → 测试”闭环推进，不先生成全部空目录再宣称完成。禁止一次引入所有重型解析器或让所有页面只对 mock 工作。

| 顺序 | 必交付物 | 可运行验收 |
| --- | --- | --- |
| A 工程与契约 | uv/pnpm 锁文件、配置 Schema、错误/IR/Event、模块边界、应用工厂、Compose | 干净环境安装/构建成功，非法配置启动失败且不泄密 |
| B 多租户与鉴权 | migrations、用户 Session、tenant/membership、应用/Token/grants、审计、基础页面 | 两租户、三角色与两个应用交叉测试；撤销/CSRF/越权全覆盖 |
| C 统一接入 | artifact 模块、三种存储、上传、SafeFetcher、source_imports、任务恢复 | 本地/URL/对象导入完整落盘；重定向/SSRF/超限拒绝，无重复 job |
| D 最小解析 | TXT/MD → PDF/Office、pipeline、IR、版本发布、工作台 | 来源定位、失败回退、重试/取消、刷新恢复；不使用假进度 |
| E 模型与检索 | 连接/profile/binding、embedding/rerank、分块与索引 | 模型测试与维度校验，旧索引切换安全，人工编辑及引用可追溯 |
| F 问答与扩展 | run/SSE、证据、媒体/高级解析/画布 | 断流不重复计费、伪引用拒绝、按能力逐项验收 |

M1 的模型设置页和连接协议先交付；E 阶段才要求检索应用接入，F 阶段才要求完整 RAG 对话。高级存储迁移、SSO、更多云 provider 与画布可后置，但 Local/S3/OSS 和外部 Token 不是空接口占位项。

### 9.2 测试矩阵补充

| 领域 | 必测用例 |
| --- | --- |
| 配置/构建 | 优先级、未知键、缺密钥、非法组合、disabled 能力、uv 锁漂移、离线包缺依赖、runtime-config 脱敏 |
| 模块 | 禁止跨模块 ORM 写入、反向依赖、业务 import 时副作用；端口 fake 与真实 adapter 共用契约 |
| 多租户 | 用户切换租户、伪造 X-Tenant-ID、所有 ID 组合跨租户、应用 A/B、缓存/事件/下载/索引隔离 |
| Token | 到期、手动撤销、停用应用、scope 缺失、grant 撤销、轮换、secret 只显示一次、签发响应丢失、限额并发 |
| URL | DNS rebinding、IPv6/混合地址、重定向内网、带凭据跨域、缺 HEAD/Content-Length、超大/慢速/压缩响应、签名过期 |
| 上传/存储 | 分片重放/遗漏、断线/取消、provider 异常、Range、SHA-256、旧 profile、STS 过期、跨租户 key、迁移中断 |
| 模型 | chat-only 端点、无 models API、未知参数、错误维度、NaN、超时/429、usage 缺失、已输出后失败、外发拒绝 |
| 一致性 | READY 重复提交、旧 fence、部分索引、撤权时发布、同文档重解析竞争、删除时异步 upsert、备份恢复 |
| 前端 | 真实接口闭环、所有异常态、快速切租户、Token 弹窗清理、模型改绑提示、URL 脱敏、无障碍与大文件 |

测试分层：unit 无外网；contract 使用固定合法/非法 fixture；integration 使用隔离 PG/Valkey/检索服务及专用对象前缀；真实 OSS/云模型测试显式 opt-in、预算受限，不因运行常规测试产生费用。

无法访问云测试账户时，本地契约测试可以通过，但报告必须把真实云链路标为“未验证”，不能伪造成功。LLM 输出质量使用留出集评估，不把固定 fake 模型输出当真实质量验证。

### 9.3 完成清单与部署健康

一个模块只有同时交付以下内容才算完成：
1. 类型/Schema、迁移/约束、权限矩阵和配置字段，无未定义类型或隐式默认。
2. 真实业务实现、已接入 adapter、事务/幂等、失败/取消/清理路径；Protocol 中的 `...` 可以存在，实现类的关键路径不得 `pass`/TODO/返回 mock success。
3. OpenAPI 与事件生成快照、前端 API client、页面/组件、空态/错误/权限/冲突交互；不能只交“好看的首页”。
4. 单元、契约、集成、安全与 E2E 用例，并记录实际运行结果和未验证项；测试失败不能以禁用断言完成交付。
5. defaults/env 示例、部署 profile、健康检查、迁移/备份/恢复步骤、依赖与模型许可材料；真实 secret 不进入提交内容。

健康检查分 liveness（进程存活）与 readiness（该进程必需的共享 DB/broker/配置可用）；某租户云存储/模型或可选检索模块故障仅影响其 profile/capability，不能导致其他租户基础 API 全部不就绪。平台监控提供 worker 心跳、最老任务、outbox lag、磁盘和 Token 拒绝统计，不开放匿名敏感配置。

CI 必须检查：格式/静态类型 → unit/contract → migrations 与多租户集成 → API 生成差异 → 前端构建/组件 → 关键 E2E → 依赖/密钥扫描。部署先迁移，再发布兼容应用/worker，排空旧 worker 后升级；破坏性 schema 变更不得依赖直接覆盖生产表。

### 9.4 建议给代码生成执行者的约束

“按当前阶段实现 owli-parser 的真实纵向闭环；Python 用 uv，配置集中且密钥独立；保留租户隔离，用户管理只做固定角色。外部应用必须 Token + scope + 数据集授权，不使用全局共享 key。业务代码只依赖 storage/model/fetcher 端口，Local/S3/阿里云 OSS 各有实现；上传和 URL 最终进入同一任务服务。前端必须真实联调并覆盖失败/冲突/断线/撤权。每完成一段运行相应测试，报告修改、命令、结果和未验证能力，不把设计草图或 mock 当成已交付功能。”
