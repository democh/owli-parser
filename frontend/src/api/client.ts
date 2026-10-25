/**
 * 统一 API client：CSRF、X-Tenant-ID、trace_id、错误体与 AbortSignal。
 * - 错误固定 {code, message, detail, trace_id}；
 * - 401 清会话并导航登录，412 保留草稿（由调用方处理），429 按 Retry-After。
 * - 写入体以 generated.ts（OpenAPI 快照生成）为编译期事实源：新写操作一律经
 *   契约守卫函数（identity，运行时零开销）或 satisfies 校验，字段漂移由 tsc 拦截。
 */
import type { components } from './generated';

type Schemas = components['schemas'];

/** 契约守卫：以生成类型校验写入体，返回同一对象（编译期检查，运行时零开销）。 */
export const datasetCreateBody = (body: Schemas['DatasetCreate']): Schemas['DatasetCreate'] =>
  body;
export const chunkPatchBody = (body: Schemas['ChunkPatch']): Schemas['ChunkPatch'] => body;


export interface ApiErrorBody {
  code: string;
  message: string;
  detail: Record<string, unknown>;
  trace_id: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: Record<string, unknown>;
  readonly traceId: string;
  readonly retryAfterSeconds: number | null;

  constructor(status: number, body: ApiErrorBody, retryAfterSeconds: number | null) {
    super(body.message);
    this.status = status;
    this.code = body.code;
    this.detail = body.detail;
    this.traceId = body.trace_id;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  tenantId?: string | null;
  csrfToken?: string | null;
  idempotencyKey?: string;
  ifMatch?: number | string | null;
  signal?: AbortSignal;
}

let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

const SAFE_METHODS = new Set(['GET', 'HEAD']);

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (options.tenantId) {
    headers['X-Tenant-ID'] = options.tenantId;
  }
  if (options.csrfToken && !SAFE_METHODS.has(method)) {
    headers['X-OWLI-CSRF'] = options.csrfToken;
  }
  if (options.idempotencyKey) {
    headers['Idempotency-Key'] = options.idempotencyKey;
  }
  if (options.ifMatch !== undefined && options.ifMatch !== null) {
    headers['If-Match'] = String(options.ifMatch);
  }

  const response = await fetch(`/v1${path}`, {
    method,
    headers,
    credentials: 'same-origin', // 同源 Cookie Session
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });

  if (response.status === 204) {
    return undefined as T;
  }

  const isJson = response.headers.get('content-type')?.includes('application/json') ?? false;
  if (!response.ok) {
    const retryAfter = response.headers.get('retry-after');
    if (isJson) {
      const body = (await response.json()) as ApiErrorBody;
      if (response.status === 401 && onUnauthorized) {
        onUnauthorized();
      }
      throw new ApiError(
        response.status,
        body,
        retryAfter ? Number(retryAfter) : null,
      );
    }
    throw new ApiError(
      response.status,
      { code: 'http_error', message: `请求失败（${response.status}）`, detail: {}, trace_id: '' },
      retryAfter ? Number(retryAfter) : null,
    );
  }
  return (await response.json()) as T;
}

/* ---- 契约类型（与后端 DTO 对齐；正式生成链见 openapi-typescript） ---- */

export interface UserMe {
  user_id: string;
  username: string;
  display_name: string;
  is_platform_admin: boolean;
}

export interface LoginResponse extends UserMe {
  csrf_token: string;
  session_expires_at: string;
}

export interface TenantEntry {
  tenant_id: string;
  name: string;
  role: string;
  status: string;
  tenant_status: string;
}

export interface Dataset {
  id: string;
  name: string;
  description: string | null;
  visibility: string;
  owner_user_id: string;
  revision: number;
  allow_partial: boolean;
  created_at: string;
}

