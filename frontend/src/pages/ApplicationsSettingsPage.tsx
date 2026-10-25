/**
 * 应用与 Token 管理（租户 admin）。
 * - 创建向导：名称/描述/scope（从 /v1/scopes 注册表取候选，不默认全选）。
 * - 授权：数据集 + actions（read/write/delete），整体替换并递增 auth_revision。
 * - Token：签发明文仅弹窗展示一次；撤销需确认并即时刷新。
 * - 调用记录：脱敏聚合（无请求正文与凭据）。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import {
  ApiError,
  request,
  type ApplicationItem,
  type Dataset,
  type GrantItem,
  type IssuedToken,
  type ListPage,
  type ScopeRegistry,
  type TokenItem,
  type UsageItem,
} from '../api/client';
import { Card, EmptyState, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { TokenSecretDialog } from '../components/TokenSecretDialog';
import { useSessionStore } from '../stores/session';

const ALL_ACTIONS = ['read', 'write', 'delete'] as const;

export function ApplicationsSettingsPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const queryClient = useQueryClient();
  const [selectedAppId, setSelectedAppId] = useState<string | null>(null);
  const [issuedToken, setIssuedToken] = useState<IssuedToken | null>(null);
  const [tab, setTab] = useState<'grants' | 'tokens' | 'usage'>('grants');

  const apps = useQuery({
    queryKey: ['applications', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<ApplicationItem[]>('/applications', { tenantId }),
  });
  const scopes = useQuery({
    queryKey: ['scopes', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<ScopeRegistry>('/scopes', { tenantId }),
  });
  const datasets = useQuery({
    queryKey: ['datasets', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<ListPage<Dataset>>('/datasets?limit=200', { tenantId }),
  });

  const selectedApp = useMemo(
    () => apps.data?.find((a) => a.id === selectedAppId) ?? null,
    [apps.data, selectedAppId],
  );

  if (apps.isPending) return <SkeletonRows />;
  if (apps.isError) {
    return (
      <ErrorState
        message={apps.error instanceof ApiError ? apps.error.message : '加载失败'}
        onRetry={() => void apps.refetch()}
      />
    );
  }

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-lg font-semibold">应用与 API</h1>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="space-y-4">
          <CreateApplicationCard scopeRegistry={scopes.data} />
          <Card>
            <div className="border-b border-surface-border px-4 py-2 text-sm font-medium">
              应用列表
            </div>
            <ul className="divide-y divide-surface-border text-sm">
              {(apps.data ?? []).map((app) => (
                <li key={app.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedAppId(app.id);
                      setTab('grants');
                    }}
                    className={`block w-full px-4 py-2 text-left hover:bg-surface-muted ${
                      selectedAppId === app.id ? 'bg-brand-soft' : ''
                    }`}
                  >
                    <span className="text-ink">{app.name}</span>
                    <span className="ml-2 text-xs text-ink-muted">
                      {app.status === 'active' ? '' : `(${app.status})`}
                      {app.allowed_scopes.length} scopes
                    </span>
                  </button>
                </li>
              ))}
              {(apps.data ?? []).length === 0 && (
                <li className="px-4 py-3 text-xs text-ink-muted">还没有应用</li>
              )}
            </ul>
          </Card>
        </div>

        <div className="lg:col-span-2">
          {selectedApp ? (
            <Card>
              <div className="flex items-center justify-between border-b border-surface-border px-4 py-2">
                <div className="text-sm font-medium">{selectedApp.name}</div>
                <div className="flex gap-1 text-xs">
                  {(
                    [
                      ['grants', '数据集授权'],
                      ['tokens', 'Token'],
                      ['usage', '调用记录'],
                    ] as const
                  ).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => setTab(value)}
                      className={`rounded-tag px-2 py-1 ${
                        tab === value ? 'bg-brand-soft text-brand' : 'text-ink-muted'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              {tab === 'grants' && (
                <GrantsPanel app={selectedApp} datasets={datasets.data?.items ?? []} />
              )}
              {tab === 'tokens' && (
                <TokensPanel app={selectedApp} datasets={datasets.data?.items ?? []} onIssued={setIssuedToken} />
              )}
              {tab === 'usage' && <UsagePanel appId={selectedApp.id} />}
            </Card>
          ) : (
            <EmptyState title="选择左侧应用查看授权、Token 与调用记录" />
          )}
        </div>
      </div>
      {issuedToken && (
        <TokenSecretDialog
          issued={issuedToken}
          onClose={() => setIssuedToken(null)}
        />
      )}
    </div>
  );

  function CreateApplicationCard({ scopeRegistry }: { scopeRegistry: ScopeRegistry | undefined }) {
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [selectedScopes, setSelectedScopes] = useState<Set<string>>(new Set());
    const create = useMutation({
      mutationFn: () =>
        request<{ id: string }>('/applications', {
          method: 'POST',
          tenantId,
          csrfToken,
          body: {
            name,
            description: description || null,
            allowed_scopes: [...selectedScopes],
          },
        }),
      onSuccess: async (created) => {
        setName('');
        setDescription('');
        setSelectedScopes(new Set());
        await queryClient.invalidateQueries({ queryKey: ['applications', tenantId] });
        setSelectedAppId(created.id);
      },
    });
    return (
      <Card>
        <div className="border-b border-surface-border px-4 py-2 text-sm font-medium">
          新建应用
        </div>
        <form
          className="space-y-2 p-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (selectedScopes.size === 0) return;
            create.mutate();
          }}
        >
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="应用名称"
            className="w-full rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
          />
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="用途说明（可选）"
            className="w-full rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
          />
          <fieldset className="max-h-48 overflow-y-auto rounded-btn border border-surface-border p-2">
            <legend className="px-1 text-xs text-ink-muted">scope 上限（不默认全选）</legend>
            {(scopeRegistry?.items ?? []).map((scope) => (
              <label key={scope.name} className="flex items-start gap-2 py-1 text-xs">
                <input
                  type="checkbox"
                  checked={selectedScopes.has(scope.name)}
                  onChange={(e) => {
                    setSelectedScopes((prev) => {
                      const next = new Set(prev);
                      if (e.target.checked) next.add(scope.name);
                      else next.delete(scope.name);
                      return next;
                    });
                  }}
                />
                <span>
                  <span className="font-mono text-ink">{scope.name}</span>
                  <span className="ml-1 text-ink-muted">{scope.description}</span>
                </span>
              </label>
            ))}
          </fieldset>
          {create.isError && (
            <p role="alert" className="text-xs text-state-danger">
              {create.error instanceof ApiError ? create.error.message : '创建失败'}
            </p>
          )}
          <button
            type="submit"
            disabled={create.isPending || selectedScopes.size === 0}
            className="w-full rounded-btn bg-brand px-3 py-2 text-sm text-white disabled:opacity-50"
          >
            创建应用
          </button>
        </form>
      </Card>
    );
  }

  function GrantsPanel({ app, datasets }: { app: ApplicationItem; datasets: Dataset[] }) {
    const grantsQuery = useQuery({
      queryKey: ['grants', tenantId, app.id],
      queryFn: () => request<{ items: GrantItem[] }>(`/applications/${app.id}/grants`, { tenantId }),
    });
    const [draft, setDraft] = useState<Map<string, Set<string>> | null>(null);
    const effective = draft ?? new Map(
      (grantsQuery.data?.items ?? []).map((g) => [g.dataset_id, new Set(g.actions)]),
    );
    const save = useMutation({
      mutationFn: () =>
        request<{ auth_revision: number }>(`/applications/${app.id}/grants`, {
          method: 'PUT',
          tenantId,
          csrfToken,
          body: {
            grants: [...effective.entries()].map(([datasetId, actions]) => ({
              dataset_id: datasetId,
              actions: [...actions],
            })),
          },
        }),
      onSuccess: async () => {
        setDraft(null);
        await queryClient.invalidateQueries({ queryKey: ['grants', tenantId, app.id] });
      },
    });

    if (grantsQuery.isPending) return <div className="p-4"><SkeletonRows rows={2} /></div>;

    return (
      <div className="space-y-3 p-4 text-sm">
        <p className="text-xs text-ink-muted">
          授权的数据集决定该应用可访问的资源范围；数据库授权变更立即影响新请求。
        </p>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
              <th className="py-1 font-medium">数据集</th>
              {ALL_ACTIONS.map((action) => (
                <th key={action} className="py-1 font-medium">
                  {action}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {datasets.map((dataset) => {
              const actions = effective.get(dataset.id);
              return (
                <tr key={dataset.id} className="border-b border-surface-border last:border-0">
                  <td className="py-1.5">{dataset.name}</td>
                  {ALL_ACTIONS.map((action) => (
                    <td key={action} className="py-1.5">
                      <input
                        type="checkbox"
                        aria-label={`${dataset.name} ${action}`}
                        checked={actions?.has(action) ?? false}
                        onChange={(e) => {
                          const next = new Map(effective);
                          const set = new Set(next.get(dataset.id) ?? []);
                          if (e.target.checked) set.add(action);
                          else set.delete(action);
                          if (set.size === 0) next.delete(dataset.id);
                          else next.set(dataset.id, set);
                          setDraft(next);
                        }}
                      />
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="flex items-center gap-3">
          <button
            type="button"
            disabled={draft === null || save.isPending}
            onClick={() => save.mutate()}
            className="rounded-btn bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            保存授权
          </button>
          {draft !== null && <span className="text-xs text-state-warn">有未保存的修改</span>}
          {save.isError && (
            <span role="alert" className="text-xs text-state-danger">
              {save.error instanceof ApiError ? save.error.message : '保存失败'}
            </span>
          )}
        </div>
      </div>
    );
  }

  function TokensPanel({
    app,
    datasets,
    onIssued,
  }: {
    app: ApplicationItem;
    datasets: Dataset[];
    onIssued: (token: IssuedToken) => void;
  }) {
    const tokensQuery = useQuery({
      queryKey: ['tokens', tenantId, app.id],
      queryFn: () => request<TokenItem[]>(`/applications/${app.id}/tokens`, { tenantId }),
    });
    const grantsQuery = useQuery({
      queryKey: ['grants', tenantId, app.id],
      queryFn: () => request<{ items: GrantItem[] }>(`/applications/${app.id}/grants`, { tenantId }),
    });
    const grantedIds = new Set((grantsQuery.data?.items ?? []).map((g) => g.dataset_id));
    const [name, setName] = useState('');
    const [scopes, setScopes] = useState<Set<string>>(new Set());
    const [datasetIds, setDatasetIds] = useState<Set<string>>(new Set());
    const [ttlDays, setTtlDays] = useState(90);

    const issue = useMutation({
      mutationFn: () =>
        request<IssuedToken>(`/applications/${app.id}/tokens`, {
          method: 'POST',
          tenantId,
          csrfToken,
          body: {
            name,
            scopes: [...scopes],
            dataset_ids: [...datasetIds],
            expires_in_seconds: ttlDays * 86400,
          },
        }),
      onSuccess: async (issued) => {
        setName('');
        setScopes(new Set());
        setDatasetIds(new Set());
        onIssued(issued); // 明文只在弹窗出现一次
        await queryClient.invalidateQueries({ queryKey: ['tokens', tenantId, app.id] });
      },
    });

    const revoke = useMutation({
      mutationFn: (tokenId: string) =>
        request<{ status: string }>(`/applications/${app.id}/tokens/${tokenId}/revoke`, {
          method: 'POST',
          tenantId,
          csrfToken,
        }),
      onSuccess: () =>
        void queryClient.invalidateQueries({ queryKey: ['tokens', tenantId, app.id] }),
    });

    return (
      <div className="space-y-4 p-4 text-sm">
        <form
          className="space-y-2 rounded-card border border-surface-border p-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (scopes.size === 0) return;
            issue.mutate();
          }}
        >
          <div className="text-xs font-medium text-ink">签发新 Token</div>
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Token 名称（如 ci-pipeline）"
            className="w-full rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
          />
          <div className="grid grid-cols-2 gap-3">
            <fieldset className="max-h-32 overflow-y-auto rounded-btn border border-surface-border p-2">
              <legend className="px-1 text-xs text-ink-muted">scopes（应用允许子集）</legend>
              {app.allowed_scopes.map((scope) => (
                <label key={scope} className="flex items-center gap-2 py-0.5 text-xs">
                  <input
                    type="checkbox"
                    checked={scopes.has(scope)}
                    onChange={(e) => {
                      setScopes((prev) => {
                        const next = new Set(prev);
                        if (e.target.checked) next.add(scope);
                        else next.delete(scope);
                        return next;
                      });
                    }}
                  />
                  <span className="font-mono">{scope}</span>
                </label>
              ))}
            </fieldset>
            <fieldset className="max-h-32 overflow-y-auto rounded-btn border border-surface-border p-2">
              <legend className="px-1 text-xs text-ink-muted">数据集（已授权子集）</legend>
              {datasets.filter((d) => grantedIds.has(d.id)).map((dataset) => (
                <label key={dataset.id} className="flex items-center gap-2 py-0.5 text-xs">
                  <input
                    type="checkbox"
                    checked={datasetIds.has(dataset.id)}
                    onChange={(e) => {
                      setDatasetIds((prev) => {
                        const next = new Set(prev);
                        if (e.target.checked) next.add(dataset.id);
                        else next.delete(dataset.id);
                        return next;
                      });
                    }}
                  />
                  <span>{dataset.name}</span>
                </label>
              ))}
              {grantedIds.size === 0 && (
                <p className="text-xs text-ink-muted">请先在“数据集授权”中授权</p>
              )}
            </fieldset>
          </div>
          <label className="flex items-center gap-2 text-xs text-ink-muted">
            有效期（天）
            <input
              type="number"
              min={1}
              max={365}
              value={ttlDays}
              onChange={(e) => setTtlDays(Number(e.target.value))}
              className="w-20 rounded-btn border border-surface-border bg-surface px-2 py-1 text-xs tabular-nums"
            />
          </label>
          {issue.isError && (
            <p role="alert" className="text-xs text-state-danger">
              {issue.error instanceof ApiError ? issue.error.message : '签发失败'}
            </p>
          )}
          <button
            type="submit"
            disabled={issue.isPending || scopes.size === 0}
            className="rounded-btn bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            签发
          </button>
        </form>

        {tokensQuery.isPending ? (
          <SkeletonRows rows={2} />
        ) : (tokensQuery.data ?? []).length === 0 ? (
          <p className="text-xs text-ink-muted">尚未签发 Token</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
                <th className="py-1 font-medium">名称 / public_id</th>
                <th className="py-1 font-medium">状态</th>
                <th className="py-1 font-medium">到期</th>
                <th className="py-1 font-medium">最近使用</th>
                <th className="py-1 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {(tokensQuery.data ?? []).map((token) => (
                <tr key={token.token_id} className="border-b border-surface-border last:border-0">
                  <td className="py-1.5">
                    {token.name}
                    <span className="ml-2 font-mono text-xs text-ink-muted">{token.public_id}</span>
                  </td>
                  <td className="py-1.5">
                    <StateBadge state={token.status === 'active' ? 'DONE' : token.status === 'revoked' ? 'CANCELLED' : 'RETRY_WAIT'} />
                  </td>
                  <td className="py-1.5 tabular-nums text-xs text-ink-muted">
                    {new Date(token.expires_at).toLocaleDateString()}
                  </td>
                  <td className="py-1.5 tabular-nums text-xs text-ink-muted">
                    {token.last_used_at ? new Date(token.last_used_at).toLocaleString() : '—'}
                  </td>
                  <td className="py-1.5">
                    {token.status === 'active' && (
                      <button
                        type="button"
                        onClick={() => {
                          if (window.confirm(`确认撤销 Token「${token.name}」？已发出的请求不受影响，新请求将立即被拒绝。`)) {
                            revoke.mutate(token.token_id);
                          }
                        }}
                        disabled={revoke.isPending}
                        className="text-xs text-state-danger hover:underline"
                      >
                        撤销
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    );
  }

  function UsagePanel({ appId }: { appId: string }) {
    const usage = useQuery({
      queryKey: ['usage', tenantId, appId],
      queryFn: () => request<{ items: UsageItem[] }>(`/applications/${appId}/usage`, { tenantId }),
    });
    if (usage.isPending) return <div className="p-4"><SkeletonRows rows={3} /></div>;
    const items = usage.data?.items ?? [];
    if (items.length === 0) {
      return <div className="p-4 text-xs text-ink-muted">暂无调用记录</div>;
    }
    return (
      <div className="p-4">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
              <th className="py-1 font-medium">操作</th>
              <th className="py-1 font-medium">状态</th>
              <th className="py-1 font-medium">决定</th>
              <th className="py-1 font-medium">时间</th>
              <th className="py-1 font-medium">request_id</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.request_id + row.created_at} className="border-b border-surface-border last:border-0">
                <td className="py-1.5 font-mono text-xs">{row.operation}</td>
                <td className="py-1.5 tabular-nums text-xs">{row.status_code}</td>
                <td className="py-1.5 text-xs">{row.decision ?? '—'}</td>
                <td className="py-1.5 tabular-nums text-xs text-ink-muted">
                  {new Date(row.created_at).toLocaleString()}
                </td>
                <td className="py-1.5 truncate font-mono text-xs text-ink-muted">{row.request_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
}
