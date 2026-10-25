/**
 * 模型设置（租户 admin）：连接与凭据（write-only）/校验/显式测试/发布严格分离。
 * - credential_ref 为受控引用（env:/file:），页面不回读明文，只显示是否已配置。
 * - 测试会向端点发起一次 GET /models（可能计入配额），结果脱敏保存。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import {
  ApiError,
  modelBindings,
  modelProfiles,
  request,
  saveModelBinding,
  type ModelConnection,
  type ModelTestReport,
  type ValidationReport,
} from '../api/client';
import { ModelBindingsBoard, type BindingSavePayload } from '../components/ModelBindingsBoard';
import { Card, EmptyState, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { useSessionStore } from '../stores/session';

export function ModelsSettingsPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const queryClient = useQueryClient();
  const [report, setReport] = useState<
    { id: string; validate?: ValidationReport; test?: ModelTestReport } | null
  >(null);

  const connections = useQuery({
    queryKey: ['model-connections', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<ModelConnection[]>('/model-connections', { tenantId }),
  });
  const profiles = useQuery({
    queryKey: ['model-profiles', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => modelProfiles(tenantId ?? ''),
  });
  const bindings = useQuery({
    queryKey: ['model-bindings', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => modelBindings(tenantId ?? ''),
  });
  const [bindingError, setBindingError] = useState<string | null>(null);
  const saveBinding = useMutation({
    mutationFn: (payload: BindingSavePayload) =>
      saveModelBinding({ ...payload, tenantId: tenantId ?? '', csrfToken }),
    onSuccess: () => {
      setBindingError(null);
      void queryClient.invalidateQueries({ queryKey: ['model-bindings', tenantId] });
    },
    onError: (err) => {
      setBindingError(err instanceof ApiError ? err.message : '绑定保存失败');
    },
  });
  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ['model-connections', tenantId] });

  const validate = useMutation({
    mutationFn: (id: string) =>
      request<ValidationReport>(`/model-connections/${id}/validate`, {
        method: 'POST',
        tenantId,
        csrfToken,
      }),
    onSuccess: (result, id) => setReport({ id, validate: result }),
  });
  const test = useMutation({
    mutationFn: (id: string) =>
      request<ModelTestReport>(`/model-connections/${id}/test`, {
        method: 'POST',
        tenantId,
        csrfToken,
      }),
    onSuccess: async (result, id) => {
      setReport({ id, test: result });
      invalidate();
    },
  });
  const publish = useMutation({
    mutationFn: (id: string) =>
      request<ModelConnection>(`/model-connections/${id}/publish`, {
        method: 'POST',
        tenantId,
        csrfToken,
      }),
    onSuccess: invalidate,
  });

  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [vendorLabel, setVendorLabel] = useState('');
  const [credentialRef, setCredentialRef] = useState('');
  const create = useMutation({
    mutationFn: () =>
      request<ModelConnection>('/model-connections', {
        method: 'POST',
        tenantId,
        csrfToken,
        body: {
          name,
          protocol: 'openai_compatible',
          base_url: baseUrl,
          vendor_label: vendorLabel || null,
          credential_ref: credentialRef || null,
        },
      }),
    onSuccess: () => {
      setName('');
      setBaseUrl('');
      setVendorLabel('');
      setCredentialRef('');
      invalidate();
    },
  });

  if (connections.isPending) return <SkeletonRows />;
  if (connections.isError) {
    return (
      <ErrorState
        message={connections.error instanceof ApiError ? connections.error.message : '加载失败'}
        onRetry={() => void connections.refetch()}
      />
    );
  }

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-lg font-semibold">模型设置</h1>
      <p className="text-xs text-ink-muted">
        首期支持 OpenAI 兼容协议（自托管 vLLM / 获批云服务）。密钥使用受控引用
        （env: 或 file:），页面不回读明文；离线模式仅允许已批准的内网端点。
      </p>

      <Card>
        <div className="border-b border-surface-border px-4 py-2 text-sm font-medium">
          新建模型连接
        </div>
        <form
          className="grid grid-cols-1 gap-3 p-4 md:grid-cols-4"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <label className="block text-xs">
            <span className="text-ink-muted">名称</span>
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-1.5 text-sm"
            />
          </label>
          <label className="block text-xs md:col-span-2">
            <span className="text-ink-muted">base_url</span>
            <input
              required
              type="url"
              placeholder="http://127.0.0.1:8000/v1"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-1.5 text-sm"
            />
          </label>
          <label className="block text-xs">
            <span className="text-ink-muted">厂商标签（可选）</span>
            <input
              value={vendorLabel}
              onChange={(e) => setVendorLabel(e.target.value)}
              className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-1.5 text-sm"
            />
          </label>
          <label className="block text-xs md:col-span-2">
            <span className="text-ink-muted">credential_ref（可选，env:引用）</span>
            <input
              placeholder="env:OWLI_MODEL_API_KEY"
              value={credentialRef}
              onChange={(e) => setCredentialRef(e.target.value)}
              className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-1.5 text-sm"
            />
          </label>
          {create.isError && (
            <p role="alert" className="text-xs text-state-danger md:col-span-4">
              {create.error instanceof ApiError ? create.error.message : '创建失败'}
            </p>
          )}
          <div className="md:col-span-4">
            <button
              type="submit"
              disabled={create.isPending}
              className="rounded-btn bg-brand px-4 py-2 text-sm text-white disabled:opacity-50"
            >
              创建（草稿）
            </button>
          </div>
        </form>
      </Card>

      {(connections.data ?? []).length === 0 ? (
        <EmptyState title="还没有模型连接" hint="检索与问答能力将在 M3/M4 绑定这些连接" />
      ) : (
        <Card>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
                <th className="px-4 py-2 font-medium">名称</th>
                <th className="px-4 py-2 font-medium">base_url</th>
                <th className="px-4 py-2 font-medium">凭据</th>
                <th className="px-4 py-2 font-medium">revision / 状态</th>
                <th className="px-4 py-2 font-medium">最近测试</th>
                <th className="px-4 py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {(connections.data ?? []).map((connection) => (
                <tr key={connection.id} className="border-b border-surface-border last:border-0">
                  <td className="px-4 py-2">
                    {connection.name}
                    {connection.vendor_label && (
                      <span className="ml-1 text-xs text-ink-muted">（{connection.vendor_label}）</span>
                    )}
                  </td>
                  <td className="max-w-[16rem] truncate px-4 py-2 font-mono text-xs text-ink-muted">
                    {connection.base_url}
                  </td>
                  <td className="px-4 py-2 text-xs">
                    {connection.credential_configured ? '已配置（不回读）' : '无'}
                  </td>
                  <td className="px-4 py-2 text-xs">
                    r{connection.revision} <StateBadge state={connection.state === 'published' ? 'DONE' : 'PENDING'} />
                  </td>
                  <td className="px-4 py-2 text-xs text-ink-muted">
                    {connection.last_test_report
                      ? `${String((connection.last_test_report as { ok?: boolean }).ok ? '通过' : '失败')} · ${String((connection.last_test_report as { latency_ms?: number }).latency_ms ?? '?')}ms`
                      : '未测试'}
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex gap-2 text-xs">
                      <button
                        type="button"
                        className="text-brand hover:underline"
                        onClick={() => validate.mutate(connection.id)}
                      >
                        校验
                      </button>
                      <button
                        type="button"
                        className="text-brand hover:underline"
                        onClick={() => test.mutate(connection.id)}
                      >
                        测试（发请求）
                      </button>
                      {connection.state !== 'published' && (
                        <button
                          type="button"
                          className="text-brand hover:underline"
                          onClick={() => publish.mutate(connection.id)}
                        >
                          发布
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <ModelBindingsBoard
        profiles={profiles.data ?? []}
        bindings={bindings.data ?? []}
        savingPurpose={saveBinding.isPending ? (saveBinding.variables?.purpose ?? null) : null}
        error={bindingError}
        onSave={(payload) => saveBinding.mutate(payload)}
      />

      {report && (
        <Card>
          <div className="flex items-center justify-between border-b border-surface-border px-4 py-2 text-sm font-medium">
            <span>{report.validate ? '校验结果' : '测试结果'}</span>
            <button type="button" className="text-xs text-ink-muted" onClick={() => setReport(null)}>
              关闭
            </button>
          </div>
          <div className="space-y-2 p-4 text-sm">
            {report.validate && (
              <>
                <p className={report.validate.ok ? 'text-state-ok' : 'text-state-danger'}>
                  {report.validate.ok ? '通过' : '未通过'}
                </p>
                {report.validate.errors.map((e) => (
                  <p key={e} className="text-xs text-state-danger">{e}</p>
                ))}
                {report.validate.warnings.map((w) => (
                  <p key={w} className="text-xs text-state-warn">{w}</p>
                ))}
              </>
            )}
            {report.test && (
              <>
                <p className={report.test.ok ? 'text-state-ok' : 'text-state-danger'}>
                  {report.test.ok ? '连接可用' : '连接失败'} · 延迟 {report.test.latency_ms ?? '—'}ms
                </p>
                <ul className="font-mono text-xs text-ink-muted">
                  {Object.entries(report.test.steps).map(([step, value]) => (
                    <li key={step}>
                      {step}: {value}
                    </li>
                  ))}
                </ul>
                {report.test.notes.map((note) => (
                  <p key={note} className="text-xs text-ink-muted">{note}</p>
                ))}
              </>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
