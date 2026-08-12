import { Activity, DatabaseZap, GitBranch, Wrench } from 'lucide-react'
import type { ReactNode } from 'react'
import type { Check, PlanStep, RunRecord } from '../../types'
import { formatDateTime, riskLabel } from '../../utils'

export function EvidencePanel({ run, children }: { run: RunRecord; children: ReactNode }) {
  const { incident } = run

  return (
    <>
      <section className="panel diagnosis-panel">
        <div className="panel-heading"><div><span className="section-kicker">EVIDENCE-BOUND REASONING</span><h2>调查结果与动态诊断</h2></div><span className="panel-meta">confidence {Math.round(run.confidence * 100)}%</span></div>
        <div className="diagnosis-grid">
          <article><small>现场输入</small><p>{incident.summary}</p><ul>{incident.symptoms.map((symptom) => <li key={symptom}>{symptom}</li>)}</ul></article>
          <article><small>模型诊断（受观察约束）</small><p>{run.diagnosis || '等待完成只读观测后生成诊断。'}</p>{run.model_proposal?.safety_notes.map((note) => <span className="safety-note" key={note}>{note}</span>)}</article>
          <article><small>最终结果</small><p>{run.resolution || '尚未进入 finalize。'}</p><dl><div><dt>观测</dt><dd>{run.observations.length}</dd></div><div><dt>计划动作</dt><dd>{run.plan.length}</dd></div><div><dt>验证</dt><dd>{run.verification.filter((item) => item.passed).length}/{run.verification.length}</dd></div></dl></article>
        </div>
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
        {children}
      </div>
    </>
  )
}

function PlanCard({ step, index }: { step: PlanStep; index: number }) {
  return (
    <article className="plan-card">
      <header><span>{String(index + 1).padStart(2, '0')}</span><div><code>{step.id}</code><h3>{step.title}</h3></div><b className={`risk-badge ${step.risk}`}>{riskLabel[step.risk]}</b></header>
      <p>{step.objective}</p>
      <div className="plan-tool"><Wrench size={14} /><code>{step.tool_name}</code><code className="json-inline">{JSON.stringify(step.tool_input)}</code></div>
      <div className="plan-contract-grid">
        <section><small>EVIDENCE</small><div className="token-row">{step.evidence_ids.map((id) => <code key={id}>{id}</code>)}</div></section>
        <section><small>DEPENDS ON</small><div className="token-row">{step.depends_on.length ? step.depends_on.map((id) => <code key={id}>{id}</code>) : <span>无前置节点</span>}</div></section>
        <section><small>PRECONDITIONS</small><CheckList checks={step.preconditions} empty="无额外前置条件" /></section>
        <section><small>SUCCESS CRITERIA</small><CheckList checks={step.success_criteria} /></section>
        <section className="rollback-contract"><small>ROLLBACK · {step.rollback.mode}</small><p>{step.rollback.rationale}</p>{step.rollback.tool_name && <code>{step.rollback.tool_name} {JSON.stringify(step.rollback.tool_input)}</code>}</section>
        <section><small>RATIONALE</small><p>{step.rationale}</p></section>
      </div>
    </article>
  )
}

function CheckList({ checks, empty = '未声明' }: { checks: Check[]; empty?: string }) {
  if (!checks.length) return <span>{empty}</span>
  return <ul className="check-list">{checks.map((check, index) => <li key={`${check.field}-${index}`}><code>{check.field} {check.operator} {JSON.stringify(check.value)}</code><span>{check.description}</span></li>)}</ul>
}
