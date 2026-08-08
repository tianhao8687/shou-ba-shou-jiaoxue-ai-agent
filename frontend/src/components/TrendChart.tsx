import type { DashboardMetrics } from '../types'

function pathFor(values: number[], width: number, height: number, min: number, max: number): string {
  return values
    .map((value, index) => {
      const x = (index / Math.max(1, values.length - 1)) * width
      const y = height - ((value - min) / Math.max(1, max - min)) * height
      return `${index === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
    })
    .join(' ')
}

export function TrendChart({ trend }: { trend: DashboardMetrics['trend'] }) {
  const success = trend.map((point) => point.success_rate)
  const latency = trend.map((point) => point.p95_ms / 1000)
  return (
    <div className="trend-chart">
      <div className="chart-legend">
        <span><i className="legend-line blue" />成功率</span>
        <span><i className="legend-line teal" />P95 延迟</span>
      </div>
      <svg role="img" aria-label="近七日成功率与延迟趋势" viewBox="0 0 620 175" preserveAspectRatio="none">
        {[0, 1, 2, 3].map((line) => <line key={line} x1="0" y1={25 + line * 40} x2="620" y2={25 + line * 40} className="grid-line" />)}
        <path d={pathFor(success, 620, 135, 80, 100)} className="chart-line success" transform="translate(0 20)" />
        <path d={pathFor(latency, 620, 135, 2, 6)} className="chart-line latency" transform="translate(0 20)" />
        {success.map((value, index) => {
          const x = (index / Math.max(1, success.length - 1)) * 620
          const y = 155 - ((value - 80) / 20) * 135
          return <circle key={trend[index].date} cx={x} cy={y} r="3.5" className="chart-dot" />
        })}
      </svg>
      <div className="chart-axis">
        {trend.map((point) => <span key={point.date}>{point.date.slice(5)}</span>)}
      </div>
    </div>
  )
}

