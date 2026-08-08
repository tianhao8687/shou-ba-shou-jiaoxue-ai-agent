import type { RiskLevel, RunStatus, TraceStatus } from './types'

export const statusLabel: Record<RunStatus, string> = {
  queued: '已入队',
  running: '运行中',
  awaiting_approval: '等待人工审批',
  completed: '已完成',
  failed: '执行失败',
  handed_off: '已转人工',
  cancelled: '已取消',
}

export const traceStatusLabel: Record<TraceStatus, string> = {
  running: '运行中',
  completed: '完成',
  waiting: '等待',
  blocked: '已阻止',
  failed: '失败',
}

export const riskLabel: Record<RiskLevel, string> = {
  low: '低风险',
  medium: '中风险',
  high: '高风险',
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(ms >= 10_000 ? 0 : 1)}s`
}

export function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

export function formatMoney(value: number): string {
  return `¥${value.toFixed(3)}`
}

export function initials(value: string): string {
  return value
    .split(/[\s.-]+/)
    .filter(Boolean)
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
}
