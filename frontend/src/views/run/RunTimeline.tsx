import { ArrowRight, BrainCircuit, GitBranch, HardDrive, KeyRound } from 'lucide-react'
import type { ReactNode } from 'react'
import { RunPipeline } from '../../components/RunPipeline'
import { TraceStatusPill } from '../../components/StatusPill'
import type { HealthResponse, JobRecord, RunRecord } from '../../types'
import { formatDateTime, formatDuration } from '../../utils'

interface RunTimelineProps {
  run: RunRecord
  jobs: JobRecord[]
  health?: HealthResponse
  selectedTraceId?: string
  onSelectTrace: (traceId: string) => void
  children: ReactNode
}

export function RunTimeline({ run, jobs, health, selectedTraceId, onSelectTrace, children }: RunTimelineProps) {
  const latestModelCall = run.model_calls.at(-1)
  const latestLease = run.lease_history.at(-1)
  const capabilityCount = run.tool_results.filter((item) => item.capability_jti).length
  const uniqueIdempotency = new Set(run.tool_results.map((item) => item.idempotency_key)).size

  return (
    <>
      <RunPipeline run={run} />

      <section className="runtime-contract runtime-contract-v3" aria-label="运行时技术合同">
        <article><span className="runtime-icon"><BrainCircuit size={18} /></span><div><small>MODEL BOUNDARY</small><strong>{latestModelCall?.model ?? String(health?.model_runtime.model ?? '等待模型调用')}</strong><p>{latestModelCall ? `${latestModelCall.phase} · ${latestModelCall.status} · 排队 ${formatDuration(latestModelCall.queue_wait_ms)} / 推理 ${formatDuration(latestModelCall.latency_ms)}` : '模型只产出严格结构候选，不直接执行工具。'}</p></div><span className={`runtime-state ${latestModelCall?.status ?? 'idle'}`}>{run.run_mode}</span></article>
        <article><span className="runtime-icon"><HardDrive size={18} /></span><div><small>DURABLE JOB</small><strong>{jobs.at(-1)?.status ?? '等待任务记录'}</strong><p>{jobs.length} 个 job · attempts {jobs.at(-1)?.attempts ?? 0} · fencing #{jobs.at(-1)?.fencing_token ?? 0}</p></div><span className="runtime-state succeeded">LEASED</span></article>
        <article><span className="runtime-icon"><GitBranch size={18} /></span><div><small>IMMUTABLE PLAN</small><strong>{shortHash(run.policy.plan_hash)}</strong><p>策略编译器重算风险、依赖、证据、Schema 与回滚，再生成计划哈希。</p></div><span className={`runtime-state ${run.policy.accepted ? 'succeeded' : 'idle'}`}>{run.policy.accepted ? 'COMPILED' : 'PENDING'}</span></article>
        <article><span className="runtime-icon"><KeyRound size={18} /></span><div><small>TOOL AUTHORIZATION</small><strong>{capabilityCount} 个已签发 capability</strong><p>{uniqueIdempotency} 个幂等键 · token 绑定 payload hash 与获批角色。</p></div><span className="runtime-state succeeded">FAIL CLOSED</span></article>
      </section>

      <section className="job-ledger panel" aria-label="持久任务和租约记录">
        <div className="panel-heading"><div><span className="section-kicker">DURABLE EXECUTION</span><h2>任务、租约与 fencing</h2></div><span className="panel-meta">旧 worker 的过期写会被存储层拒绝</span></div>
        <div className="job-ledger-grid">
          <div className="job-list">
            {jobs.length ? jobs.map((job) => (
              <article key={job.id}><span className={`job-state ${job.status}`}>{job.status}</span><div><code>{job.id}</code><strong>{job.stage} · attempt {job.attempts}</strong></div><dl><div><dt>owner</dt><dd>{job.owner ?? 'unclaimed'}</dd></div><div><dt>lease</dt><dd>{job.lease_until ? formatDateTime(job.lease_until) : '—'}</dd></div><div><dt>fence</dt><dd>#{job.fencing_token}</dd></div></dl></article>
            )) : <p className="panel-empty">任务记录尚未写入。</p>}
          </div>
          <div className="lease-timeline">
            {run.lease_history.length ? run.lease_history.slice(-8).map((event, index) => <div key={`${event.timestamp}-${index}`}><i className={`lease-${event.action}`} /><span>{event.action}</span><code>{event.worker_id} · fence #{event.fencing_token}</code><time>{formatDateTime(event.timestamp)}</time></div>) : <p className="panel-empty">等待 worker claim。</p>}
            {latestLease && <small className="lease-principle">lease 解决“谁现在可以工作”，fencing token 解决“过期 worker 还能不能写”。</small>}
          </div>
        </div>
      </section>

      <div className="run-workspace">
        <section className="panel trace-panel">
          <div className="panel-heading"><div><span className="section-kicker">CHECKPOINT TRACE</span><h2>执行轨迹</h2></div><span className="panel-meta">{run.traces.length} 个节点 · 第 {run.attempt}/{run.max_attempts} 次尝试</span></div>
          <div className="trace-table-wrap"><table className="data-table trace-table"><thead><tr><th>步骤</th><th>耗时</th><th>状态</th><th>摘要</th><th><span className="sr-only">详情</span></th></tr></thead><tbody>
            {run.traces.map((trace, index) => <tr key={trace.id} className={selectedTraceId === trace.id ? 'selected' : ''}><td><span className="step-number">{String(index + 1).padStart(2, '0')}</span><strong>{trace.label}</strong></td><td className="mono muted">{trace.duration_ms ? formatDuration(trace.duration_ms) : '—'}</td><td><TraceStatusPill status={trace.status} /></td><td className="trace-summary">{trace.summary}</td><td><button className="trace-detail-button" type="button" aria-label={`查看${trace.label}详情`} aria-pressed={selectedTraceId === trace.id} onClick={() => onSelectTrace(trace.id)}><ArrowRight size={15} /></button></td></tr>)}
          </tbody></table></div>
        </section>
        {children}
      </div>
    </>
  )
}

export function shortHash(value?: string | null): string {
  return value ? `sha256:${value.slice(0, 12)}…${value.slice(-6)}` : '尚未编译计划'
}
