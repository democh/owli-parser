import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, datasetCreateBody, request, type Dataset, type ListPage } from '../api/client';
import { Card, EmptyState, ErrorState, SkeletonRows } from '../components/shared';
import { useSessionStore } from '../stores/session';

export function DatasetsPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const [showCreate, setShowCreate] = useState(false);

  const datasets = useQuery({
    queryKey: ['datasets', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () =>
      request<ListPage<Dataset>>('/datasets?limit=100', { tenantId }),
  });

  const createDataset = useMutation({
    mutationFn: (name: string) =>
      request<{ id: string }>('/datasets', {
        method: 'POST',
        tenantId,
        csrfToken,
        body: datasetCreateBody({ name, visibility: 'tenant' }),
      }),
    onSuccess: () => {
      setShowCreate(false);
      void queryClientInvalidate();
    },
  });
  const queryClient = useQueryClient();
  const queryClientInvalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ['datasets', tenantId] });
  };

  if (datasets.isPending) return <SkeletonRows />;
  if (datasets.isError) {
    return (
      <ErrorState
        message={datasets.error instanceof ApiError ? datasets.error.message : '加载失败'}
        onRetry={() => void datasets.refetch()}
      />
    );
  }

  const items = datasets.data?.items ?? [];

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold">知识库（数据集）</h1>
        <button
          type="button"
          onClick={() => setShowCreate((v) => !v)}
          className="rounded-btn bg-brand px-3 py-1.5 text-sm text-white"
        >
          新建数据集
        </button>
      </div>
      {showCreate && (
        <form
          className="mb-4 flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            createDataset.mutate(String(data.get('name') ?? ''));
          }}
        >
          <input
            name="name"
            required
            minLength={1}
            maxLength={128}
            placeholder="数据集名称"
            className="flex-1 rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={createDataset.isPending}
            className="rounded-btn bg-brand px-4 py-2 text-sm text-white disabled:opacity-60"
          >
            创建
          </button>
        </form>
      )}
      {createDataset.isError && (
        <p role="alert" className="mb-3 text-sm text-state-danger">
          {createDataset.error instanceof ApiError ? createDataset.error.message : '创建失败'}
        </p>
      )}
      {items.length === 0 ? (
        <EmptyState title="还没有数据集" hint="创建第一个数据集后即可上传文档" />
      ) : (
        <Card>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
                <th className="px-4 py-2 font-medium">名称</th>
                <th className="px-4 py-2 font-medium">可见性</th>
                <th className="px-4 py-2 font-medium">允许部分发布</th>
                <th className="px-4 py-2 font-medium">创建时间</th>
              </tr>
            </thead>
            <tbody>
              {items.map((dataset) => (
                <tr key={dataset.id} className="border-b border-surface-border last:border-0">
                  <td className="px-4 py-2">
                    <Link className="text-brand hover:underline" to={`/documents?datasetId=${dataset.id}`}>
                      {dataset.name}
                    </Link>
                  </td>
                  <td className="px-4 py-2">{dataset.visibility}</td>
                  <td className="px-4 py-2">{dataset.allow_partial ? '是' : '否'}</td>
                  <td className="px-4 py-2 tabular-nums text-ink-muted">
                    {new Date(dataset.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
