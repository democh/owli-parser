import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ApiError, request, type DocumentItem, type ImportItem, type ListPage } from '../api/client';
import { Card, EmptyState, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { useSessionStore } from '../stores/session';

/** 上传流程：init -> PUT parts -> complete -> 提交解析任务。 */
export function DocumentsPage() {
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const [searchParams] = useSearchParams();
  const datasetId = searchParams.get('datasetId');
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  const [importUrl, setImportUrl] = useState('');

  const documents = useQuery({
    queryKey: ['documents', tenantId, datasetId],
    enabled: Boolean(tenantId),
    queryFn: () =>
      request<ListPage<DocumentItem>>(
        `/documents?limit=100${datasetId ? `&dataset_id=${datasetId}` : ''}`,
        { tenantId },
      ),
    // 快照轮询降级策略：有在途任务时缩短刷新间隔（事件通道由任务详情订阅）
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      const inflight = items.some(
        (d) => d.desired_version_id !== null && d.desired_version_id !== d.visible_version_id,
      );
      return inflight ? 4000 : false;
    },
  });

  const imports = useQuery({
    queryKey: ['imports', tenantId],
    enabled: Boolean(tenantId),
    queryFn: () => request<ListPage<ImportItem>>('/imports?limit=50', { tenantId }),
    refetchInterval: 5000,
  });

  async function uploadAndParse(file: File) {
    if (!datasetId) {
      setUploadStatus('请先选择数据集（从知识库进入）');
      return;
    }
    setUploadStatus('创建上传会话…');
    const init = await request<{ id: string; part_size_bytes: number }>('/uploads/init', {
      method: 'POST',
      tenantId,
      csrfToken,
      body: { dataset_id: datasetId, filename: file.name, total_bytes: file.size },
    });
    const partSize = init.part_size_bytes;
    const parts = Math.max(1, Math.ceil(file.size / partSize));
    for (let index = 0; index < parts; index += 1) {
      setUploadStatus(`上传分片 ${index + 1}/${parts}…`);
      const chunk = file.slice(index * partSize, Math.min((index + 1) * partSize, file.size));
      const response = await fetch(`/v1/uploads/${init.id}/parts/${index + 1}`, {
        method: 'PUT',
        headers: { 'X-Tenant-ID': tenantId ?? '', 'X-OWLI-CSRF': csrfToken ?? '' },
        credentials: 'same-origin',
        body: chunk,
      });
      if (!response.ok) {
        throw new ApiError(response.status, await response.json(), null);
      }
    }
    setUploadStatus('校验并生成产物…');
    const completed = await request<{ artifact_id: string }>(`/uploads/${init.id}/complete`, {
      method: 'POST',
      tenantId,
      csrfToken,
    });
    setUploadStatus('提交解析任务…');
    await request('/parsing/jobs', {
      method: 'POST',
      tenantId,
      csrfToken,
      // filename 影响预检与 adapter 路由（扩展名）；服务端只取清洗后的 basename
      body: { artifact_id: completed.artifact_id, dataset_id: datasetId, filename: file.name },
    });
    setUploadStatus('已提交，可在任务中心查看进度');
    await queryClient.invalidateQueries({ queryKey: ['documents', tenantId] });
    await queryClient.invalidateQueries({ queryKey: ['jobs', tenantId] });
  }

  const submitImport = useMutation({
    mutationFn: (url: string) =>
      request<ImportItem>('/imports', {
        method: 'POST',
        tenantId,
        csrfToken,
        idempotencyKey: crypto.randomUUID(),
        body: { dataset_id: datasetId, source: { kind: 'url', url } },
      }),
    onSuccess: () => {
      setImportUrl('');
      void queryClient.invalidateQueries({ queryKey: ['imports', tenantId] });
    },
  });

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">文档中心</h1>
        <input
          ref={fileInput}
          type="file"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) {
              void uploadAndParse(file).catch((error: unknown) => {
                setUploadStatus(
                  error instanceof ApiError ? `失败：${error.message}` : '上传失败，请重试',
                );
              });
            }
            event.target.value = '';
          }}
        />
        <button
          type="button"
          disabled={!datasetId}
          title={datasetId ? undefined : '请从知识库进入并选择数据集'}
          onClick={() => fileInput.current?.click()}
          className="rounded-btn bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50"
        >
          上传本地文件
        </button>
      </div>

      {uploadStatus && (
        <p aria-live="polite" className="text-sm text-ink-muted">
          {uploadStatus}
        </p>
      )}

      <Card>
        <div className="border-b border-surface-border px-4 py-3">
          <h2 className="text-sm font-medium">URL 导入（单个文件，服务端安全拉取）</h2>
        </div>
        <form
          className="flex gap-2 p-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (importUrl.trim()) {
              submitImport.mutate(importUrl.trim());
            }
          }}
        >
          <input
            type="url"
            required
            value={importUrl}
            onChange={(event) => setImportUrl(event.target.value)}
            placeholder="https://files.example.org/manual.pdf"
            className="flex-1 rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={!datasetId || submitImport.isPending}
            className="rounded-btn border border-surface-border px-4 py-2 text-sm disabled:opacity-50"
          >
            导入
          </button>
        </form>
        {submitImport.isError && (
          <p role="alert" className="px-4 pb-3 text-sm text-state-danger">
            {submitImport.error instanceof ApiError ? submitImport.error.message : '导入失败'}
          </p>
        )}
        {(imports.data?.items.length ?? 0) > 0 && (
          <ul className="border-t border-surface-border px-4 py-2 text-sm">
            {imports.data?.items.slice(0, 5).map((item) => (
              <li key={item.id} className="flex items-center gap-3 py-1">
                <StateBadge state={item.state} />
                <span className="text-ink-muted">
                  {item.display_host}
                  {item.display_path}
                </span>
                {item.error && (
                  <span className="text-xs text-state-danger">{item.error.message}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {documents.isPending ? (
        <SkeletonRows />
      ) : documents.isError ? (
        <ErrorState
          message={documents.error instanceof ApiError ? documents.error.message : '加载失败'}
          onRetry={() => void documents.refetch()}
        />
      ) : (documents.data?.items.length ?? 0) === 0 ? (
        <EmptyState title="该范围内还没有文档" hint="上传本地文件或通过 URL 导入" />
      ) : (
        <Card>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-surface-border text-left text-xs text-ink-muted">
                <th className="px-4 py-2 font-medium">文件名</th>
                <th className="px-4 py-2 font-medium">类型</th>
                <th className="px-4 py-2 font-medium">大小</th>
                <th className="px-4 py-2 font-medium">版本状态</th>
                <th className="px-4 py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {documents.data?.items.map((doc) => {
                const pending =
                  doc.desired_version_id !== null && doc.desired_version_id !== doc.visible_version_id;
                return (
                  <tr key={doc.id} className="border-b border-surface-border last:border-0">
                    <td className="px-4 py-2">
                      <Link className="text-brand hover:underline" to={`/documents/${doc.id}`}>
                        {doc.filename}
                      </Link>
                    </td>
                    <td className="px-4 py-2 text-ink-muted">{doc.mime ?? '未知'}</td>
                    <td className="px-4 py-2 tabular-nums text-ink-muted">
                      {doc.size_bytes ?? '—'}
                    </td>
                    <td className="px-4 py-2">
                      {pending ? (
                        <StateBadge state="PARSING" />
                      ) : doc.visible_version_id ? (
                        <StateBadge state="DONE" />
                      ) : (
                        <StateBadge state="PENDING" />
                      )}
                    </td>
                    <td className="px-4 py-2">
                      <Link className="text-brand hover:underline" to={`/documents/${doc.id}`}>
                        工作台
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
