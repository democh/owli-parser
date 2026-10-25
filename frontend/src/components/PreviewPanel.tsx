/**
 * PreviewPanel：文档预览器（PDF / 图片 / 文本 / L4 兜底）。
 *
 * - 预览 descriptor 由后端提供（能力事实源），前端只消费已安装 renderer。
 * - 资源统一走受权代理路径（同源 Cookie + X-Tenant-ID；服务端每次请求重新鉴权）。
 *   浏览器直接加载（<img>/pdf.js）无法补自定义头，因此图片/文本/下载一律先经
 *   fetch 取值再落地为 blob，PDF 由 pdf.js 的 httpHeaders 携带同一请求头。
 * - bbox 高亮：page_region 的 0-1 归一化坐标直接映射为百分比定位，
 *   不依赖 canvas 像素尺寸（正确处理旋转/裁剪后的视觉方向坐标）。
 * - “可以预览”不代表“已解析/已索引”：状态由调用方分别展示。
 */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { request } from '../api/client';

/**
 * 自动预览体积上限（保守默认）：超过后暂停自动加载，由用户显式确认。
 * 目标设备基准就绪后再按实测调整；文本预览另有 200k 字符截断。
 */
export const PREVIEW_AUTO_LOAD_LIMITS = {
  pdf: 50 * 1024 * 1024,
  image: 20 * 1024 * 1024,
  text: 5 * 1024 * 1024,
} as const;

/** 人类可读体积（纯函数；非法值不伪造数字）。 */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '未知大小';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

export interface PreviewDescriptor {
  document_id: string;
  version_id: string;
  artifact_id: string;
  filename: string;
  mime: string | null;
  kind: 'pdf' | 'image' | 'text' | 'unsupported';
  size_bytes: number;
  content_path: string;
  pages: Array<{
    page_index: number;
    geometry_id: string;
    width: number;
    height: number;
    rotation: number;
    width_unit: string;
  }>;
  status: string;
}

export interface PreviewBBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface PreviewHighlight {
  pageIndex: number;
  bbox?: PreviewBBox;
}

/** bbox（0-1 归一化）→ 百分比定位样式；纯函数便于测试。 */
export function bboxToPercent(bbox: PreviewBBox): {
  left: string;
  top: string;
  width: string;
  height: string;
} {
  const left = Math.max(0, Math.min(1, bbox.x0));
  const top = Math.max(0, Math.min(1, bbox.y0));
  const right = Math.max(left, Math.min(1, bbox.x1));
  const bottom = Math.max(top, Math.min(1, bbox.y1));
  return {
    left: `${(left * 100).toFixed(3)}%`,
    top: `${(top * 100).toFixed(3)}%`,
    width: `${((right - left) * 100).toFixed(3)}%`,
    height: `${((bottom - top) * 100).toFixed(3)}%`,
  };
}

export function PreviewPanel({
  descriptor,
  highlight,
  tenantId,
  onPageChange,
}: {
  descriptor: PreviewDescriptor;
  highlight: PreviewHighlight | null;
  /** 当前租户：受权代理路径要求 X-Tenant-ID（与 API client 同一口径）。 */
  tenantId: string;
  /** 当前展示页变化回调（含高亮驱动与手动翻页）：用于与块列表联动。 */
  onPageChange?: (pageIndex: number) => void;
}) {
  if (descriptor.kind === 'unsupported') {
    return <UnsupportedCard descriptor={descriptor} tenantId={tenantId} />;
  }
  if (descriptor.kind === 'text') {
    return (
      <LargeFileGuard descriptor={descriptor} tenantId={tenantId} limit={PREVIEW_AUTO_LOAD_LIMITS.text}>
        <TextPreview descriptor={descriptor} tenantId={tenantId} />
      </LargeFileGuard>
    );
  }
  if (descriptor.kind === 'image') {
    return (
      <LargeFileGuard descriptor={descriptor} tenantId={tenantId} limit={PREVIEW_AUTO_LOAD_LIMITS.image}>
        <ImagePreview descriptor={descriptor} tenantId={tenantId} highlight={highlight} />
      </LargeFileGuard>
    );
  }
  return (
    <LargeFileGuard descriptor={descriptor} tenantId={tenantId} limit={PREVIEW_AUTO_LOAD_LIMITS.pdf}>
      <PdfPreview
        descriptor={descriptor}
        tenantId={tenantId}
        highlight={highlight}
        onPageChange={onPageChange}
      />
    </LargeFileGuard>
  );
}