export interface DocumentItem {
  id: string;
  dataset_id: string;
  filename: string;
  mime: string | null;
  size_bytes: number | null;
  visible_version_id: string | null;
  desired_version_id: string | null;
  revision: number;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobItem {
  id: string;
  document_id: string;
  version_id: string;
  dataset_id: string;
  state: string;
  stage: string | null;
  attempt: number;
  progress: Record<string, unknown>;
  total_units: number | null;
  completed_units: number;
  coverage: number | null;
  error: { code?: string; message?: string } | null;
  retrieval_ready: boolean;
  status_url: string;
}

export interface ChunkItem {
  id: string;
  version_id: string;
  doc_id: string;
  ordinal: number;
  kind: string;
  parent_id: string | null;
  revision: number;
  text: string;
  token_count: number;
  source_refs: Array<Record<string, unknown>>;
  explanation: Record<string, unknown> | null;
  enabled: boolean;
  embedding_state: string;
}

export interface ListPage<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface ImportItem {
  id: string;
  dataset_id: string;
  source_kind: string;
  display_host: string;
  display_path: string;
  state: string;
  attempt: number;
  bytes_received: number;
  total_bytes: number | null;
  artifact_id: string | null;
  parsing_job_id: string | null;
  error: { code?: string; message?: string } | null;
}

export interface Capabilities {
  tenant_id: string;
  formats: string[];
  retrieval_enabled: boolean;
  network_mode: string;
  limits: {
    upload_direct_max_bytes: number;
    upload_total_max_bytes: number;
    part_size_bytes: number;
  };
}

/* ---- 设置管理契约（应用/Token、存储 profile、模型连接） ---- */

export interface ScopeDefinition {
  name: string;
  description: string;
  category: string;
  constraints: string;
  requires_dataset_grant: boolean;
}

export interface ScopeRegistry {
  registry_version: string;
  items: ScopeDefinition[];
  roles: string[];
}

export interface ApplicationItem {
  id: string;
  name: string;
  description: string | null;
  status: string;
  allowed_scopes: string[];
  auth_revision: number;
}

export interface GrantItem {
  dataset_id: string;
  actions: string[];
}

export interface TokenItem {
  token_id: string;
  public_id: string;
  name: string;
  scopes: string[];
  dataset_ids: string[];
  expires_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
  status: 'active' | 'revoked' | 'expired';
}

/** 签发响应：明文 token 只在本次响应出现（Cache-Control: no-store）。 */
export interface IssuedToken {
  token_id: string;
  public_id: string;
  token: string;
  expires_at_epoch: number;
  scopes: string[];
  dataset_ids: string[];
  secret_available: boolean;
}

export interface UsageItem {
  operation: string;
  method: string;
  path: string;
  status_code: number;
  decision: string | null;
  request_id: string;
  created_at: string;
}

export interface StorageProfile {
  id: string;
  name: string;
  provider: 'local' | 's3' | 'aliyun_oss';
  revision: number;
  state: string;
  config: Record<string, unknown>;
  read_disabled: boolean;
  write_disabled: boolean;
  created_at: string;
}

export interface ValidationReport {
  ok: boolean;
  errors: string[];
  warnings: string[];
}

export interface StorageTestReport {
  ok: boolean;
  steps: Record<string, string>;
  notes: string[];
}

export interface ModelConnection {
  id: string;
  name: string;
  protocol: string;
  vendor_label: string | null;
  base_url: string;
  region: string | null;
  revision: number;
  state: string;
  credential_configured: boolean;
  last_test_report: Record<string, unknown> | null;
  created_at: string;
}

export interface ModelTestReport {
  ok: boolean;
  steps: Record<string, string>;
  latency_ms: number | null;
  notes: string[];
}

/** 模型能力档案（不可变；用途绑定只引用已发布档案）。 */
export interface ModelProfile {
  id: string;
  connection_id: string;
  model_name: string;
  revision: number;
  capabilities: string[];
  context_window: number | null;
  tokenizer_ref: string | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  supported_parameters: string[];
  pricing_ref: string | null;
  embedding_dimensions: number | null;
  embedding_normalization: string | null;
  embedding_distance: string | null;
  output_kinds: string[];
  state: string;
}

/** 用途绑定：purpose → primary/fallback 档案 + 自由预算 JSON。 */
export interface ModelBinding {
  id: string;
  purpose: string;
  primary_profile_id: string;
  primary_model_name: string | null;
  fallback_profile_ids: string[];
  revision: number;
  budget: Record<string, unknown>;
}

export function modelProfiles(tenantId: string): Promise<ModelProfile[]> {
  return request<ModelProfile[]>('/model-profiles', { tenantId });
}

export function modelBindings(tenantId: string): Promise<ModelBinding[]> {
  return request<ModelBinding[]>('/model-bindings', { tenantId });
}

/** 首次创建免 If-Match，更新须携带当前 revision；budget=null 表示不修改。 */
export function saveModelBinding(params: {
  purpose: string;
  primaryProfileId: string;
  expectedRevision: number | null;
  budget: Record<string, unknown> | null;
  tenantId: string;
  csrfToken?: string | null;
}): Promise<ModelBinding> {
  return request<ModelBinding>(`/model-bindings/${params.purpose}`, {
    method: 'PUT',
    tenantId: params.tenantId,
    csrfToken: params.csrfToken,
    ifMatch: params.expectedRevision ?? undefined,
    body: {
      primary_profile_id: params.primaryProfileId,
      fallback_profile_ids: [],
      ...(params.budget !== null ? { budget: params.budget } : {}),
    },
  });
}

export interface EffectiveSettings {
  tenant_id: string;
  sources: string[];
  summary: {
    environment: string;
    network_mode: string;
    retrieval_enabled: boolean;
    default_pipeline: string;
    storage_default_profile: string;
    local_roots: Array<{ id: string; path: string; quota_bytes: number | null }>;
  };
}

// ---------------------------------------------------------------------------
// 召回测试台（M3 检索通道；未启用时服务端返回 capability_not_ready）
// ---------------------------------------------------------------------------

export interface RetrievalTestItem {
  id: string;
  rrf_score: number;
  channel_ranks: Record<string, number>;
  channel_raw_scores: Record<string, number>;
  channel_contributions: Record<string, number>;
  rerank_score: number | null;
  payload: Record<string, unknown>;
}

export interface RetrievalTestResponse {
  items: RetrievalTestItem[];
  channels_used: string[];
  applied_filters: {
    tenant_id: string;
    dataset_ids: string[] | null;
    acl_refs: string[] | null;
  };
  low_relevance: boolean;
  warnings: string[];
  applied_params: {
    top_k: number;
    per_channel_limit: number;
    candidate_limit: number;
    rrf_k: number;
    channel_weights: Record<string, number>;
    min_relevance_threshold: number;
    /** 权威校验后的重排是否生效（未配置 rerank 绑定时为 false）。 */
    rerank_applied?: boolean;
  };
}

/** 召回测试台可调参数（缺省取服务端配置；candidate_limit 不可覆盖）。 */
export interface RetrievalTunables {
  top_k?: number;
  per_channel_limit?: number;
  rrf_k?: number;
  channel_weights?: Record<string, number>;
  min_relevance_threshold?: number;
}

export async function retrievalTest(params: {
  query: string;
  dataset_ids: string[];
  tunables?: RetrievalTunables;
  tenantId: string;
  csrfToken?: string | null;
}): Promise<RetrievalTestResponse> {
  const { query, dataset_ids, tunables = {}, tenantId, csrfToken } = params;
  return request<RetrievalTestResponse>('/retrieval/test', {
    method: 'POST',
    tenantId,
    csrfToken,
    body: { query, dataset_ids, ...tunables } satisfies Schemas['RetrievalTestRequest'],
  });
}

// ---------------------------------------------------------------------------
// 复核队列（低置信 / 缺失单位人工审核；处置为 CAS，重复处置返回 409）
// ---------------------------------------------------------------------------

export interface ReviewItem {
  id: string;
  version_id: string;
  document_id: string | null; // 供直接跳转工作台定位
  job_id: string | null;
  block_id: string;
  reason: string;
  status: string;
  reviewer_id: string | null;
  resolution: Record<string, unknown> | null;
  created_at: string;
}

export const REVIEW_ACTIONS = [
  { value: 'accept', label: '接受当前结果' },
  { value: 'fix', label: '人工修正' },
  { value: 'rerun_page', label: '重跑该页' },
  { value: 'switch_adapter', label: '切换适配器' },
  { value: 'dismiss', label: '忽略（记录理由）' },
] as const;

export function reviewQueue(params: {
  status: string;
  tenantId: string;
  cursor?: string | null;
  reason?: string | null;
  versionId?: string | null;
}): Promise<ListPage<ReviewItem>> {
  const query = new URLSearchParams({ status: params.status, limit: '50' });
  if (params.cursor) query.set('cursor', params.cursor);
  if (params.reason) query.set('reason', params.reason);
  if (params.versionId) query.set('version_id', params.versionId);
  return request<ListPage<ReviewItem>>(`/review/queue?${query.toString()}`, {
    tenantId: params.tenantId,
  });
}

export async function resolveReviewItem(params: {
  itemId: string;
  action: string;
  note: string | null;
  tenantId: string;
  csrfToken?: string | null;
}): Promise<{ id: string; status: string }> {
  return request<{ id: string; status: string }>(
    `/review/queue/${params.itemId}/resolve`,
    {
      method: 'POST',
      tenantId: params.tenantId,
      csrfToken: params.csrfToken,
      body: {
        action: params.action,
        ...(params.note ? { note: params.note } : {}),
      } satisfies Schemas['ReviewResolve'],
    },
  );
}

// ---------------------------------------------------------------------------
// 质量回归（黄金集运行历史；仅指标与判定，不含被检索内容）
// ---------------------------------------------------------------------------

export interface RegressionRun {
  id: string;
  golden_set_id: string;
  golden_set_name: string | null;
  run_id: string;
  adapter_ref: string;
  config_digest: string | null;
  state: string; // passed / failed
  created_at: string;
  aggregate: Record<string, number>;
  passed: boolean | null;
  violations: string[];
}

export interface RegressionRunResult {
  run_id: string;
  passed: boolean;
  violations: string[];
  aggregate: Record<string, number>;
}

/** 触发同步回归（tenant admin；结果落 golden_runs）；超限请用 CLI。 */
export function triggerRegressionRun(params: {
  goldenSetId: string;
  tenantId: string;
  csrfToken?: string | null;
}): Promise<RegressionRunResult> {
  return request<RegressionRunResult>('/quality/regression-runs', {
    method: 'POST',
    tenantId: params.tenantId,
    csrfToken: params.csrfToken,
    body: { golden_set_id: params.goldenSetId },
  });
}

export function regressionRuns(params: {
  tenantId: string;
  limit?: number;
  goldenSetId?: string | null;
}): Promise<{ items: RegressionRun[] }> {
  const query = new URLSearchParams({ limit: String(params.limit ?? 50) });
  if (params.goldenSetId) query.set('golden_set_id', params.goldenSetId);
  return request<{ items: RegressionRun[] }>(`/quality/regression-runs?${query.toString()}`, {
    tenantId: params.tenantId,
  });
}

/** 单次回归运行详情：含逐 case 的指标（不含被检索正文）。 */
export interface RegressionCase {
  /** 解析回归：原始序号与版本对；检索回归：query 与 ranked_ids。 */
  case?: number;
  query?: string;
  ranked_ids?: string[];
  reference_version_id?: string;
  hypothesis_version_id?: string;
  groups?: Record<string, string>;
  metrics?: Record<string, number>;
  [key: string]: unknown;
}

export interface RegressionRunDetail extends RegressionRun {
  cases: RegressionCase[];
}

export function regressionRun(runId: string, tenantId: string): Promise<RegressionRunDetail> {
  return request<RegressionRunDetail>(`/quality/regression-runs/${runId}`, { tenantId });
}

// ---------------------------------------------------------------------------
// 解析管道（版本化配置；PUT 要求 If-Match）
// ---------------------------------------------------------------------------

export interface PipelineSummary {
  name: string;
  version: number;
  active: boolean;
  digest: string;
  source: 'builtin' | 'tenant';
}

export interface PipelineDetail extends PipelineSummary {
  yaml: string;
  execution_plan: Record<string, unknown>;
}

export function pipelines(tenantId: string): Promise<PipelineSummary[]> {
  return request<PipelineSummary[]>('/pipelines', { tenantId });
}

export function pipelineDetail(name: string, tenantId: string): Promise<PipelineDetail> {
  return request<PipelineDetail>(`/pipelines/${name}`, { tenantId });
}

/** adapter 清单：内置（恒 ready）+ 外部注册（心跳时效判定）。 */
export interface AdapterInfo {
  name: string;
  version: string;
  mode: string;
  health: string;
  reported_health?: string;
  heartbeat_at?: string | null;
  stale?: boolean;
  source?: 'builtin' | 'registered';
  config_ref?: string | null;
  capabilities?: Record<string, unknown>;
}

export function adapters(tenantId: string): Promise<AdapterInfo[]> {
  return request<AdapterInfo[]>('/adapters', { tenantId });
}

/** 注册/更新外部 adapter（平台管理员；内置同名 409）。 */
export interface AdapterRegisterInput {
  name: string;
  version: string;
  mode: string;
  health: string;
  configRef?: string | null;
  capabilities: Record<string, unknown>;
}

export function registerAdapter(
  params: AdapterRegisterInput & { tenantId: string; csrfToken?: string | null },
): Promise<AdapterInfo> {
  return request<AdapterInfo>(`/adapters/${encodeURIComponent(params.name)}`, {
    method: 'PUT',
    tenantId: params.tenantId,
    csrfToken: params.csrfToken,
    body: {
      version: params.version,
      mode: params.mode,
      capabilities: params.capabilities,
      config_ref: params.configRef ?? null,
      health: params.health,
    },
  });
}

/** 外部 adapter 心跳/readiness 上报（未注册 404）。 */
export function adapterHeartbeat(params: {
  name: string;
  health: string;
  version?: string | null;
  tenantId: string;
  csrfToken?: string | null;
}): Promise<AdapterInfo> {
  return request<AdapterInfo>(`/adapters/${encodeURIComponent(params.name)}/heartbeat`, {
    method: 'POST',
    tenantId: params.tenantId,
    csrfToken: params.csrfToken,
    body: {
      health: params.health,
      ...(params.version ? { version: params.version } : {}),
    },
  });
}

export function savePipeline(params: {
  name: string;
  yamlText: string;
  expectedVersion: number;
  tenantId: string;
  csrfToken?: string | null;
}): Promise<{ name: string; version: number; digest: string; active: boolean }> {
  return request(`/pipelines/${params.name}`, {
    method: 'PUT',
    tenantId: params.tenantId,
    csrfToken: params.csrfToken,
    ifMatch: params.expectedVersion,
    body: { yaml_text: params.yamlText },
  });
}

/** 草稿校验（不保存）：ok=false 时 errors 为服务端可读原因。 */
export interface PipelineValidationResult {
  ok: boolean;
  errors: string[];
  name?: string;
  version?: number;
  digest?: string;
  execution_plan?: Record<string, unknown>;
}

export function validatePipeline(params: {
  name: string;
  yamlText: string;
  tenantId: string;
  csrfToken?: string | null;
}): Promise<PipelineValidationResult> {
  return request<PipelineValidationResult>(`/pipelines/${params.name}/validate`, {
    method: 'POST',
    tenantId: params.tenantId,
    csrfToken: params.csrfToken,
    body: { yaml_text: params.yamlText },
  });
}

/** dry-run 路由解释：只做预检与 first_success 解释，不执行 adapter。 */
export interface PipelineDryRunResult {
  ok: boolean;
  pipeline: {
    name: string;
    version: number;
    digest: string;
    source: 'draft' | 'tenant' | 'builtin';
  };
  format: string;
  features: {
    mime: string | null;
    extension: string;
    size_bytes: number;
    text_layer_ratio: number | null;
    page_count: number | null;
    encrypted: boolean | null;
    has_zip_container: boolean | null;
    language_hint: string | null;
    extra: Record<string, unknown>;
  };
  route: {
    mode: string;
    adapter: string | null;
    reason: string;
    skipped: Array<{ adapter: string; reason: string }>;
  };
  warnings: string[];
}

export function pipelineDryRun(params: {
  name: string;
  yamlText?: string;
  versionId?: string;
  artifactId?: string;
  filename?: string;
  tenantId: string;
  csrfToken?: string | null;
}): Promise<PipelineDryRunResult> {
  return request<PipelineDryRunResult>(`/pipelines/${params.name}/dry-run`, {
    method: 'POST',
    tenantId: params.tenantId,
    csrfToken: params.csrfToken,
    body: {
      ...(params.yamlText !== undefined ? { yaml_text: params.yamlText } : {}),
      ...(params.versionId ? { version_id: params.versionId } : {}),
      ...(params.artifactId ? { artifact_id: params.artifactId } : {}),
      ...(params.filename ? { filename: params.filename } : {}),
    },
  });
}

// ---------------------------------------------------------------------------
// 问答（M4：SSE 流式生成；事件按 seq 持久化，断流可按 after_seq 重放）
// ---------------------------------------------------------------------------

export interface ChatEvidence {
  evidence_id: string;
  chunk_id: string;
  version_id: string;
  chunk_revision: number;
  text_preview: string;
}

/** SSE data 行解析后的事件；POST 流与事件重放（GET）字段一致（run_id 仅流内出现）。 */
export interface ChatStreamEvent {
  seq: number;
  type: string;
  run_id?: string;
  payload: Record<string, unknown>;
}

export interface ChatEventItem {
  seq: number;
  type: string;
  payload: Record<string, unknown>;
}

export interface ChatRunInfo {
  id: string;
  state: string;
  dataset_ids: string[];
  request_snapshot: Record<string, unknown>;
  error: Record<string, unknown> | null;
}

/**
 * SSE 缓冲解析（纯函数）：按空行切分事件并拼接 data: 行；
 * 不完整尾块保留在 rest 中，与下一个 chunk 续拼（跨 chunk 边界安全）。
 */
export function parseSseBuffer(buffer: string): { events: string[]; rest: string } {
  const blocks = buffer.replace(/\r\n/g, '\n').split('\n\n');
  const rest = blocks.pop() ?? '';
  const events: string[] = [];
  for (const block of blocks) {
    const data = block
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).replace(/^ /, ''));
    if (data.length > 0) events.push(data.join('\n'));
  }
  return { events, rest };
}

