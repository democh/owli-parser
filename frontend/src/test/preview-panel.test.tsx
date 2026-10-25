import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  QualitySummary,
  ReviewPendingBadge,
  SourceRefs,
  formatQualityValue,
  isPreviewVersionDrift,
  locateQuality,
  refsOnPage,
} from '../pages/WorkbenchPage';

// pdfjs 动态 import 的替身：两页文档，getContext 在 jsdom 返回 null（渲染短路）
vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: {},
  getDocument: () => ({
    promise: Promise.resolve({
      numPages: 2,
      getPage: () =>
        Promise.resolve({
          getViewport: () => ({ width: 100, height: 100 }),
          render: () => ({ promise: Promise.resolve() }),
        }),
    }),
  }),
}));
import {
  PREVIEW_AUTO_LOAD_LIMITS,
  PreviewPanel,
  bboxToPercent,
  formatFileSize,
  type PreviewDescriptor,
} from '../components/PreviewPanel';

const BASE: PreviewDescriptor = {
  document_id: 'd-1',
  version_id: 'v-1',
  artifact_id: 'a-1',
  filename: 'doc.txt',
  mime: 'text/plain',
  kind: 'text',
  size_bytes: 64,
  content_path: '/v1/artifacts/a-1/content?document_id=d-1&version_id=v-1',
  pages: [],
  status: 'ready',
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('bboxToPercent（来源锚点 → 预览高亮定位）', () => {
  it('0-1 归一化坐标直接映射为百分比（不依赖像素尺寸）', () => {
    const style = bboxToPercent({ x0: 0.1, y0: 0.2, x1: 0.6, y1: 0.5 });
    expect(style.left).toBe('10.000%');
    expect(style.top).toBe('20.000%');
    expect(style.width).toBe('50.000%');
    expect(style.height).toBe('30.000%');
  });

  it('越界坐标被夹取到 [0,1]，不产生负尺寸', () => {
    const style = bboxToPercent({ x0: -0.5, y0: 0.9, x1: 1.4, y1: 0.95 });
    expect(style.left).toBe('0.000%');
    expect(style.width).toBe('100.000%');
    expect(parseFloat(style.height)).toBeGreaterThan(0);
  });
});

describe('PreviewPanel 分支', () => {
  it('unsupported 走 L4 兜底信息卡（类型/大小/受权下载）', () => {
    render(
      <PreviewPanel
        descriptor={{ ...BASE, kind: 'unsupported', mime: null, filename: 'blob.bin' }}
        highlight={null}
      />,
    );
    expect(screen.getByTestId('preview-unsupported')).toBeInTheDocument();
    expect(screen.getByText(/blob\.bin/)).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /下载原件/ });
    expect(link).toHaveAttribute('href', BASE.content_path);
  });

  it('text 分支经受权路径加载内容并展示', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response('hello preview', { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<PreviewPanel descriptor={BASE} highlight={null} />);
    await waitFor(() => expect(screen.getByText('hello preview')).toBeInTheDocument());
    // 资源请求走同源凭据，不绕过服务端鉴权
    expect(fetchMock).toHaveBeenCalledWith(BASE.content_path, {
      credentials: 'same-origin',
    });
  });

  it('text 分支加载失败给出内联错误而非无穷 loading', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('no', { status: 403 })));
    render(<PreviewPanel descriptor={BASE} highlight={null} />);
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByRole('alert').textContent).toContain('文本内容加载失败');
  });

  it('image 分支带 page_region 高亮层（百分比定位）', () => {
    render(
      <PreviewPanel
        descriptor={{ ...BASE, kind: 'image', mime: 'image/png' }}
        highlight={{ pageIndex: 0, bbox: { x0: 0.2, y0: 0.3, x1: 0.7, y1: 0.6 } }}
      />,
    );
    const box = screen.getByTestId('preview-highlight');
    expect(box.style.left).toBe('20.000%');
    expect(box.style.top).toBe('30.000%');
  });

  it('image 高亮页不匹配时不渲染高亮层', () => {
    render(
      <PreviewPanel
        descriptor={{ ...BASE, kind: 'image', mime: 'image/png' }}
        highlight={{ pageIndex: 3, bbox: { x0: 0.2, y0: 0.3, x1: 0.7, y1: 0.6 } }}
      />,
    );
    expect(screen.queryByTestId('preview-highlight')).not.toBeInTheDocument();
  });
});


const PDF_DESCRIPTOR: PreviewDescriptor = {
  ...BASE,
  kind: 'pdf',
  filename: 'doc.pdf',
  mime: 'application/pdf',
  pages: [
    { page_index: 0, geometry_id: 'g0', width: 612, height: 792, rotation: 0, width_unit: 'pt' },
    { page_index: 1, geometry_id: 'g1', width: 612, height: 792, rotation: 0, width_unit: 'pt' },
  ],
};