/** 受权资源读取：同源 Cookie + 租户头，非 2xx 一律抛错（调用方给内联提示，不假装成功）。 */
export async function fetchPreviewResource(url: string, tenantId: string): Promise<Response> {
  const response = await fetch(url, {
    credentials: 'same-origin',
    headers: tenantId ? { 'X-Tenant-ID': tenantId } : undefined,
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response;
}

/** 受权下载：浏览器 <a download> 同样无法带租户头，改为取值后本地生成链接。 */
function AuthorizedDownload({
  descriptor,
  tenantId,
  className,
  children,
}: {
  descriptor: PreviewDescriptor;
  tenantId: string;
  className: string;
  children: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function handleDownload(): Promise<void> {
    setBusy(true);
    setFailed(false);
    try {
      const response = await fetchPreviewResource(descriptor.content_path, tenantId);
      const blobUrl = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = blobUrl;
      anchor.download = descriptor.filename;
      anchor.click();
      URL.revokeObjectURL(blobUrl);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-1">
      <button type="button" onClick={() => void handleDownload()} disabled={busy} className={className}>
        {children}
      </button>
      {failed && (
        <p role="alert" className="text-xs text-state-danger">
          下载失败（可能权限已变化或连接中断）
        </p>
      )}
    </div>
  );
}

function LargeFileGuard({
  descriptor,
  tenantId,
  limit,
  children,
}: {
  descriptor: PreviewDescriptor;
  tenantId: string;
  limit: number;
  children: ReactNode;
}) {
  const [loadedArtifactId, setLoadedArtifactId] = useState<string | null>(null);
  if (descriptor.size_bytes <= limit || loadedArtifactId === descriptor.artifact_id) {
    return <>{children}</>;
  }
  return (
    <div data-testid="preview-large" className="space-y-2 p-4 text-sm">
      <p className="text-ink">
        文件较大（{formatFileSize(descriptor.size_bytes)}），已暂停自动预览以控制内存。
      </p>
      <p className="text-xs text-ink-muted">
        预览按需加载；文本内容仍会截断展示。也可使用受权下载查看完整内容。
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => setLoadedArtifactId(descriptor.artifact_id)}
          className="rounded-btn border border-surface-border px-3 py-1.5 text-xs hover:bg-surface-muted"
        >
          仍要加载预览
        </button>
        <AuthorizedDownload
          descriptor={descriptor}
          tenantId={tenantId}
          className="rounded-btn border border-surface-border px-3 py-1.5 text-xs hover:bg-surface-muted"
        >
          下载原件（受权）
        </AuthorizedDownload>
      </div>
    </div>
  );
}

function HighlightBox({ bbox }: { bbox: PreviewBBox }) {
  return (
    <div
      aria-hidden="true"
      data-testid="preview-highlight"
      className="pointer-events-none absolute border-2 border-brand bg-brand/15"
      style={bboxToPercent(bbox)}
    />
  );
}

function UnsupportedCard({
  descriptor,
  tenantId,
}: {
  descriptor: PreviewDescriptor;
  tenantId: string;
}) {
  return (
    <div className="space-y-2 p-4 text-sm" data-testid="preview-unsupported">
      <p className="text-ink">该类型未启用预览渲染器，提供文件级兜底信息。</p>
      <ul className="space-y-1 text-xs text-ink-muted">
        <li>文件名：{descriptor.filename}</li>
        <li>类型：{descriptor.mime ?? '未知（MIME 无法判定）'}</li>
        <li>大小：{descriptor.size_bytes} 字节</li>
        <li>artifact：{descriptor.artifact_id.slice(0, 8)}</li>
      </ul>
      <AuthorizedDownload
        descriptor={descriptor}
        tenantId={tenantId}
        className="rounded-btn border border-surface-border px-3 py-1.5 text-xs hover:bg-surface-muted"
      >
        下载原件（受权）
      </AuthorizedDownload>
    </div>
  );
}

function ImagePreview({
  descriptor,
  tenantId,
  highlight,
}: {
  descriptor: PreviewDescriptor;
  tenantId: string;
  highlight: PreviewHighlight | null;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    async function load(): Promise<void> {
      try {
        const response = await fetchPreviewResource(descriptor.content_path, tenantId);
        objectUrl = URL.createObjectURL(await response.blob());
        if (!cancelled) setSrc(objectUrl);
      } catch {
        if (!cancelled) setError('图片内容加载失败（可能权限已变化或连接中断）');
      }
    }
    void load();
    return () => {
      cancelled = true;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [descriptor.content_path, tenantId]);

  return (
    <div className="relative">
      <img
        src={src ?? undefined}
        alt={descriptor.filename}
        className="block max-h-[36rem] w-full object-contain"
      />
      {highlight && highlight.pageIndex === 0 && highlight.bbox && (
        <HighlightBox bbox={highlight.bbox} />
      )}
      {error && (
        <p role="alert" className="p-4 text-sm text-state-danger">
          {error}
        </p>
      )}
    </div>
  );
}

function TextPreview({
  descriptor,
  tenantId,
}: {
  descriptor: PreviewDescriptor;
  tenantId: string;
}) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load(): Promise<void> {
      try {
        const response = await fetchPreviewResource(descriptor.content_path, tenantId);
        const body = await response.text();
        if (!cancelled) {
          // 交互端限制展示体积；完整内容仍可经受权导出获取
          setText(body.length > 200_000 ? `${body.slice(0, 200_000)}\n…（已截断展示）` : body);
        }
      } catch {
        if (!cancelled) setError('文本内容加载失败（可能权限已变化或连接中断）');
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [descriptor.content_path, tenantId]);

  if (error) {
    return (
      <p role="alert" className="p-4 text-sm text-state-danger">
        {error}
      </p>
    );
  }
  if (text === null) {
    return (
      <p className="p-4 text-xs text-ink-muted" aria-busy="true">
        加载中…
      </p>
    );
  }
  return (
    <pre className="max-h-[36rem] overflow-auto whitespace-pre-wrap p-4 text-xs text-ink">
      {text}
    </pre>
  );
}

function PdfPreview({
  descriptor,
  tenantId,
  highlight,
  onPageChange,
}: {
  descriptor: PreviewDescriptor;
  tenantId: string;
  highlight: PreviewHighlight | null;
  onPageChange?: (pageIndex: number) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [pageCount, setPageCount] = useState(0);
  const [currentPage, setCurrentPage] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const docRef = useRef<{ numPages: number } | null>(null);
  const onPageChangeRef = useRef(onPageChange);

  // 回调存 ref，避免父组件内联函数导致重复触发
  useEffect(() => {
    onPageChangeRef.current = onPageChange;
  });

  // 当前页变化（高亮驱动或手动翻页）→ 通知外部联动
  useEffect(() => {
    onPageChangeRef.current?.(currentPage);
  }, [currentPage]);

  // 高亮驱动翻页（来源锚点点击 → 预览定位）
  useEffect(() => {
    if (highlight && highlight.pageIndex >= 0 && highlight.pageIndex !== currentPage) {
      setCurrentPage(highlight.pageIndex);
    }
    // 仅在 highlight 变化时响应，避免把用户手动翻页拉回
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlight]);

  useEffect(() => {
    let cancelled = false;
    // pdf.js 与 worker 按需加载（PDF 渲染不进入首屏 bundle）
    async function render(): Promise<void> {
      try {
        const pdfjs = await import('pdfjs-dist');
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          'pdfjs-dist/build/pdf.worker.min.mjs',
          import.meta.url,
        ).toString();
        if (cancelled) return;
        let pdf: { numPages: number; getPage: (n: number) => Promise<unknown> };
        if (docRef.current === null) {
          pdf = (await pdfjs.getDocument({
            url: descriptor.content_path,
            withCredentials: true,
            // 受权代理要求租户头：pdf.js 支持为文档请求附加自定义头
            httpHeaders: tenantId ? { 'X-Tenant-ID': tenantId } : undefined,
          }).promise) as unknown as { numPages: number; getPage: (n: number) => Promise<unknown> };
          docRef.current = pdf;
          if (!cancelled) setPageCount(pdf.numPages);
        } else {
          pdf = docRef.current as unknown as {
            numPages: number;
            getPage: (n: number) => Promise<unknown>;
          };
        }
        const page = (await pdf.getPage(currentPage + 1)) as {
          getViewport: (opts: { scale: number }) => { width: number; height: number };
          render: (opts: { canvasContext: CanvasRenderingContext2D; viewport: unknown }) => {
            promise: Promise<void>;
          };
        };
        const viewport = page.getViewport({ scale: 1.5 });
        const canvas = canvasRef.current;
        if (canvas === null || cancelled) return;
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        const context = canvas.getContext('2d');
        if (context === null) return;
        await page.render({ canvasContext: context, viewport }).promise;
        if (!cancelled) setError(null);
      } catch {
        if (!cancelled) {
          setError('PDF 渲染失败（文件可能损坏、加密或会话已过期）');
        }
      }
    }
    void render();
    return () => {
      cancelled = true;
    };
  }, [descriptor.content_path, tenantId, currentPage]);

  if (error) {
    return (
      <div className="space-y-2 p-4 text-sm">
        <p role="alert" className="text-state-danger">
          {error}
        </p>
        <AuthorizedDownload
          descriptor={descriptor}
          tenantId={tenantId}
          className="rounded-btn border border-surface-border px-3 py-1.5 text-xs hover:bg-surface-muted"
        >
          下载原件（受权）
        </AuthorizedDownload>
      </div>
    );
  }

  const pageGeometry = descriptor.pages.find((p) => p.page_index === currentPage);

  return (
    <div>
      <div className="flex items-center justify-between border-b border-surface-border px-4 py-2 text-xs">
        <div className="flex items-center gap-2">
          <button
            type="button"
            aria-label="上一页"
            disabled={currentPage <= 0}
            onClick={() => setCurrentPage((p) => Math.max(0, p - 1))}
            className="rounded-tag border border-surface-border px-2 py-1 disabled:opacity-40"
          >
            ‹
          </button>
          <span className="tabular-nums">
            第 {currentPage + 1} / {Math.max(pageCount, 1)} 页
          </span>
          <button
            type="button"
            aria-label="下一页"
            disabled={pageCount === 0 || currentPage >= pageCount - 1}
            onClick={() =>
              setCurrentPage((p) => Math.max(0, Math.min(Math.max(pageCount, 1) - 1, p + 1)))
            }
            className="rounded-tag border border-surface-border px-2 py-1 disabled:opacity-40"
          >
            ›
          </button>
        </div>
        {pageGeometry && pageGeometry.rotation !== 0 && (
          <span className="text-ink-muted">页面旋转 {pageGeometry.rotation}°</span>
        )}
      </div>
      <div className="relative overflow-auto">
        <canvas ref={canvasRef} className="block w-full" />
        {highlight && highlight.pageIndex === currentPage && highlight.bbox && (
          <HighlightBox bbox={highlight.bbox} />
        )}
      </div>
    </div>
  );
}

/** content_path 为同源受权代理：浏览器直接可用的 <img>/pdf.js 资源读取封装。 */
export function fetchPreviewDescriptor(
  tenantId: string,
  documentId: string,
  versionId: string | null,
): Promise<PreviewDescriptor> {
  const params = versionId ? `?version_id=${versionId}` : '';
  return request<PreviewDescriptor>(`/documents/${documentId}/preview${params}`, { tenantId });
}
