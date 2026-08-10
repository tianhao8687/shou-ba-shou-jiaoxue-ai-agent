import { useEffect, useMemo, useState } from 'react'
import {
  Activity,
  ArrowRight,
  BadgeCheck,
  Ban,
  BookOpenCheck,
  BrainCircuit,
  Clock3,
  Cpu,
  DatabaseZap,
  Download,
  FileKey2,
  GitBranch,
  HardDrive,
  KeyRound,
  Network,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  UserRoundCheck,
  Wrench,
} from 'lucide-react'
import { MetricCard } from '../components/MetricCard'
import { RunPipeline } from '../components/RunPipeline'
import { RunStatusPill, TraceStatusPill } from '../components/StatusPill'
import type { Check, DashboardMetrics, HealthResponse, JobRecord, PlanStep, RunRecord, UserIdentity } from '../types'
import { formatDateTime, formatDuration, riskLabel } from '../utils'

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

export function RunView({ run, jobs, metrics, health, user, busy, onDecision, onRetry, onCancel, onDownloadEvidence, onRefresh, onCreate }: RunViewProps) {
  const [selectedTraceId, setSelectedTraceId] = useState<string>()
  const [note, setNote] = useState('已核对证据、影响范围、成功条件与回滚方案，同意执行当前计划哈希。')

  useEffect(() => { setSelectedTraceId(run?.traces.at(-1)?.id) }, [run?.id, run?.traces.length])
  const selectedTrace = useMemo(() => run?.traces.find((trace) => trace.id === selectedTraceId) ?? run?.traces.at(-1), [run, selectedTraceId])

  if (!run) {
    const canCreate = user.roles.includes('operator') || user.roles.includes('admin')
    return (
      <section className="empty-state">
        <span><Wrench size={26} /></span><h2>还没有 Agent 运行</h2>
        <p>{canCreate ? '提交一个自由事故，或由管理员在密封故障实验室创建受控故障。输入中不会出现标准答案。' : '当前是只读观察身份。你可以查看本租户运行，但不能创建或取消操作任务。'}</p>
        {canCreate && <button className="button primary" type="button" onClick={onCreate}><Plus size={16} />创建第一条运行</button>}
      </section>
    )
  }

  const incident = run.incident
  const latestModelCall = run.model_calls.at(-1)
  const latestLease = run.lease_history.at(-1)
  const riskySteps = run.plan.filter((step) => step.risk !== 'low')
  const eligibility = approvalEligibility(run, user)
  const canApprove = eligibility.eligible
  const approvalProgress = run.approval.required_approvals
    ? Math.min(100, (run.approval.votes.length / run.approval.required_approvals) * 100)
    : 100
  const canRetry = run.status === 'failed' && user.roles.includes('on-call-lead')
  const canCancel = (user.roles.includes('operator') || user.roles.includes('admin')) && !['completed', 'cancelled', 'handed_off'].includes(run.status)
  const capabilityCount = run.tool_results.filter((item) => item.capability_jti).length
  const uniqueIdempotency = new Set(run.tool_results.map((item) => item.idempotency_key)).size

  return (
    <div className="run-view">
      <header className="run-heading run-heading-v3">
        <div>
          <div className="eyebrow-row"><span className={`severity severity-${incident.severity.toLowerCase()}`}>{incident.severity}</span><span>{incident.service}</span><span className="dot-separator" /><span>{incident.environment}</span>{incident.experiment_id && <code className="experiment-chip">{incident.experiment_id}</code>}</div>
          <h1>{incident.title}</h1>
          <div className="run-subline"><span className="mono">{run.id}</span><span className="tenant-chip">TENANT {run.tenant_id}</span><span className="version-chip">CAS v{run.version}</span><RunStatusPill status={run.status} /><span className={`mode-chip ${run.run_mode}`}>{run.run_mode}</span></div>
        </div>
        <div className="run-header-actions">
          <button className="button ghost" type="button" onClick={onRefresh} disabled={busy}><RefreshCw size={15} />刷新</button>
          {canRetry && <button className="button secondary" type="button" onClick={onRetry} disabled={busy}><RotateCcw size={15} />失败重试</button>}
          {canCancel && <button className="button secondary" type="button" onClick={onCancel} disabled={busy}><Ban size={15} />安全取消</button>}
          <button className="button secondary" type="button" onClick={onDownloadEvidence}><Download size={15} />证据包</button>
        </div>
      </header>

      {(run.status === 'queued' || run.status === 'running') && <div className="run-progress-banner"><RefreshCw className="spin" size={16} /><div><strong>{run.status === 'queued' ? '等待 durable worker 认领' : `正在执行 ${run.current_node} 检查点`}</strong><span>前端只轮询状态；执行不依赖浏览器连接，刷新或关闭页面不会中断任务。</span></div></div>}
      {(run.status === 'failed' || run.status === 'handed_off') && <div className="run-failure-banner"><ShieldAlert size={17} /><div><strong>{run.status === 'failed' ? `${run.error_code ?? 'RUN_FAILED'} · 自动执行失败` : '系统选择转人工'}</strong><span>{run.error_detail || run.resolution || '失败边界已记录，没有继续猜测。'}</span></div></div>}

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
            {jobs.length ? jobs.map((job) => <article key={job.id}><span className={`job-state ${job.status}`}>{job.status}</span><div><code>{job.id}</code><strong>{job.stage} · attempt {job.attempts}</strong></div><dl><div><dt>owner</dt><dd>{job.owner ?? 'unclaimed'}</dd></div><div><dt>lease</dt><dd>{job.lease_until ? formatDateTime(job.lease_until) : '—'}</dd></div><div><dt>fence</dt><dd>#{job.fencing_token}</dd></div></dl></article>) : <p className="panel-empty">任务记录尚未写入。</p>}
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
            {run.traces.map((trace, index) => <tr key={trace.id} className={selectedTrace?.id === trace.id ? 'selected' : ''}><td><span className="step-number">{String(index + 1).padStart(2, '0')}</span><strong>{trace.label}</strong></td><td className="mono muted">{trace.duration_ms ? formatDuration(trace.duration_ms) : '—'}</td><td><TraceStatusPill status={trace.status} /></td><td className="trace-summary">{trace.summary}</td><td><button className="trace-detail-button" type="button" aria-label={`查看${trace.label}详情`} onClick={() => setSelectedTraceId(trace.id)}><ArrowRight size={15} /></button></td></tr>)}
          </tbody></table></div>
        </section>

        <aside className="panel inspector-panel">
          {run.status === 'awaiting_approval' ? (
            <>
              <div className="safety-banner"><span><ShieldAlert size={20} /></span><div><small>HASH-BOUND FOUR-EYES GATE</small><strong>写操作已暂停</strong><p>审批前无副作用；满足独立主体 quorum 后才执行当前计划哈希。</p></div></div>
              <div className="approval-body">
                <div className="risk-line"><span className={`risk-dot ${run.risk_level}`} /><span>服务端有效风险</span><strong>{riskLabel[run.risk_level]}</strong></div>
                <div className="approval-hash"><small>PLAN SHA-256</small><code>{run.approval.plan_hash}</code></div>
                <section className="approval-quorum" aria-label={`审批进度 ${run.approval.votes.length}/${run.approval.required_approvals}`}>
                  <header><div><small>FOUR-EYES QUORUM</small><strong>{run.approval.votes.length} / {run.approval.required_approvals} 票</strong></div><span>{run.approval.separation_of_duties ? '职责分离开启' : '职责分离关闭'}</span></header>
                  <div className="quorum-track" aria-hidden="true"><i style={{ width: `${approvalProgress}%` }} /></div>
                  <dl><div><dt>申请人</dt><dd>{run.approval.requester ?? run.created_by}</dd></div><div><dt>剩余</dt><dd>{Math.max(0, run.approval.required_approvals - run.approval.votes.length)} 个独立主体</dd></div></dl>
                  {run.approval.votes.length > 0 && <ol className="approval-votes">{run.approval.votes.map((vote, index) => <li key={vote.approver}><span>{index + 1}</span><div><strong>{vote.display_name}</strong><code>{vote.approver}</code></div><small>{vote.decision === 'approve' ? '已批准' : '已拒绝'} · {shortHash(vote.plan_hash)}</small></li>)}</ol>}
                </section>
                <div className="required-roles"><small>每位审批人必须同时具备</small>{run.approval.required_roles.map((role) => <span className={user.roles.includes(role) || user.roles.includes('admin') ? 'owned' : 'missing'} key={role}>{role}</span>)}</div>
                {riskySteps.map((step) => <article className="risky-action" key={step.id}><div className="action-title"><FileKey2 size={17} /><strong>{step.title}</strong><code>{step.tool_name}</code></div><p>{step.objective}</p><dl><div><dt>证据绑定</dt><dd>{step.evidence_ids.join(', ')}</dd></div><div><dt>回滚</dt><dd>{step.rollback.rationale}</dd></div></dl></article>)}
                <label className="approval-note">审批备注<textarea value={note} onChange={(event) => setNote(event.target.value)} rows={3} maxLength={500} /></label>
                {!canApprove && <p className="role-warning">{eligibility.reasons.join(' ')}</p>}
                <div className="approval-actions"><button type="button" className="button primary danger-accent" disabled={busy || !canApprove} onClick={() => onDecision('approve', note)}><UserRoundCheck size={17} />{busy ? '处理中…' : `登记审批票 (${Math.min(run.approval.required_approvals, run.approval.votes.length + 1)}/${run.approval.required_approvals})`}</button><button type="button" className="button secondary" disabled={busy || !canApprove} onClick={() => onDecision('deny', '证据、权限或回滚条件不足，转人工处理。')}>拒绝并转人工</button></div>
                <p className="audit-hint"><ShieldCheck size={14} /> 租户、主体、角色、计划哈希、时间和备注进入审计事件；同一主体不能重复投票。</p>
              </div>
            </>
          ) : (
            <>
              <div className="panel-heading compact"><div><span className="section-kicker">NODE INSPECTOR</span><h2>节点检查器</h2></div></div>
              {selectedTrace ? <div className="inspector-body"><div className="inspector-title"><span className={`trace-mark trace-${selectedTrace.status}`} /><div><small>{selectedTrace.node}</small><h3>{selectedTrace.label}</h3></div><TraceStatusPill status={selectedTrace.status} /></div><p className="inspector-summary">{selectedTrace.summary}</p><dl className="detail-list"><div><dt>输入摘要</dt><dd>{selectedTrace.input_preview || '该节点没有外部输入。'}</dd></div><div><dt>输出摘要</dt><dd>{selectedTrace.output_preview || '等待输出。'}</dd></div><div><dt>开始时间</dt><dd>{formatDateTime(selectedTrace.started_at)}</dd></div><div><dt>检查点元数据</dt><dd><code>{JSON.stringify(selectedTrace.metadata, null, 2)}</code></dd></div></dl></div> : <p className="panel-empty">选择左侧节点查看详情。</p>}
            </>
          )}
        </aside>
      </div>

      <section className="panel diagnosis-panel">
        <div className="panel-heading"><div><span className="section-kicker">EVIDENCE-BOUND REASONING</span><h2>调查结果与动态诊断</h2></div><span className="panel-meta">confidence {Math.round(run.confidence * 100)}%</span></div>
        <div className="diagnosis-grid"><article><small>现场输入</small><p>{incident.summary}</p><ul>{incident.symptoms.map((symptom) => <li key={symptom}>{symptom}</li>)}</ul></article><article><small>模型诊断（受观察约束）</small><p>{run.diagnosis || '等待完成只读观测后生成诊断。'}</p>{run.model_proposal?.safety_notes.map((noteItem) => <span className="safety-note" key={noteItem}>{noteItem}</span>)}</article><article><small>最终结果</small><p>{run.resolution || '尚未进入 finalize。'}</p><dl><div><dt>观测</dt><dd>{run.observations.length}</dd></div><div><dt>计划动作</dt><dd>{run.plan.length}</dd></div><div><dt>验证</dt><dd>{run.verification.filter((item) => item.passed).length}/{run.verification.length}</dd></div></dl></article></div>
      </section>

      <section className="panel plan-panel">
        <div className="panel-heading"><div><span className="section-kicker">PLAN IR · SERVER COMPILED</span><h2>证据约束执行计划</h2></div><span className="panel-meta"><GitBranch size={14} /> {run.plan.length} 个 DAG 节点</span></div>
        {run.plan.length ? <div className="plan-list">{run.plan.map((step, index) => <PlanCard key={step.id} step={step} index={index} />)}</div> : <p className="panel-empty">Agent 尚未产出修复计划；调查计划与修复计划是两个不同阶段。</p>}
        {run.policy.issues.length > 0 && <div className="policy-issues"><strong>编译器发现的问题</strong>{run.policy.issues.map((issue, index) => <span className={issue.blocking ? 'blocking' : 'warning'} key={`${issue.code}-${index}`}><code>{issue.code}</code>{issue.message}</span>)}</div>}
      </section>

      <div className="evidence-execution-grid">
        <section className="panel evidence-panel">
          <div className="panel-heading"><div><span className="section-kicker">GROUNDING + OBSERVATION</span><h2>检索与现场证据</h2></div><span className="panel-meta"><DatabaseZap size={14} /> {run.sources.length + run.observations.length} 条</span></div>
          <div className="observation-list">{run.observations.map((observation) => <article key={observation.id}><span><Activity size={15} /></span><div><strong>{observation.tool_name}</strong><p>{observation.summary}</p><code>{observation.id} · {observation.transport}{observation.source_uri ? ` · ${observation.source_uri}` : ''}</code></div><time>{formatDateTime(observation.captured_at)}</time></article>)}</div>
          <div className="source-list">{run.sources.map((source) => <article key={source.chunk_id} className="source-item"><span className="source-code">{source.doc_id}</span><div><strong>{source.title} · {source.section}</strong><p>{source.excerpt}</p><code className="chunk-code">{source.chunk_id} · {source.retrieval_channel}</code></div><span className="score">{Math.round(source.score * 100)}%</span></article>)}</div>
        </section>

        <section className="panel execution-panel">
          <div className="panel-heading"><div><span className="section-kicker">SIDE-EFFECT PROOF</span><h2>工具结果与重新验证</h2></div><span className="panel-meta"><KeyRound size={14} /> {capabilityCount} capability</span></div>
          <div className="tool-result-list">{run.tool_results.map((result, index) => <article key={`${result.step_id}-${index}`}><div className="result-head"><span className={`result-state ${result.status}`}>{result.status}</span><strong>{result.tool_name}</strong><small>{formatDuration(result.duration_ms)}</small></div><p>{result.summary}</p><dl><div><dt>idempotency</dt><dd><code>{result.idempotency_key}</code></dd></div><div><dt>capability jti</dt><dd><code>{result.capability_jti ?? 'read-only / none'}</code></dd></div><div><dt>transport</dt><dd>{result.transport} · attempt {result.attempt}</dd></div></dl></article>)}</div>
          <div className="verification-list">{run.verification.map((item) => <article className={item.passed ? 'passed' : 'failed'} key={item.step_id}><ShieldCheck size={16} /><div><strong>{item.passed ? '验证通过' : '验证失败'} · {item.step_id}</strong><p>{item.summary}</p>{item.checks.map((check, index) => <code key={index}>{JSON.stringify(check)}</code>)}</div></article>)}</div>
          {run.tool_results.length === 0 && <p className="panel-empty">工具尚未执行；调查与审批不会被伪装成副作用成功。</p>}
        </section>
      </div>

      <details className="audit-drawer panel"><summary><span><ShieldCheck size={16} />审计事件与模型调用</span><small>{run.audit.length} audit · {run.model_calls.length} model calls</small></summary><div className="audit-grid"><div>{run.audit.map((event) => <article key={event.id}><time>{formatDateTime(event.timestamp)}</time><strong>{event.action}</strong><span>{event.actor}</span><p>{event.detail}</p></article>)}</div><div>{run.model_calls.map((call) => <article key={call.id}><time>{formatDuration(call.queue_wait_ms)} 排队 + {formatDuration(call.latency_ms)} 推理</time><strong>{call.phase} · {call.status}</strong><span>{call.provider} / {call.model}</span><p>{call.error || `${call.input_characters} input chars → ${call.output_characters} output chars`}</p></article>)}</div></div></details>

      {metrics && <section className="metrics-row compact-metrics" aria-label="运行指标"><MetricCard icon={BadgeCheck} label="任务成功率" value={`${metrics.success_rate}%`} detail={`${metrics.completed_runs}/${metrics.total_runs} 个运行完成`} tone="green" /><MetricCard icon={Wrench} label="工具成功率" value={`${metrics.tool_success_rate}%`} detail="含持久幂等跳过" /><MetricCard icon={BookOpenCheck} label="证据覆盖" value={`${metrics.citation_coverage}%`} detail="诊断可追溯" tone="teal" /><MetricCard icon={Clock3} label="P95 节点延迟" value={formatDuration(metrics.p95_latency_ms)} detail="不含人工等待" tone="amber" /><MetricCard icon={Cpu} label="真实模型命中" value={`${metrics.model_live_rate}%`} detail={`降级 ${metrics.model_fallback_rate}%`} /><MetricCard icon={Network} label="危险越权率" value={`${metrics.unsafe_action_rate}%`} detail="目标必须为 0" /></section>}
    </div>
  )
}

function PlanCard({ step, index }: { step: PlanStep; index: number }) {
  return <article className="plan-card"><header><span>{String(index + 1).padStart(2, '0')}</span><div><code>{step.id}</code><h3>{step.title}</h3></div><b className={`risk-badge ${step.risk}`}>{riskLabel[step.risk]}</b></header><p>{step.objective}</p><div className="plan-tool"><Wrench size={14} /><code>{step.tool_name}</code><JsonCode value={step.tool_input} /></div><div className="plan-contract-grid"><section><small>EVIDENCE</small><div className="token-row">{step.evidence_ids.map((id) => <code key={id}>{id}</code>)}</div></section><section><small>DEPENDS ON</small><div className="token-row">{step.depends_on.length ? step.depends_on.map((id) => <code key={id}>{id}</code>) : <span>无前置节点</span>}</div></section><section><small>PRECONDITIONS</small><CheckList checks={step.preconditions} empty="无额外前置条件" /></section><section><small>SUCCESS CRITERIA</small><CheckList checks={step.success_criteria} /></section><section className="rollback-contract"><small>ROLLBACK · {step.rollback.mode}</small><p>{step.rollback.rationale}</p>{step.rollback.tool_name && <code>{step.rollback.tool_name} {JSON.stringify(step.rollback.tool_input)}</code>}</section><section><small>RATIONALE</small><p>{step.rationale}</p></section></div></article>
}

function CheckList({ checks, empty = '未声明' }: { checks: Check[]; empty?: string }) {
  if (!checks.length) return <span>{empty}</span>
  return <ul className="check-list">{checks.map((check, index) => <li key={`${check.field}-${index}`}><code>{check.field} {check.operator} {JSON.stringify(check.value)}</code><span>{check.description}</span></li>)}</ul>
}

function JsonCode({ value }: { value: Record<string, unknown> }) {
  return <code className="json-inline">{JSON.stringify(value)}</code>
}

function shortHash(value?: string | null): string {
  return value ? `sha256:${value.slice(0, 12)}…${value.slice(-6)}` : '尚未编译计划'
}

export function approvalEligibility(run: RunRecord, user: UserIdentity): { eligible: boolean; reasons: string[] } {
  const reasons: string[] = []
  const hasRequiredRoles = user.roles.includes('admin') || run.approval.required_roles.every((role) => user.roles.includes(role))
  const isRequester = Boolean(run.approval.separation_of_duties && run.approval.requester?.toLowerCase() === user.username.toLowerCase())
  const alreadyVoted = run.approval.votes.some((vote) => vote.approver.toLowerCase() === user.username.toLowerCase())

  if (run.tenant_id !== user.tenant_id) reasons.push('当前身份不属于该运行的租户。')
  if (!hasRequiredRoles) reasons.push('当前身份缺少服务端要求的审批角色。')
  if (isRequester) reasons.push('职责分离：申请人不能审批或拒绝自己的变更，请切换到独立审批账号。')
  if (alreadyVoted) reasons.push('同一身份只能投一票，请由另一名独立审批人继续。')

  return { eligible: reasons.length === 0, reasons }
}
