/**
 * 存储设置（租户 admin）。
 * - Local 只允许选择平台获准的 root 池，不接受任意本机路径。
 * - 校验（无副作用）与显式 IO 测试分离；测试使用隔离前缀并即时清理。
 * - 发布仅影响新写入；读写停用开关独立控制，历史对象按原位置读取。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import {
  ApiError,
  request,
  type EffectiveSettings,
  type StorageProfile,
  type StorageTestReport,
  type ValidationReport,
} from '../api/client';
import { Card, EmptyState, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { useSessionStore } from '../stores/session';

const PROVIDERS = [
  { value: 'local', label: 'Local（本机批准目录）' },
  { value: 's3', label: 'S3 / S3 兼容' },
  { value: 'aliyun_oss', label: '阿里云 OSS' },
] as const;

interface ReportState {
  profileId: string;
  kind: 'validate' | 'test';
  validate?: ValidationReport;
  test?: StorageTestReport;
}

export function StorageSettingsPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const queryClient = useQueryClient();
  const [report, setReport] = useState<ReportState | null>(null);

  const profiles = useQuery({
    queryKey: ['storage-profiles', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<StorageProfile[]>('/storage-profiles', { tenantId }),
  });
  const effective = useQuery({
    queryKey: ['settings-effective', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<EffectiveSettings>('/settings/effective', { tenantId }),
  });

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ['storage-profiles', tenantId] });

  const validate = useMutation({
    mutationFn: (id: string) =>
      request<ValidationReport>(`/storage-profiles/${id}/validate`, {
        method: 'POST',
        tenantId,
        csrfToken,
      }),
    onSuccess: (result, id) => setReport({ profileId: id, kind: 'validate', validate: result }),
  });

  const test = useMutation({
    mutationFn: (id: string) =>
      request<StorageTestReport>(`/storage-profiles/${id}/test`, {
        method: 'POST',
        tenantId,
        csrfToken,
        body: { include_write: true },
      }),
    onSuccess: (result, id) => setReport({ profileId: id, kind: 'test', test: result }),
  });

  const publish = useMutation({
    mutationFn: (id: string) =>
      request<StorageProfile>(`/storage-profiles/${id}/publish`, {
        method: 'POST',
        tenantId,
        csrfToken,
      }),
    onSuccess: invalidate,
  });

  const setFlags = useMutation({
    mutationFn: ({ id, flags }: { id: string; flags: { read_disabled?: boolean; write_disabled?: boolean } }) =>
      request<StorageProfile>(`/storage-profiles/${id}/flags`, {
        method: 'POST',
        tenantId,
        csrfToken,
        body: flags,
      }),
    onSuccess: invalidate,
  });

  if (profiles.isPending) return <SkeletonRows />;
  if (profiles.isError) {
    return (
      <ErrorState
        message={profiles.error instanceof ApiError ? profiles.error.message : '加载失败'}
        onRetry={() => void profiles.refetch()}
      />
    );
  }
  const roots = effective.data?.summary.local_roots ?? [];

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-lg font-semibold">存储设置</h1>
      <p className="text-xs text-ink-muted">
        默认写入 profile：{effective.data?.summary.storage_default_profile ?? '—'}；
        修改默认只影响新写入，历史对象按原位置读取。
      </p>

      <CreateProfileCard roots={roots} onCreated={invalidate} />

      {(profiles.data ?? []).length === 0 ? (
        <EmptyState title="还没有存储 profile" hint="创建后可选择为数据集的写入目标" />
      ) : (
        <Card>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
                <th className="px-4 py-2 font-medium">名称</th>
                <th className="px-4 py-2 font-medium">provider</th>
                <th className="px-4 py-2 font-medium">revision</th>
                <th className="px-4 py-2 font-medium">状态</th>
                <th className="px-4 py-2 font-medium">读写开关</th>
                <th className="px-4 py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {(profiles.data ?? []).map((profile) => (
                <tr key={profile.id} className="border-b border-surface-border last:border-0">
                  <td className="px-4 py-2">{profile.name}</td>
                  <td className="px-4 py-2 text-ink-muted">{profile.provider}</td>
                  <td className="px-4 py-2 tabular-nums text-ink-muted">{profile.revision}</td>
                  <td className="px-4 py-2">
                    <StateBadge state={profile.state === 'published' ? 'DONE' : 'PENDING'} />
                  </td>
                  <td className="px-4 py-2 text-xs">
                    <label className="mr-3 inline-flex items-center gap-1">
                      <input
                        type="checkbox"
                        checked={!profile.write_disabled}
                        onChange={(e) =>
                          setFlags.mutate({
                            id: profile.id,
                            flags: { write_disabled: !e.target.checked },
                          })
                        }
                      />
                      可写
                    </label>
                    <label className="inline-flex items-center gap-1">
                      <input
                        type="checkbox"
                        checked={!profile.read_disabled}
                        onChange={(e) =>
                          setFlags.mutate({
                            id: profile.id,
                            flags: { read_disabled: !e.target.checked },
                          })
                        }
                      />
                      可读
                    </label>
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex gap-2 text-xs">
                      <button
                        type="button"
                        className="text-brand hover:underline"
                        onClick={() => validate.mutate(profile.id)}
                      >
                        校验
                      </button>
                      <button
                        type="button"
                        className="text-brand hover:underline"
                        onClick={() => test.mutate(profile.id)}
                      >
                        连接测试
                      </button>
                      {profile.state !== 'published' && (
                        <button
                          type="button"
                          className="text-brand hover:underline"
                          onClick={() => publish.mutate(profile.id)}
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

      {report && (
        <Card>
          <div className="flex items-center justify-between border-b border-surface-border px-4 py-2 text-sm font-medium">
            <span>{report.kind === 'validate' ? '校验结果' : 'IO 测试结果'}</span>
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
                {report.validate.errors.map((error) => (
                  <p key={error} className="text-xs text-state-danger">
                    {error}
                  </p>
                ))}
                {report.validate.warnings.map((warning) => (
                  <p key={warning} className="text-xs text-state-warn">
                    {warning}
                  </p>
                ))}
              </>
            )}
            {report.test && (
              <>
                <p className={report.test.ok ? 'text-state-ok' : 'text-state-danger'}>
                  {report.test.ok ? '全部步骤通过' : '存在失败步骤'}
                </p>
                <ul className="font-mono text-xs text-ink-muted">
                  {Object.entries(report.test.steps).map(([step, value]) => (
                    <li key={step}>
                      {step}: {value}
                    </li>
                  ))}
                </ul>
                {report.test.notes.map((note) => (
                  <p key={note} className="text-xs text-ink-muted">
                    {note}
                  </p>
                ))}
              </>
            )}
          </div>
        </Card>
      )}
    </div>
  );

  function CreateProfileCard({
    roots,
    onCreated,
  }: {
    roots: Array<{ id: string; path: string }>;
    onCreated: () => void;
  }) {
    const [name, setName] = useState('');
    const [provider, setProvider] = useState<string>('local');
    const [fields, setFields] = useState<Record<string, string>>({});
    const create = useMutation({
      mutationFn: () =>
        request<StorageProfile>('/storage-profiles', {
          method: 'POST',
          tenantId,
          csrfToken,
          body: { name, provider, config: buildConfig(provider, fields) },
        }),
      onSuccess: () => {
        setName('');
        setFields({});
        onCreated();
      },
    });
    const input = (key: string, label: string, required = true) => (
      <label className="block text-xs">
        <span className="text-ink-muted">{label}</span>
        <input
          required={required}
          value={fields[key] ?? ''}
          onChange={(e) => setFields((prev) => ({ ...prev, [key]: e.target.value }))}
          className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-1.5 text-sm"
        />
      </label>
    );
    return (
      <Card>
        <div className="border-b border-surface-border px-4 py-2 text-sm font-medium">
          新建存储 profile
        </div>
        <form
          className="grid grid-cols-1 gap-3 p-4 md:grid-cols-3"
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
          <label className="block text-xs">
            <span className="text-ink-muted">provider</span>
            <select
              value={provider}
              onChange={(e) => {
                setProvider(e.target.value);
                setFields({});
              }}
              className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-1.5 text-sm"
            >
              {PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <div className="md:col-span-3">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              {provider === 'local' &&
                (roots.length > 0 ? (
                  <label className="block text-xs">
                    <span className="text-ink-muted">获准目录池</span>
                    <select
                      value={fields.root_ref ?? ''}
                      onChange={(e) => {
                        const root = roots.find((r) => r.id === e.target.value);
                        setFields((prev) => ({
                          ...prev,
                          root_ref: e.target.value,
                          root_path: root?.path ?? '',
                        }));
                      }}
                      className="mt-1 w-full rounded-btn border border-surface-border bg-surface px-3 py-1.5 text-sm"
                    >
                      <option value="">请选择</option>
                      {roots.map((root) => (
                        <option key={root.id} value={root.id}>
                          {root.id}（{root.path}）
                        </option>
                      ))}
                    </select>
                  </label>
                ) : (
                  <p className="text-xs text-state-warn">平台未配置本机批准目录池</p>
                ))}
              {provider === 's3' && (
                <>
                  {input('bucket', 'bucket')}
                  {input('region', 'region')}
                  {input('endpoint', 'endpoint（可选）', false)}
                  {input('credential_ref', 'credential_ref（env:引用，可选）', false)}
                </>
              )}
              {provider === 'aliyun_oss' && (
                <>
                  {input('region', 'region')}
                  {input('endpoint', 'endpoint')}
                  {input('bucket', 'bucket')}
                  {input('credential_ref', 'credential_ref（env:引用）')}
                </>
              )}
            </div>
          </div>
          {create.isError && (
            <p role="alert" className="text-xs text-state-danger md:col-span-3">
              {create.error instanceof ApiError ? create.error.message : '创建失败'}
            </p>
          )}
          <div className="md:col-span-3">
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
    );
  }
}

function buildConfig(provider: string, fields: Record<string, string>): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(fields)) {
    if (value) config[key] = value;
  }
  if (provider === 'local' && !config.root_path) {
    throw new Error('请选择获准目录');
  }
  return config;
}
