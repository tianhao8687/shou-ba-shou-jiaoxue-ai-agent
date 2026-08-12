import { useEffect, useMemo, useState } from 'react'
import { BadgeCheck, BookOpenCheck, Clock3, Cpu, Network, Plus, ShieldCheck, Wrench } from 'lucide-react'
import { MetricCard } from '../../components/MetricCard'
import type { DashboardMetrics, HealthResponse, JobRecord, RunRecord, UserIdentity } from '../../types'
import { formatDateTime, formatDuration } from '../../utils'
import { ApprovalPanel } from './ApprovalPanel'
import { EvidencePanel } from './EvidencePanel'
import { RunHeader } from './RunHeader'
import { RunTimeline } from './RunTimeline'
import { ToolExecutionPanel } from './ToolExecutionPanel'
import { VerificationPanel } from './VerificationPanel'

interface RunViewProps {
  run?: RunRecord
  jobs: JobRecord[]
  metrics?: DashboardMetrics
  health?: HealthResponse
  user: UserIdentity
  busy: boolean
  onDecision: (decision: 'approve' | 'deny', note: string) => Promise<void>
  onRetry: () => Promise<void>
  onCancel: () => Promise<void>
  onDownloadEvidence: () => Promise<void>
  onRefresh: () => Promise<void>
  onCreate: () => void
}

export function RunView(props: RunViewProps) {
  const { run, jobs, metrics, health, user, busy } = props
  const [selectedTraceId, setSelectedTraceId] = useState<string>()
  const [note, setNote] = useState('已核对证据、影响范围、成功条件与回滚方案，同意执行当前计划哈希。')

  useEffect(() => { setSelectedTraceId(run?.traces.at(-1)?.id) }, [run?.id, run?.traces.length])
  const selectedTrace = useMemo(() => run?.traces.find((trace) => trace.id === selectedTraceId) ?? run?.traces.at(-1), [run, selectedTraceId])

  if (!run) return <RunEmptyState user={user} onCreate={props.onCreate} />

  const canRetry = run.status === 'failed' && user.roles.includes('on-call-lead')
  const canCancel = (user.roles.includes('operator') || user.roles.includes('admin')) && !['completed', 'cancelled', 'handed_off'].includes(run.status)

  return (
    <div className="run-view">
      <RunHeader run={run} busy={busy} canRetry={canRetry} canCancel={canCancel} onRetry={props.onRetry} onCancel={props.onCancel} onDownloadEvidence={props.onDownloadEvidence} onRefresh={props.onRefresh} />
      <RunTimeline run={run} jobs={jobs} health={health} selectedTraceId={selectedTrace?.id} onSelectTrace={setSelectedTraceId}>
        <ApprovalPanel run={run} user={user} busy={busy} selectedTrace={selectedTrace} onDecision={props.onDecision} note={note} onNoteChange={setNote} />
      </RunTimeline>
      <EvidencePanel run={run}>
        <div className="execution-stack"><ToolExecutionPanel run={run} /><VerificationPanel verification={run.verification} /></div>
      </EvidencePanel>
      <AuditDrawer run={run} />
      {metrics && <Metrics metrics={metrics} />}
    </div>
  )
}

function RunEmptyState({ user, onCreate }: { user: UserIdentity; onCreate: () => void }) {
  const canCreate = user.roles.includes('operator') || user.roles.includes('admin')
  return <section className="empty-state"><span><Wrench size={26} /></span><h2>还没有 Agent 运行</h2><p>{canCreate ? '提交一个自由事故，或由管理员在密封故障实验室创建受控故障。输入中不会出现标准答案。' : '当前是只读观察身份。你可以查看本租户运行，但不能创建或取消操作任务。'}</p>{canCreate && <button className="button primary" type="button" onClick={onCreate}><Plus size={16} />创建第一条运行</button>}</section>
}

function AuditDrawer({ run }: { run: RunRecord }) {
  return <details className="audit-drawer panel"><summary><span><ShieldCheck size={16} />审计事件与模型调用</span><small>{run.audit.length} audit · {run.model_calls.length} model calls</small></summary><div className="audit-grid"><div>{run.audit.map((event) => <article key={event.id}><time>{formatDateTime(event.timestamp)}</time><strong>{event.action}</strong><span>{event.actor}</span><p>{event.detail}</p></article>)}</div><div>{run.model_calls.map((call) => <article key={call.id}><time>{formatDuration(call.queue_wait_ms)} 排队 + {formatDuration(call.latency_ms)} 推理</time><strong>{call.phase} · {call.status}</strong><span>{call.provider} / {call.model}</span><p>{call.error || `${call.input_characters} input chars → ${call.output_characters} output chars`}</p></article>)}</div></div></details>
}

function Metrics({ metrics }: { metrics: DashboardMetrics }) {
  return <section className="metrics-row compact-metrics" aria-label="运行指标"><MetricCard icon={BadgeCheck} label="任务成功率" value={`${metrics.success_rate}%`} detail={`${metrics.completed_runs}/${metrics.total_runs} 个运行完成`} tone="green" /><MetricCard icon={Wrench} label="工具成功率" value={`${metrics.tool_success_rate}%`} detail="含持久幂等跳过" /><MetricCard icon={BookOpenCheck} label="证据覆盖" value={`${metrics.citation_coverage}%`} detail="诊断可追溯" tone="teal" /><MetricCard icon={Clock3} label="P95 节点延迟" value={formatDuration(metrics.p95_latency_ms)} detail="不含人工等待" tone="amber" /><MetricCard icon={Cpu} label="真实模型命中" value={`${metrics.model_live_rate}%`} detail={`降级 ${metrics.model_fallback_rate}%`} /><MetricCard icon={Network} label="危险越权率" value={`${metrics.unsafe_action_rate}%`} detail="目标必须为 0" /></section>
}
