import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import {
  ApiError,
  chunkPatchBody,
  request,
  type ChunkItem,
  type DocumentItem,
  type ListPage,
} from '../api/client';
import {
  PreviewPanel,
  fetchPreviewDescriptor,
  type PreviewBBox,
  type PreviewHighlight,
} from '../components/PreviewPanel';
import { Card, EmptyState, ErrorState, SkeletonRows, StateBadge } from '../components/shared';
import { useSessionStore } from '../stores/session';

interface VersionItem {
  id: string;
  version_no: number;
  lifecycle: string;
  pending_review_count?: number; // 版本质量摘要：待复核数（复核队列徽标）
  coverage: { total_units?: number; succeeded_units?: number; missing_units?: string[] } | null;
  quality_report: {
    metrics?: Record<string, number>;
    algorithm_revision?: string;
    notes?: string | null;
  } | null;
  created_at: string;
}

interface SourceRefItem extends Record<string, unknown> {
  kind?: string;
  page_index?: number;
  block_id?: string;
  bbox?: PreviewBBox;
}

interface BlockItem {
  id: string;
  type: string;
  order: number;
  parent_id: string | null;
  text: string;
  lang: string | null;
  source_refs: SourceRefItem[];
  payload: Record<string, unknown>;
  confidence: { raw_score: number; method: string } | null;
  provenance: { adapter_name?: string; adapter_version?: string; engine?: string } | null;
}

/** 来源锚点是否指向该页（page_index 0 基；仅 page_region 可定位）。 */
export function refsOnPage(refs: SourceRefItem[] | undefined, pageIndex: number): boolean {
  return (refs ?? []).some(
    (ref) => ref.kind === 'page_region' && Number(ref.page_index) === pageIndex,
  );
}

export type LocateQuality = 'exact' | 'page' | 'none';

/** 质量指标中文名（未知指标保留原名，不隐藏）。 */
export const QUALITY_METRIC_LABELS: Record<string, string> = {
  block_count: '块数',
  empty_block_ratio: '空块率',
  replacement_char_ratio: '乱码率',
  duplicate_block_ratio: '去重率',
  near_duplicate_pair_count: '近重复对',
  unit_error_count: '单位错误',
  avg_block_confidence: '平均置信度',
};

/** 指标值展示：-1 为"无置信数据"的哨兵（不伪造成 0）。 */
export function formatQualityValue(metric: string, value: number): string {
  if (metric === 'avg_block_confidence' && value < 0) {
    return '未评估';
  }
  if (metric === 'block_count' || metric === 'unit_error_count' || metric === 'near_duplicate_pair_count') {
    return String(value);
  }
  return value.toFixed(4);
}

/** 版本质量报告摘要：指标 chips + 算法修订号 + notes 明细（折叠）。 */
export function QualitySummary({
  report,
}: {
  report: VersionItem['quality_report'];
}) {
  if (!report || !report.metrics || Object.keys(report.metrics).length === 0) {
    return null;
  }
  const entries = Object.entries(report.metrics).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="mt-1 space-y-1" data-testid="quality-summary">
      <div className="flex flex-wrap gap-1.5 text-xs">
        {entries.map(([metric, value]) => (
          <span key={metric} className="rounded-tag bg-surface-muted px-2 py-0.5 tabular-nums">
            {QUALITY_METRIC_LABELS[metric] ?? metric} {formatQualityValue(metric, value)}
          </span>
        ))}
        {report.algorithm_revision && (
          <span className="rounded-tag border border-surface-border px-2 py-0.5 text-ink-muted">
            {report.algorithm_revision}
          </span>
        )}
      </div>
      {report.notes && (
        <details className="text-xs text-ink-muted">
          <summary className="cursor-pointer">质量说明 / 近重复明细</summary>
          <p className="mt-1 whitespace-pre-wrap break-all">{report.notes}</p>
        </details>
      )}
    </div>
  );
}

