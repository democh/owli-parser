/**
 * TrendChart：零依赖 SVG 折线（质量回归指标随时间的演化）。
 *
 * - 不引入图表库：当前需求仅为趋势折线（文档建议的 echarts/core 按需引入
 *   留给 M5 交互型仪表板，避免为一条折线付出 100KB+ 依赖）；
 * - 坐标计算为纯函数（scaleSeries），异常与缺失从不下沉为 0：
 *   单点画圆点、同值画水平线、空序列显式提示；
 * - failed 运行以警示色点标记（趋势中的质量事故一眼可见）。
 */

export interface TrendPoint {
  runId: string;
  label: string; // 时间/run 标识（tooltip）
  value: number;
  failed: boolean;
}

export interface ScaledPoint extends TrendPoint {
  x: number;
  y: number;
}

export interface ScaledSeries {
  points: ScaledPoint[];
  min: number;
  max: number;
}

/** 归一化坐标：x 等距分布；y 按值域映射（同值时取中线形成水平线）。 */
export function scaleSeries(
  points: TrendPoint[],
  width: number,
  height: number,
  padding = 12,
): ScaledSeries | null {
  if (points.length === 0) {
    return null;
  }
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const innerWidth = Math.max(width - padding * 2, 1);
  const innerHeight = Math.max(height - padding * 2, 1);
  const scaled = points.map((point, index) => ({
    ...point,
    x: padding + (points.length === 1 ? innerWidth / 2 : (index / (points.length - 1)) * innerWidth),
    y: span === 0 ? padding + innerHeight / 2 : padding + (1 - (point.value - min) / span) * innerHeight,
  }));
  return { points: scaled, min, max };
}

export function TrendChart({
  points,
  width = 360,
  height = 120,
  ariaLabel,
}: {
  points: TrendPoint[];
  width?: number;
  height?: number;
  ariaLabel?: string;
}) {
  const scaled = scaleSeries(points, width, height);
  if (scaled === null) {
    return (
      <p className="text-xs text-ink-muted" data-testid="trend-empty">
        暂无可绘制的指标数据
      </p>
    );
  }
  const polyline = scaled.points.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
  const last = scaled.points[scaled.points.length - 1];
  const first = scaled.points[0];

  return (
    <figure data-testid="trend-chart" aria-label={ariaLabel} className="m-0">
      <svg width={width} height={height} role="img" aria-label={ariaLabel ?? '指标趋势'}>
        {/* 值域参考线（min/max 标注，不伪造 0 基线） */}
        <line
          x1={0}
          y1={scaled.points[0]?.y ?? 0}
          x2={width}
          y2={scaled.points[0]?.y ?? 0}
          stroke="currentColor"
          className="text-surface-border"
          strokeDasharray="2 4"
        />
        {scaled.points.length >= 2 && (
          <polyline
            points={polyline}
            fill="none"
            stroke="currentColor"
            className="text-brand"
            strokeWidth={2}
          />
        )}
        {scaled.points.map((point) => (
          <circle
            key={point.runId}
            cx={point.x}
            cy={point.y}
            r={4}
            className={point.failed ? 'text-state-danger' : 'text-brand'}
            fill="currentColor"
          >
            <title>
              {point.label}：{point.value.toFixed(4)}
              {point.failed ? '（未达基线）' : ''}
            </title>
          </circle>
        ))}
      </svg>
      <figcaption className="mt-1 flex justify-between text-xs text-ink-muted tabular-nums">
        <span>min {scaled.min.toFixed(4)}</span>
        <span>
          {first?.label} → {last?.label}
        </span>
        <span>max {scaled.max.toFixed(4)}</span>
      </figcaption>
    </figure>
  );
}
