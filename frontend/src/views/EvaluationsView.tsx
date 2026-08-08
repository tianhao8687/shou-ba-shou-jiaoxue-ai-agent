import type { CSSProperties } from 'react'
import { Check, FlaskConical, Play, RefreshCw, ShieldCheck, Target, Wrench, X } from 'lucide-react'
import type { EvaluationReport, UserIdentity } from '../types'
import { formatDateTime } from '../utils'

interface EvaluationsViewProps {
  report?: EvaluationReport
  user: UserIdentity
  busy: boolean
  onRun: (liveModel: boolean, caseLimit?: number) => Promise<void>
}

const faultLabel: Record<string, string> = {
  connection_pool_exhaustion: '连接池耗尽',
  queue_backlog: '队列积压',
  expired_credential: '凭据过期',
  stale_cache: '缓存陈旧',
  dependency_rate_limit: '依赖限流',
}

export function EvaluationsView({ report, user, busy, onRun }: EvaluationsViewProps) {
  const isAdmin = user.roles.includes('admin')
  return (
    <div className="page-view eval-view">
      <header className="page-heading eval-heading">
        <div><span className="section-kicker">SEALED VALIDATION LAB</span><h1>密封生产评测</h1><p>5 类故障 × 基线、噪声、Prompt Injection 三种变体。每个用例创建新的隔离状态与数据库；Agent 看不到 oracle，评测器最后才核对根因、工具和真实系统状态。</p></div>
        <div className="eval-actions">
          <button className="button secondary" type="button" disabled={busy || !isAdmin} onClick={() => onRun(true, 3)}>{busy ? <RefreshCw className="spin" size={17} /> : <FlaskConical size={17} />}真实 Qwen 抽样 3 例</button>
          <button className="button primary" type="button" disabled={busy || !isAdmin} onClick={() => onRun(false)}>{busy ? <RefreshCw className="spin" size={17} /> : <Play size={17} />}{busy ? '隔离实验运行中…' : '运行全部 15 例'}</button>
          {!isAdmin && <small>需要 admin 角色启动评测</small>}
        </div>
      </header>

      {!report ? (
        <section className="empty-state panel"><FlaskConical size={28} /><h2>还没有密封评测证据</h2><p>管理员可先运行可复现夹具套件，再用本地 Qwen 做少量真实模型回归。</p></section>
      ) : (
        <>
          <section className="eval-scoreboard eval-v3-scoreboard">
            <article className="score-hero">
              <div className="score-ring" style={{ '--score': report.score } as CSSProperties}><span><strong>{report.score}</strong><small>总分</small></span></div>
              <div><span className="section-kicker">LATEST SEALED RUN</span><h2>{report.id}</h2><p>{formatDateTime(report.created_at)} · {report.cases.length} 个全新实验 · P95 {report.p95_case_latency_ms}ms</p><code className="suite-mode">{report.suite_mode}</code></div>
            </article>
            {[
              ['根因准确率', report.root_cause_accuracy, Target],
              ['工具准确率', report.tool_accuracy, Wrench],
              ['真实结果匹配', report.task_success_rate, Check],
              ['检索召回', report.retrieval_recall, Target],
              ['安全门准确', report.safety_gate_accuracy, ShieldCheck],
              ['注入抵抗', report.injection_resistance, ShieldCheck],
              ['能力令牌校验', report.capability_enforcement, ShieldCheck],
              ['零危险越权', 100 - report.unsafe_action_rate, ShieldCheck],
            ].map(([label, value, Icon]) => {
              const MetricIcon = Icon as typeof Target
              const numericValue = value as number
              return <article className="eval-metric" key={label as string}><MetricIcon size={18} /><span>{label as string}</span><strong>{numericValue}%</strong><i><b style={{ width: `${numericValue}%` }} /></i></article>
            })}
          </section>

          <section className="panel eval-cases">
            <div className="panel-heading"><div><span className="section-kicker">ORACLE COMPARISON</span><h2>逐例验证矩阵</h2></div><span className="panel-meta">oracle 仅在运行结束后进入报告</span></div>
            <div className="table-wrap">
              <table className="data-table">
                <thead><tr><th>用例</th><th>密封故障</th><th>期望 / 实际工具</th><th>根因</th><th>结果</th><th>检索</th><th>安全门</th><th>抗注入</th><th>能力边界</th><th>无越权</th><th>结果</th></tr></thead>
                <tbody>
                  {report.cases.map((item) => (
                    <tr key={item.case_id}>
                      <td><code>{item.case_id}</code><small className="case-latency">{item.latency_ms}ms</small></td>
                      <td><strong>{faultLabel[item.fault_kind] ?? item.fault_kind}</strong></td>
                      <td><code>{item.expected_tool ?? 'handoff'}</code><small className="case-tools">{item.selected_tools.join(' → ') || '未执行工具'}</small></td>
                      <td><BooleanMark value={item.root_cause_match} /></td><td><BooleanMark value={item.outcome_match} /></td><td><BooleanMark value={item.retrieval_hit} /></td>
                      <td><BooleanMark value={item.gate_correct} /></td><td><BooleanMark value={item.injection_resistant} /></td><td><BooleanMark value={item.capability_enforced} /></td><td><BooleanMark value={!item.unsafe_action} /></td>
                      <td><span className={item.passed ? 'case-result passed' : 'case-result failed'}>{item.passed ? '通过' : '失败'}</span>{item.notes.length > 0 && <small className="case-note">{item.notes.join('；')}</small>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="eval-explainer eval-v3-explainer">
            <article><span>01</span><div><h3>新状态，不复读答案</h3><p>每例新建 experiment、RunStore 与 worker。运行输入只含事件事实，不含 expected 字段。</p></div></article>
            <article><span>02</span><div><h3>验证副作用，而非文本</h3><p>评分读取故障实验最终状态，确认池、队列、凭据或缓存真的恢复；漂亮的回答不能代替结果。</p></div></article>
            <article><span>03</span><div><h3>安全是不变量</h3><p>注入文本不能修改工具白名单、计划哈希、角色门或能力令牌约束；越权动作必须保持 0。</p></div></article>
            <article><span>04</span><div><h3>夹具与模型分层</h3><p>夹具验证编排确定性，真实 Qwen 回归验证生成质量。两者分别报告，避免把 mock 分数冒充模型能力。</p></div></article>
          </section>
        </>
      )}
    </div>
  )
}

function BooleanMark({ value }: { value: boolean }) {
  return <span className={value ? 'boolean-mark yes' : 'boolean-mark no'}>{value ? <Check size={14} /> : <X size={14} />}{value ? '符合' : '不符'}</span>
}
