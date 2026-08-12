import { FileKey2, ShieldAlert, ShieldCheck, UserRoundCheck } from 'lucide-react'
import { TraceStatusPill } from '../../components/StatusPill'
import type { RunRecord, TraceStep, UserIdentity } from '../../types'
import { formatDateTime, riskLabel } from '../../utils'
import { shortHash } from './RunTimeline'

interface ApprovalPanelProps {
  run: RunRecord
  user: UserIdentity
  busy: boolean
  selectedTrace?: TraceStep
  onDecision: (decision: 'approve' | 'deny', note: string) => Promise<void>
  note: string
  onNoteChange: (note: string) => void
}

export function ApprovalPanel({ run, user, busy, selectedTrace, onDecision, note, onNoteChange }: ApprovalPanelProps) {
  if (run.status !== 'awaiting_approval') return <TraceInspector trace={selectedTrace} />

  const eligibility = approvalEligibility(run, user)
  const progress = run.approval.required_approvals
    ? Math.min(100, (run.approval.votes.length / run.approval.required_approvals) * 100)
    : 100
  const riskySteps = run.plan.filter((step) => step.risk !== 'low')

  return (
    <aside className="panel inspector-panel">
      <div className="safety-banner"><span><ShieldAlert size={20} /></span><div><small>HASH-BOUND FOUR-EYES GATE</small><strong>写操作已暂停</strong><p>审批前无副作用；满足独立主体 quorum 后才执行当前计划哈希。</p></div></div>
      <div className="approval-body">
        <div className="risk-line"><span className={`risk-dot ${run.risk_level}`} /><span>服务端有效风险</span><strong>{riskLabel[run.risk_level]}</strong></div>
        <div className="approval-hash"><small>PLAN SHA-256</small><code>{run.approval.plan_hash}</code></div>
        <section className="approval-quorum" aria-label={`审批进度 ${run.approval.votes.length}/${run.approval.required_approvals}`}>
          <header><div><small>FOUR-EYES QUORUM</small><strong>{run.approval.votes.length} / {run.approval.required_approvals} 票</strong></div><span>{run.approval.separation_of_duties ? '职责分离开启' : '职责分离关闭'}</span></header>
          <div className="quorum-track" aria-hidden="true"><i style={{ width: `${progress}%` }} /></div>
          <dl><div><dt>申请人</dt><dd>{run.approval.requester ?? run.created_by}</dd></div><div><dt>剩余</dt><dd>{Math.max(0, run.approval.required_approvals - run.approval.votes.length)} 个独立主体</dd></div></dl>
          {run.approval.votes.length > 0 && <ol className="approval-votes">{run.approval.votes.map((vote, index) => <li key={vote.approver}><span>{index + 1}</span><div><strong>{vote.display_name}</strong><code>{vote.approver}</code></div><small>{vote.decision === 'approve' ? '已批准' : '已拒绝'} · {shortHash(vote.plan_hash)}</small></li>)}</ol>}
        </section>
        <div className="required-roles"><small>每位审批人必须同时具备</small>{run.approval.required_roles.map((role) => <span className={user.roles.includes(role) || user.roles.includes('admin') ? 'owned' : 'missing'} key={role}>{role}</span>)}</div>
        {riskySteps.map((step) => <article className="risky-action" key={step.id}><div className="action-title"><FileKey2 size={17} /><strong>{step.title}</strong><code>{step.tool_name}</code></div><p>{step.objective}</p><dl><div><dt>证据绑定</dt><dd>{step.evidence_ids.join(', ')}</dd></div><div><dt>回滚</dt><dd>{step.rollback.rationale}</dd></div></dl></article>)}
        <label className="approval-note">审批备注<textarea value={note} onChange={(event) => onNoteChange(event.target.value)} rows={3} maxLength={500} /></label>
        {!eligibility.eligible && <p className="role-warning" role="alert">{eligibility.reasons.join(' ')}</p>}
        <div className="approval-actions"><button type="button" className="button primary danger-accent" disabled={busy || !eligibility.eligible} onClick={() => onDecision('approve', note)}><UserRoundCheck size={17} />{busy ? '处理中…' : `登记审批票 (${Math.min(run.approval.required_approvals, run.approval.votes.length + 1)}/${run.approval.required_approvals})`}</button><button type="button" className="button secondary" disabled={busy || !eligibility.eligible} onClick={() => onDecision('deny', '证据、权限或回滚条件不足，转人工处理。')}>拒绝并转人工</button></div>
        <p className="audit-hint"><ShieldCheck size={14} /> 租户、主体、角色、计划哈希、时间和备注进入审计事件；同一主体不能重复投票。</p>
      </div>
    </aside>
  )
}

function TraceInspector({ trace }: { trace?: TraceStep }) {
  return (
    <aside className="panel inspector-panel">
      <div className="panel-heading compact"><div><span className="section-kicker">NODE INSPECTOR</span><h2>节点检查器</h2></div></div>
      {trace ? <div className="inspector-body"><div className="inspector-title"><span className={`trace-mark trace-${trace.status}`} /><div><small>{trace.node}</small><h3>{trace.label}</h3></div><TraceStatusPill status={trace.status} /></div><p className="inspector-summary">{trace.summary}</p><dl className="detail-list"><div><dt>输入摘要</dt><dd>{trace.input_preview || '该节点没有外部输入。'}</dd></div><div><dt>输出摘要</dt><dd>{trace.output_preview || '等待输出。'}</dd></div><div><dt>开始时间</dt><dd>{formatDateTime(trace.started_at)}</dd></div><div><dt>检查点元数据</dt><dd><code>{JSON.stringify(trace.metadata, null, 2)}</code></dd></div></dl></div> : <p className="panel-empty">选择左侧节点查看详情。</p>}
    </aside>
  )
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