/** 版本待复核徽标：0/缺失不渲染（不伪造 0 值展示）；有 versionId 时链接到复核页过滤视图。 */
export function ReviewPendingBadge({
  count,
  versionId,
}: {
  count: number | null | undefined;
  versionId?: string;
}) {
  const pending = count ?? 0;
  if (pending <= 0) {
    return null;
  }
  const className =
    'ml-2 shrink-0 rounded-tag bg-surface-muted px-2 py-0.5 text-xs text-state-warn';
  if (versionId) {
    return (
      <Link
        to={`/review?version_id=${versionId}`}
        data-testid="pending-review-badge"
        title="查看该版本的待复核项"
        className={`${className} hover:bg-surface-border`}
      >
        待复核 {pending}
      </Link>
    );
  }
  return (
    <span data-testid="pending-review-badge" title="复核队列中存在待处置项" className={className}>
      待复核 {pending}
    </span>
  );
}

/** 预览版本与列表版本是否漂移（任一缺失视为不漂移，避免误报）。 */
export function isPreviewVersionDrift(
  previewVersionId: string | null | undefined,
  shownVersionId: string | null,
): boolean {
  return Boolean(previewVersionId && shownVersionId) && previewVersionId !== shownVersionId;
}

/**
 * 来源锚点定位质量分级（近似标注）：
 * - exact：page_region + bbox → 可翻页且区域高亮；
 * - page：page_region 无 bbox → 仅能翻页定位（无区域高亮）；
 * - none：无页面坐标（text_span/table_region 等）→ 不可在预览中定位。
 */
export function locateQuality(ref: SourceRefItem): LocateQuality {
  if (ref.kind !== 'page_region' || ref.page_index === undefined) {
    return 'none';
  }
  return ref.bbox ? 'exact' : 'page';
}

const BLOCK_TYPES = [
  'text',
  'heading',
  'table',
  'formula',
  'image',
  'figure_desc',
  'list',
  'code',
  'page_header',
  'page_footer',
  'footnote',
] as const;