describe('预览 ↔ 块列表联动', () => {
  it('refsOnPage：仅 page_region 且页号匹配才命中', () => {
    const refs = [
      { kind: 'text_span', page_index: 0 },
      { kind: 'page_region', page_index: 1, bbox: { x0: 0, y0: 0, x1: 1, y1: 1 } },
    ];
    expect(refsOnPage(refs, 1)).toBe(true);
    expect(refsOnPage(refs, 0)).toBe(false);
    expect(refsOnPage([{ kind: 'text_span', page_index: 1 }], 1)).toBe(false);
    expect(refsOnPage(undefined, 0)).toBe(false);
    // 数字字符串页号（JSON 边界）也命中
    expect(refsOnPage([{ kind: 'page_region', page_index: '2' as unknown as number }], 2)).toBe(true);
  });

  it('PdfPreview 翻页触发 onPageChange（含高亮驱动的页切换）', async () => {
    const onPageChange = vi.fn();
    const { rerender } = render(
      <PreviewPanel descriptor={PDF_DESCRIPTOR} highlight={null} onPageChange={onPageChange} />,
    );
    await waitFor(() => expect(onPageChange).toHaveBeenCalledWith(0));

    // 等页数加载完成再翻页；组件在页数未知时禁用按钮，避免算出负页码。
    const next = screen.getByRole('button', { name: '下一页' });
    await waitFor(() => expect(next).toBeEnabled());
    fireEvent.click(next);
    await waitFor(() => expect(onPageChange).toHaveBeenCalledWith(1));

    // 高亮（来源锚点定位）驱动的页切换同样对外播报
    rerender(
      <PreviewPanel
        descriptor={PDF_DESCRIPTOR}
        highlight={{ pageIndex: 0 }}
        onPageChange={onPageChange}
      />,
    );
    await waitFor(() => expect(onPageChange).toHaveBeenCalledWith(0));
    expect(onPageChange.mock.calls.filter(([page]) => page === 0).length).toBeGreaterThanOrEqual(2);
  });
});

describe('近似来源标注（定位质量分级）', () => {
  it('locateQuality：区域高亮 / 仅到页 / 不可定位三档', () => {
    expect(locateQuality({ kind: 'page_region', page_index: 0, bbox: { x0: 0, y0: 0, x1: 1, y1: 1 } })).toBe('exact');
    expect(locateQuality({ kind: 'page_region', page_index: 1 })).toBe('page');
    expect(locateQuality({ kind: 'text_span', page_index: 1 })).toBe('none');
    expect(locateQuality({ kind: 'table_region', table_id: 't1' })).toBe('none');
  });

  it('SourceRefs 按质量分级渲染：exact 区域高亮按钮、page 仅到页、none 明示不可定位', () => {
    const onLocate = vi.fn();
    render(
      <SourceRefs
        refs={[
          { kind: 'page_region', page_index: 1, bbox: { x0: 0, y0: 0, x1: 0.5, y1: 0.5 } },
          { kind: 'page_region', page_index: 2 },
          { kind: 'text_span', part: 3 },
        ]}
        onLocate={onLocate}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /区域高亮/ }));
    expect(onLocate).toHaveBeenCalledWith({ pageIndex: 1, bbox: { x0: 0, y0: 0, x1: 0.5, y1: 0.5 } });
    fireEvent.click(screen.getByRole('button', { name: /仅到页/ }));
    expect(onLocate).toHaveBeenCalledWith({ pageIndex: 2, bbox: undefined });
    expect(screen.getByText(/无页面坐标，不可定位/)).toBeInTheDocument();
    const items = document.querySelectorAll('[data-locate-quality]');
    expect([...items].map((el) => el.getAttribute('data-locate-quality'))).toEqual(['exact', 'page', 'none']);
  });

  it('isPreviewVersionDrift：一致/不一致/缺失时的判定', () => {
    expect(isPreviewVersionDrift('v1', 'v1')).toBe(false);
    expect(isPreviewVersionDrift('v1', 'v2')).toBe(true);
    expect(isPreviewVersionDrift(undefined, 'v1')).toBe(false);
    expect(isPreviewVersionDrift('v1', null)).toBe(false);
  });
});