/**
 * 创建问答运行并消费 SSE 事件流（data: json；[DONE] 结束）。
 * - 调用方 abort 即断流：服务端会把 run 标记为 cancelled（断开即取消）；
 * - 畸形数据行跳过，不中断整条流；非 2xx 响应映射为 ApiError。
 */
export async function streamChatRun(params: {
  question: string;
  datasetIds: string[];
  parentRunId?: string | null;
  tenantId: string;
  csrfToken?: string | null;
  signal?: AbortSignal;
  onEvent: (event: ChatStreamEvent) => void;
}): Promise<void> {
  const headers: Record<string, string> = {
    Accept: 'text/event-stream',
    'Content-Type': 'application/json',
  };
  if (params.tenantId) headers['X-Tenant-ID'] = params.tenantId;
  if (params.csrfToken) headers['X-OWLI-CSRF'] = params.csrfToken;
  const body = {
    question: params.question,
    dataset_ids: params.datasetIds,
    ...(params.parentRunId ? { parent_run_id: params.parentRunId } : {}),
  } satisfies Schemas['ChatRunCreate'];

  const response = await fetch('/v1/chat/runs', {
    method: 'POST',
    headers,
    credentials: 'same-origin',
    body: JSON.stringify(body),
    signal: params.signal,
  });

  if (!response.ok) {
    const retryAfter = response.headers.get('retry-after');
    const isJson = response.headers.get('content-type')?.includes('application/json') ?? false;
    if (isJson) {
      const errorBody = (await response.json()) as ApiErrorBody;
      if (response.status === 401 && onUnauthorized) onUnauthorized();
      throw new ApiError(response.status, errorBody, retryAfter ? Number(retryAfter) : null);
    }
    throw new ApiError(
      response.status,
      { code: 'http_error', message: `请求失败（${response.status}）`, detail: {}, trace_id: '' },
      retryAfter ? Number(retryAfter) : null,
    );
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error('响应不含可读流');
  const decoder = new TextDecoder();
  let buffer = '';

  const consume = (dataLines: string[]): boolean => {
    for (const data of dataLines) {
      if (data === '[DONE]') return true;
      try {
        params.onEvent(JSON.parse(data) as ChatStreamEvent);
      } catch {
        // 契约外数据行跳过（不阻断已完成事件）
      }
    }
    return false;
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSseBuffer(buffer);
      buffer = parsed.rest;
      if (consume(parsed.events)) return;
    }
    buffer += decoder.decode();
    if (buffer.trim()) consume(parseSseBuffer(`${buffer}\n\n`).events);
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

export function chatRun(runId: string, tenantId: string): Promise<ChatRunInfo> {
  return request<ChatRunInfo>(`/chat/runs/${runId}`, { tenantId });
}

/** 事件重放：after_seq 之后的事件（断流恢复时不重复已接收事件）。 */
export function chatRunEvents(params: {
  runId: string;
  tenantId: string;
  afterSeq?: number;
}): Promise<ChatEventItem[]> {
  const query = new URLSearchParams({ after_seq: String(params.afterSeq ?? 0) });
  return request<ChatEventItem[]>(`/chat/runs/${params.runId}/events?${query.toString()}`, {
    tenantId: params.tenantId,
  });
}

/** 幂等取消：非终态置 cancelled，生成循环在下一检查点结束并发出 run.cancelled。 */
export function cancelChatRun(
  runId: string,
  tenantId: string,
  csrfToken?: string | null,
): Promise<ChatRunInfo> {
  return request<ChatRunInfo>(`/chat/runs/${runId}/cancel`, {
    method: 'POST',
    tenantId,
    csrfToken,
  });
}

/** 单位级解析步骤（页/时间窗/文档），按阶段、单位与 attempt 排序。 */
export interface JobStepItem {
  stage: string;
  unit_id: string;
  attempt: number;
  state: string;
  checkpoint_ref: Record<string, unknown> | null;
  error_code: string | null;
  started_at: string;
  finished_at: string | null;
}

export function jobSteps(params: {
  jobId: string;
  tenantId: string;
  stage?: string | null;
  limit?: number;
}): Promise<{ items: JobStepItem[]; total: number; limit: number }> {
  const query = new URLSearchParams({ limit: String(params.limit ?? 1000) });
  if (params.stage) query.set('stage', params.stage);
  return request<{ items: JobStepItem[]; total: number; limit: number }>(
    `/parsing/jobs/${params.jobId}/steps?${query.toString()}`,
    { tenantId: params.tenantId },
  );
}

// ---------------------------------------------------------------------------
// 管线 A/B 实验（按 artifact 稳定分桶；变体 YAML 冻结进任务快照）
// ---------------------------------------------------------------------------

export interface PipelineExperiment {
  id: string;
  pipeline_name: string;
  name: string;
  traffic_percent: number;
  active: boolean;
  revision: number;
  variant_a_digest: string;
  variant_b_digest: string;
  variant_a_yaml: string;
  variant_b_yaml: string;
  created_at: string;
  updated_at: string;
}

/** 未配置实验时返回 null（404 不是错误态）。 */
export async function pipelineExperiment(
  name: string,
  tenantId: string,
): Promise<PipelineExperiment | null> {
  try {
    return await request<PipelineExperiment>(`/pipelines/${name}/experiment`, { tenantId });
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export function savePipelineExperiment(params: {
  pipelineName: string;
  name: string;
  variantAYaml: string;
  variantBYaml: string;
  trafficPercent: number;
  active: boolean;
  tenantId: string;
  csrfToken?: string | null;
}): Promise<PipelineExperiment> {
  return request<PipelineExperiment>(`/pipelines/${params.pipelineName}/experiment`, {
    method: 'PUT',
    tenantId: params.tenantId,
    csrfToken: params.csrfToken,
    body: {
      name: params.name,
      variant_a_yaml: params.variantAYaml,
      variant_b_yaml: params.variantBYaml,
      traffic_percent: params.trafficPercent,
      active: params.active,
    },
  });
}

export interface PipelineExperimentVariantStats {
  jobs: number;
  done: number;
  partial_done: number;
  failed: number;
  cancelled: number;
  in_progress: number;
  avg_coverage: number | null;
  avg_elapsed_seconds: number | null;
  /** 可归因到本变体产物的回归 case 数与比例类指标均值。 */
  quality_cases: number;
  quality_metrics: Record<string, number>;
}

export interface PipelineExperimentReport {
  experiment: {
    id: string;
    pipeline_name: string;
    name: string;
    revision: number;
    traffic_percent: number;
    active: boolean;
  };
  total_jobs: number;
  scanned_jobs: number;
  variants: { A: PipelineExperimentVariantStats; B: PipelineExperimentVariantStats };
}

/** A/B 对照：按任务快照归属聚合（最近 2000 条租户任务）。 */
export function pipelineExperimentReport(
  name: string,
  tenantId: string,
): Promise<PipelineExperimentReport> {
  return request<PipelineExperimentReport>(`/pipelines/${name}/experiment/report`, { tenantId });
}