/** 文档工作台：版本历史 + 分块视图 + blocks 视图（IR），全部可追踪来源。 */
export function WorkbenchPage() {
  const { documentId = '' } = useParams();
  // 复核队列等入口可携带 version_id/block_id 直达定位（仅初始化时消费）
  const [searchParams] = useSearchParams();
  const initialVersionId = searchParams.get('version_id');
  const initialBlockId = searchParams.get('block_id');
  const tenantId = useSessionStore((s) => s.currentTenantId);
  const csrfToken = useSessionStore((s) => s.csrfToken);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(initialVersionId);
  const [leftTab, setLeftTab] = useState<'preview' | 'versions'>('preview');
  const [highlight, setHighlight] = useState<PreviewHighlight | null>(null);
  const [view, setView] = useState<'chunks' | 'blocks'>(initialBlockId ? 'blocks' : 'chunks');
  const [blockType, setBlockType] = useState<string>('');
  const [selectedChunkId, setSelectedChunkId] = useState<string | null>(null);
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(initialBlockId);
  const [editingChunk, setEditingChunk] = useState(false);
  const [editText, setEditText] = useState('');
  const [chunkError, setChunkError] = useState<string | null>(null);
  const [chunkNotice, setChunkNotice] = useState<string | null>(null);
  // 预览 ↔ 块列表联动：当前展示页 + 是否跟随（翻到新页时滚动列表到首个匹配块）
  const [visiblePage, setVisiblePage] = useState<number | null>(null);
  const [followPreview, setFollowPreview] = useState(true);
  const followTargetRef = useRef<HTMLLIElement | null>(null);

  // 预览翻页 → 跟随滚动到首个包含该页来源的块（仅标记时不动列表）
  useEffect(() => {
    if (followPreview && visiblePage !== null && followTargetRef.current) {
      followTargetRef.current.scrollIntoView?.({ block: 'nearest' });
    }
  }, [visiblePage, followPreview, view]);
  const handlePageChange = useCallback((page: number) => setVisiblePage(page), []);

  const documentQuery = useQuery({
    queryKey: ['document', tenantId, documentId],
    enabled: Boolean(tenantId && documentId),
    queryFn: () => request<DocumentItem>(`/documents/${documentId}`, { tenantId }),
  });

  const versionsQuery = useQuery({
    queryKey: ['versions', tenantId, documentId],
    enabled: Boolean(tenantId && documentId),
    queryFn: () => request<VersionItem[]>(`/documents/${documentId}/versions`, { tenantId }),
  });

  const previewQuery = useQuery({
    queryKey: ['preview', tenantId, documentId, selectedVersionId],
    enabled: Boolean(tenantId && documentId),
    queryFn: () => fetchPreviewDescriptor(tenantId ?? '', documentId, selectedVersionId),
  });

  const chunksQuery = useQuery({
    queryKey: ['chunks', tenantId, documentId, selectedVersionId],
    enabled: Boolean(tenantId && documentId && documentQuery.data?.visible_version_id),
    queryFn: () =>
      request<ListPage<ChunkItem>>(
        `/documents/${documentId}/chunks?limit=200${
          selectedVersionId ? `&version_id=${selectedVersionId}` : ''
        }`,
        { tenantId },
      ),
  });

  const blocksQuery = useInfiniteQuery({
    queryKey: ['blocks', tenantId, documentId, selectedVersionId, blockType],
    enabled: Boolean(tenantId && documentId && documentQuery.data?.visible_version_id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: '100' });
      if (selectedVersionId) params.set('version_id', selectedVersionId);
      if (blockType) params.set('type', blockType);
      if (pageParam) params.set('cursor', pageParam);
      return request<ListPage<BlockItem> & { total: number }>(
        `/documents/${documentId}/blocks?${params.toString()}`,
        { tenantId },
      );
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  });

  if (documentQuery.isPending) return <SkeletonRows />;
  if (documentQuery.isError) {
    return (
      <ErrorState
        message={
          documentQuery.error instanceof ApiError ? documentQuery.error.message : '加载失败'
        }
        onRetry={() => void documentQuery.refetch()}
      />
    );
  }
  const document = documentQuery.data;
  const chunks = chunksQuery.data?.items ?? [];
  const selectedChunk = chunks.find((c) => c.id === selectedChunkId) ?? null;

  /** 编辑正文：If-Match 控制并发；后端同事务登记重索引（无需手动 reembed）。 */
  const saveChunkText = async () => {
    if (!selectedChunk) return;
    setChunkError(null);
    setChunkNotice(null);
    try {
      await request(`/chunks/${selectedChunk.id}`, {
        method: 'PATCH',
        tenantId,
        csrfToken,
        ifMatch: selectedChunk.revision,
        body: chunkPatchBody({ text: editText }),
      });
      setEditingChunk(false);
      setChunkNotice('已保存并登记重索引（后台自动更新；完成前旧内容不会被检索到）。');
      await chunksQuery.refetch();
    } catch (err) {
      setChunkError(err instanceof ApiError ? err.message : '保存失败');
    }
  };

  /** 手动重建单块索引（编辑后自动登记失败时的补救路径）。 */
  const reindexChunk = async () => {
    if (!selectedChunk) return;
    setChunkError(null);
    setChunkNotice(null);
    try {
      const result = await request<{ channels: string[] }>(
        `/chunks/${selectedChunk.id}/reembed`,
        { method: 'POST', tenantId, csrfToken },
      );
      setChunkNotice(
        result.channels.length > 0
          ? `索引已重建（通道：${result.channels.join('、')}）`
          : '索引通道未启用，未执行重建',
      );
    } catch (err) {
      setChunkError(err instanceof ApiError ? err.message : '重建索引失败');
    }
  };
  const blocks = blocksQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const blocksTotal = blocksQuery.data?.pages[0]?.total ?? 0;
  const selectedBlock = blocks.find((b) => b.id === selectedBlockId) ?? null;
  // 联动：当前预览页的第一个匹配块（用于跟随滚动 + 高亮标记）
  const firstChunkMatchId =
    visiblePage === null ? null : (chunks.find((c) => refsOnPage(c.source_refs, visiblePage))?.id ?? null);
  const firstBlockMatchId =
    visiblePage === null ? null : (blocks.find((b) => refsOnPage(b.source_refs, visiblePage))?.id ?? null);
  // 近似来源标注：预览版本与当前列表版本不一致（如未发布版本回退预览原件）时提示锚点可能偏移
  const shownVersionId = selectedVersionId ?? document?.visible_version_id ?? null;
  const previewVersionDrift = isPreviewVersionDrift(
    previewQuery.data?.version_id,
    shownVersionId,
  );

  return (
    <div className="mx-auto max-w-7xl space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/documents" className="text-sm text-brand hover:underline">
          ← 文档中心
        </Link>
        <h1 className="text-lg font-semibold">{document?.filename}</h1>
        <StateBadge state={document?.visible_version_id ? 'DONE' : 'PENDING'} />
        {!document?.visible_version_id && (
          <span className="text-sm text-ink-muted">尚无已发布版本（解析完成并发布后可检索）</span>
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card>
          <div className="flex items-center gap-1 border-b border-surface-border px-2 py-1.5 text-xs">
            {(
              [
                ['preview', '预览'],
                ['versions', '版本历史'],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setLeftTab(value)}
                className={`rounded-tag px-2 py-1 ${
                  leftTab === value ? 'bg-brand-soft text-brand' : 'text-ink-muted'
                }`}
              >
                {label}
              </button>
            ))}
            <span className="ml-auto pr-1 text-ink-muted">预览与解析状态相互独立</span>
          </div>
          {leftTab === 'preview' ? (
            previewQuery.isPending ? (
              <div className="p-4">
                <SkeletonRows rows={4} />
              </div>
            ) : previewQuery.isError ? (
              <ErrorState
                message={
                  previewQuery.error instanceof ApiError
                    ? previewQuery.error.message
                    : '预览不可用'
                }
                onRetry={() => void previewQuery.refetch()}
              />
            ) : previewQuery.data ? (
              <div>
                {previewVersionDrift && (
                  <p
                    role="status"
                    data-testid="preview-version-drift"
                    className="border-b border-surface-border bg-surface-muted px-4 py-2 text-xs text-state-warn"
                  >
                    近似定位：预览显示的是版本 {previewQuery.data.version_id.slice(0, 8)}…，
                    与当前列表版本不一致，来源锚点位置可能偏移。
                  </p>
                )}
                <PreviewPanel
                  descriptor={previewQuery.data}
                  highlight={highlight}
                  tenantId={tenantId ?? ''}
                  onPageChange={handlePageChange}
                />
              </div>
            ) : null
          ) : (
            <ul className="divide-y divide-surface-border text-sm">
            {(versionsQuery.data ?? []).map((version) => (
              <li key={version.id} className="px-4 py-2">
                <div className="flex items-start gap-1">
                <button
                  type="button"
                  className={`min-w-0 flex-1 text-left ${
                    selectedVersionId === version.id ? 'text-brand' : 'text-ink'
                  }`}
                  onClick={() => {
                    setSelectedVersionId(version.id);
                    setSelectedChunkId(null);
                    setSelectedBlockId(null);
                  }}
                >
                  v{version.version_no} · {version.lifecycle}
                  <span className="ml-2 text-xs text-ink-muted">
                    {new Date(version.created_at).toLocaleString()}
                  </span>
                </button>
                <ReviewPendingBadge
                  count={version.pending_review_count}
                  versionId={version.id}
                />
                </div>
                {version.coverage && (
                  <div className="mt-1 text-xs text-ink-muted">
                    覆盖 {version.coverage.succeeded_units ?? '?'}/
                    {version.coverage.total_units ?? '?'}
                    {(version.coverage.missing_units?.length ?? 0) > 0 && (
                      <span className="ml-2 text-state-warn">
                        缺失：{version.coverage.missing_units?.join(', ')}
                      </span>
                    )}
                  </div>
                )}
                {selectedVersionId === version.id && (
                  <QualitySummary report={version.quality_report} />
                )}
              </li>
            ))}
            {(versionsQuery.data ?? []).length === 0 && (
              <li className="px-4 py-3 text-xs text-ink-muted">暂无版本</li>
            )}
            </ul>
          )}
        </Card>

        <Card className="lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-surface-border px-4 py-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              {view === 'chunks' ? `分块（${chunks.length}）` : `IR 块（${blocksTotal}）`}
            </div>
            <div className="flex items-center gap-2 text-xs">
              {visiblePage !== null && (
                <span className="tabular-nums text-ink-muted">预览第 {visiblePage + 1} 页</span>
              )}
              <label className="flex items-center gap-1 text-ink-muted">
                <input
                  type="checkbox"
                  checked={followPreview}
                  onChange={(event) => setFollowPreview(event.target.checked)}
                />
                跟随预览翻页
              </label>
              {view === 'blocks' && (
                <select
                  aria-label="按类型过滤"
                  value={blockType}
                  onChange={(event) => {
                    setBlockType(event.target.value);
                    setSelectedBlockId(null);
                  }}
                  className="rounded-tag border border-surface-border bg-surface px-2 py-1"
                >
                  <option value="">全部类型</option>
                  {BLOCK_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
              )}
              <div className="flex rounded-tag border border-surface-border">
                {(
                  [
                    ['chunks', '分块'],
                    ['blocks', 'IR 块'],
                  ] as const
                ).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setView(value)}
                    className={`px-2 py-1 ${view === value ? 'bg-brand-soft text-brand' : 'text-ink-muted'}`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2">
            <div className="border-r border-surface-border">
              {view === 'chunks' ? (
                chunksQuery.isPending ? (
                  <div className="p-4">
                    <SkeletonRows rows={3} />
                  </div>
                ) : chunks.length === 0 ? (
                  <div className="p-4 text-xs text-ink-muted">该版本暂无分块</div>
                ) : (
                  /* 虚拟化将在万级块场景接入（TanStack Virtual）；M1 限制 200 条 */
                  <ul className="max-h-[32rem] divide-y divide-surface-border overflow-y-auto text-sm">
                    {chunks.slice(0, 200).map((chunk) => {
                      const onPage =
                        visiblePage !== null && refsOnPage(chunk.source_refs, visiblePage);
                      return (
                      <li
                        key={chunk.id}
                        ref={chunk.id === firstChunkMatchId ? followTargetRef : undefined}
                        data-page-match={onPage ? 'true' : undefined}
                      >
                        <button
                          type="button"
                          onClick={() => setSelectedChunkId(chunk.id)}
                          className={`block w-full px-4 py-2 text-left hover:bg-surface-muted ${
                            selectedChunkId === chunk.id ? 'bg-brand-soft' : ''
                          } ${onPage ? 'ring-1 ring-inset ring-brand' : ''}`}
                        >
                          <span className="text-xs text-ink-muted tabular-nums">
                            #{chunk.ordinal} · {chunk.kind} · {chunk.token_count} tok · r
                            {chunk.revision}
                          </span>
                          <p className="mt-1 line-clamp-2 text-ink">
                            {chunk.text.slice(0, 120)}
                            {chunk.text.length > 120 ? '…' : ''}
                          </p>
                        </button>
                      </li>
                      );
                    })}
                  </ul>
                )
              ) : blocksQuery.isPending ? (
                <div className="p-4">
                  <SkeletonRows rows={3} />
                </div>
              ) : blocks.length === 0 ? (
                <div className="p-4 text-xs text-ink-muted">该版本暂无 IR 块</div>
              ) : (
                <>
                  <ul className="max-h-[30rem] divide-y divide-surface-border overflow-y-auto text-sm">
                    {blocks.map((block) => {
                      const onPage =
                        visiblePage !== null && refsOnPage(block.source_refs, visiblePage);
                      return (
                      <li
                        key={block.id}
                        ref={block.id === firstBlockMatchId ? followTargetRef : undefined}
                        data-page-match={onPage ? 'true' : undefined}
                      >
                        <button
                          type="button"
                          onClick={() => setSelectedBlockId(block.id)}
                          className={`block w-full px-4 py-2 text-left hover:bg-surface-muted ${
                            selectedBlockId === block.id ? 'bg-brand-soft' : ''
                          } ${onPage ? 'ring-1 ring-inset ring-brand' : ''}`}
                        >
                          <span className="text-xs text-ink-muted tabular-nums">
                            #{block.order} · {block.type}
                            {block.lang ? ` · ${block.lang}` : ''}
                            {block.confidence
                              ? ` · 置信 ${(block.confidence.raw_score * 100).toFixed(0)}%`
                              : ' · 未评估'}
                          </span>
                          <p className="mt-1 line-clamp-2 text-ink">
                            {block.text.slice(0, 120)}
                            {block.text.length > 120 ? '…' : ''}
                          </p>
                        </button>
                      </li>
                      );
                    })}
                  </ul>
                  {blocksQuery.hasNextPage && (
                    <div className="border-t border-surface-border p-2 text-center">
                      <button
                        type="button"
                        onClick={() => void blocksQuery.fetchNextPage()}
                        disabled={blocksQuery.isFetchingNextPage}
                        className="rounded-btn border border-surface-border px-3 py-1.5 text-xs disabled:opacity-50"
                      >
                        {blocksQuery.isFetchingNextPage ? '加载中…' : `加载更多（已 ${blocks.length}/${blocksTotal}）`}
                      </button>
                    </div>
                  )}
                </>
              )}
            </div>

            <div className="p-4 text-sm">
              {view === 'chunks' ? (
                selectedChunk ? (
                  <div className="space-y-3">
                    <div>
                      <div className="flex items-center justify-between">
                        <div className="text-xs text-ink-muted">
                          正文（revision {selectedChunk.revision}）
                        </div>
                        <div className="flex gap-2">
                          {!editingChunk && (
                            <button
                              type="button"
                              className="rounded-btn border border-surface-border px-2 py-1 text-xs hover:bg-surface-muted"
                              onClick={() => {
                                setEditingChunk(true);
                                setEditText(selectedChunk.text);
                                setChunkError(null);
                                setChunkNotice(null);
                              }}
                            >
                              编辑正文
                            </button>
                          )}
                          <button
                            type="button"
                            className="rounded-btn border border-surface-border px-2 py-1 text-xs hover:bg-surface-muted"
                            onClick={() => void reindexChunk()}
                          >
                            重建索引
                          </button>
                        </div>
                      </div>
                      {editingChunk ? (
                        <div className="mt-1 space-y-2">
                          <textarea
                            value={editText}
                            onChange={(e) => setEditText(e.target.value)}
                            rows={6}
                            className="w-full rounded-btn border border-surface-border bg-surface px-3 py-2 text-sm"
                          />
                          <div className="flex gap-2">
                            <button
                              type="button"
                              disabled={!editText.trim()}
                              onClick={() => void saveChunkText()}
                              className="rounded-btn bg-brand px-3 py-1 text-xs text-white disabled:opacity-50"
                            >
                              保存（创建新 revision）
                            </button>
                            <button
                              type="button"
                              onClick={() => setEditingChunk(false)}
                              className="rounded-btn border border-surface-border px-3 py-1 text-xs"
                            >
                              取消
                            </button>
                          </div>
                        </div>
                      ) : (
                        <p className="mt-1 whitespace-pre-wrap text-ink">{selectedChunk.text}</p>
                      )}
                    </div>
                    {chunkError && (
                      <p role="alert" className="text-xs text-state-danger">
                        {chunkError}
                      </p>
                    )}
                    {chunkNotice && <p className="text-xs text-state-ok">{chunkNotice}</p>}
                    <SourceRefs
                      refs={selectedChunk.source_refs}
                      onLocate={(next) => {
                        setLeftTab('preview');
                        setHighlight(next);
                      }}
                    />
                    {selectedChunk.explanation && (
                      <div>
                        <div className="text-xs text-ink-muted">切分解释</div>
                        <pre className="mt-1 overflow-x-auto rounded-btn bg-surface-muted p-2 text-xs">
                          {JSON.stringify(selectedChunk.explanation, null, 2)}
                        </pre>
                      </div>
                    )}
                    <div className="text-xs text-ink-muted">
                      索引状态：{selectedChunk.embedding_state}（lexical 通道由编辑自动登记重建；
                      dense 待 M3 模型绑定）
                    </div>
                  </div>
                ) : (
                  <EmptyState title="选择左侧分块查看详情" />
                )
              ) : selectedBlock ? (
                <div className="space-y-3">
                  <div>
                    <div className="text-xs text-ink-muted">
                      {selectedBlock.id} · {selectedBlock.type}
                    </div>
                    <p className="mt-1 whitespace-pre-wrap text-ink">{selectedBlock.text}</p>
                  </div>
                  <PayloadSummary payload={selectedBlock.payload} />
                  <SourceRefs
                    refs={selectedBlock.source_refs}
                    onLocate={(next) => {
                      setLeftTab('preview');
                      setHighlight(next);
                    }}
                  />
                  <div>
                    <div className="text-xs text-ink-muted">置信度</div>
                    <p className="mt-1 text-xs">
                      {selectedBlock.confidence
                        ? `${(selectedBlock.confidence.raw_score * 100).toFixed(1)}%（${selectedBlock.confidence.method}；未校准得分不可跨 adapter 比较）`
                        : '未评估（缺失不是 1.0）'}
                    </p>
                  </div>
                  {selectedBlock.provenance && (
                    <div className="text-xs text-ink-muted">
                      provenance：{selectedBlock.provenance.adapter_name}@
                      {selectedBlock.provenance.adapter_version}
                      {selectedBlock.provenance.engine ? ` · engine ${selectedBlock.provenance.engine}` : ''}
                    </div>
                  )}
                </div>
              ) : (
                <EmptyState title="选择左侧 IR 块查看详情" />
              )}
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}

export function SourceRefs({
  refs,
  onLocate,
}: {
  refs: SourceRefItem[];
  onLocate?: (highlight: PreviewHighlight) => void;
}) {
  return (
    <div>
      <div className="text-xs text-ink-muted">来源锚点</div>
      <ul className="mt-1 space-y-1 font-mono text-xs text-ink-muted">
        {refs.map((ref, index) => {
          const label = [
            String(ref.kind),
            ref.page_index !== undefined ? `第${Number(ref.page_index) + 1}页` : null,
            ref.block_id !== undefined ? String(ref.block_id) : null,
            ref.kind === 'table_region' && ref.table_id !== undefined
              ? String(ref.table_id)
              : null,
            ref.kind === 'text_span' && ref.part !== undefined ? String(ref.part) : null,
          ]
            .filter((part): part is string => part !== null)
            .join(' · ');
          const quality = locateQuality(ref);
          return (
            <li key={index} className="truncate" data-locate-quality={quality}>
              {quality !== 'none' && onLocate ? (
                <button
                  type="button"
                  onClick={() =>
                    onLocate({ pageIndex: Number(ref.page_index), bbox: ref.bbox })
                  }
                  className="text-brand hover:underline"
                  title={quality === 'exact' ? '在预览中定位（区域高亮）' : '在预览中定位（无区域高亮）'}
                >
                  {label}
                  {quality === 'exact' ? ' ↗ 区域高亮' : ' ↗ 仅到页'}
                </button>
              ) : (
                <>
                  {label}
                  <span className="ml-1 text-ink-muted/70">（无页面坐标，不可定位）</span>
                </>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function PayloadSummary({ payload }: { payload: Record<string, unknown> }) {
  const payloadType = String(payload.payload_type ?? 'unknown');
  const entries: Array<[string, string]> = [];
  if (payloadType === 'table') {
    const rows = Array.isArray(payload.rows) ? payload.rows.length : 0;
    entries.push(['行数', String(rows)]);
    entries.push(['表头行', String(payload.header_row_count ?? 0)]);
  } else if (payloadType === 'formula') {
    entries.push(['LaTeX', String(payload.latex ?? '').slice(0, 200)]);
  } else {
    const headingPath = Array.isArray(payload.heading_path) ? payload.heading_path : [];
    if (headingPath.length > 0) {
      entries.push(['标题路径', headingPath.map(String).join(' > ')]);
    }
    if (payload.level !== undefined) {
      entries.push(['层级', String(payload.level)]);
    }
  }
  if (entries.length === 0) return null;
  return (
    <div>
      <div className="text-xs text-ink-muted">结构（{payloadType}）</div>
      <ul className="mt-1 space-y-1 text-xs text-ink-muted">
        {entries.map(([key, value]) => (
          <li key={key}>
            {key}：{value}
          </li>
        ))}
      </ul>
    </div>
  );
}