describe('版本待复核徽标', () => {
  it('正数渲染计数；0/缺失不渲染（不伪造 0 值展示）', () => {
    const { unmount } = render(<ReviewPendingBadge count={3} />);
    expect(screen.getByTestId('pending-review-badge')).toHaveTextContent('待复核 3');
    unmount();

    const zero = render(<ReviewPendingBadge count={0} />);
    expect(screen.queryByTestId('pending-review-badge')).not.toBeInTheDocument();
    zero.unmount();

    render(<ReviewPendingBadge count={undefined} />);
    expect(screen.queryByTestId('pending-review-badge')).not.toBeInTheDocument();
  });

  it('携带 versionId 时渲染复核页过滤链接', () => {
    render(
      <MemoryRouter>
        <ReviewPendingBadge count={2} versionId="v-123" />
      </MemoryRouter>,
    );
    const link = screen.getByRole('link', { name: /待复核 2/ });
    expect(link).toHaveAttribute('href', '/review?version_id=v-123');
  });
});

describe('版本质量摘要（quality_report 展示）', () => {
  it('空报告/空 metrics 不渲染', () => {
    const { unmount } = render(<QualitySummary report={null} />);
    expect(screen.queryByTestId('quality-summary')).not.toBeInTheDocument();
    unmount();
    render(<QualitySummary report={{ metrics: {} }} />);
    expect(screen.queryByTestId('quality-summary')).not.toBeInTheDocument();
  });

  it('指标中文映射与值格式化；未知指标保留原名', () => {
    render(
      <QualitySummary
        report={{
          metrics: {
            replacement_char_ratio: 0.0012,
            near_duplicate_pair_count: 2,
            block_count: 17,
            custom_metric: 0.5,
            avg_block_confidence: -1,
          },
          algorithm_revision: 'quality-v3',
        }}
      />,
    );
    const summary = screen.getByTestId('quality-summary');
    expect(summary).toHaveTextContent('乱码率 0.0012');
    expect(summary).toHaveTextContent('近重复对 2');
    expect(summary).toHaveTextContent('块数 17');
    expect(summary).toHaveTextContent('custom_metric 0.5000'); // 未映射不隐藏
    expect(summary).toHaveTextContent('平均置信度 未评估'); // -1 哨兵不伪造成 0
    expect(summary).toHaveTextContent('quality-v3');
  });

  it('notes 以折叠明细展示；formatQualityValue 直接断言', () => {
    render(
      <QualitySummary
        report={{
          metrics: { near_duplicate_pair_count: 1 },
          notes: 'near_duplicate b-0000~b-0001 (0.8182)',
        }}
      />,
    );
    expect(screen.getByText('质量说明 / 近重复明细')).toBeInTheDocument();
    expect(screen.getByText(/b-0000~b-0001/)).toBeInTheDocument();

    expect(formatQualityValue('avg_block_confidence', 0.87)).toBe('0.8700');
    expect(formatQualityValue('near_duplicate_pair_count', 3)).toBe('3');
  });
});

describe('大文件预览保护', () => {
  it('formatFileSize：B/KB/MB 与非法值兜底', () => {
    expect(formatFileSize(512)).toBe('512 B');
    expect(formatFileSize(1536)).toBe('1.5 KB');
    expect(formatFileSize(6 * 1024 * 1024)).toBe('6.0 MB');
    expect(formatFileSize(Number.NaN)).toBe('未知大小');
    expect(formatFileSize(-1)).toBe('未知大小');
  });

  it('超限文本暂停自动请求，确认后才加载', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('big body'));
    vi.stubGlobal('fetch', fetchMock);
    render(
      <PreviewPanel
        descriptor={{ ...BASE, size_bytes: PREVIEW_AUTO_LOAD_LIMITS.text + 1 }}
        highlight={null}
      />,
    );
    expect(screen.getByTestId('preview-large')).toHaveTextContent('已暂停自动预览');
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '仍要加载预览' }));
    await waitFor(() => expect(screen.getByText('big body')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('超限图片先展示确认，不创建 img 请求', () => {
    render(
      <PreviewPanel
        descriptor={{
          ...BASE,
          kind: 'image',
          mime: 'image/png',
          size_bytes: PREVIEW_AUTO_LOAD_LIMITS.image + 1,
        }}
        highlight={null}
      />,
    );
    expect(screen.getByTestId('preview-large')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '仍要加载预览' }));
    expect(screen.getByRole('img')).toBeInTheDocument();
  });

  it('超限 PDF 确认后才挂载渲染器', async () => {
    render(
      <PreviewPanel
        descriptor={{ ...PDF_DESCRIPTOR, size_bytes: PREVIEW_AUTO_LOAD_LIMITS.pdf + 1 }}
        highlight={null}
      />,
    );
    expect(screen.getByTestId('preview-large')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '下一页' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '仍要加载预览' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '下一页' })).toBeInTheDocument(),
    );
  });
});
