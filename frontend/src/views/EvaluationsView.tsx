import { useMemo, useState, type CSSProperties } from 'react'
import { Activity, Check, Database, FlaskConical, Play, RefreshCw, ShieldCheck, Target, Wrench, X } from 'lucide-react'
import { ExternalValidationPanel } from '../components/ExternalValidationPanel'
import { TelemetryValidationPanel } from '../components/TelemetryValidationPanel'
import type { EvaluationReport, ExternalValidationReport, TelemetryValidationReport, UserIdentity } from '../types'
import { formatDateTime } from '../utils'

interface EvaluationsViewProps {
  report?: EvaluationReport
  externalReport?: ExternalValidationReport
  telemetryReport?: TelemetryValidationReport
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

const categoryLabel: Record<string, string> = {
  baseline: '基线',
  prompt_injection: '提示注入',
  identity_spoofing: '身份伪造',
  approval_bypass: '绕过审批',
  scope_manipulation: '扩大范围',
  verification_bypass: '跳过复核',
  evidence_conflict: '证据冲突',
  context_noise: '无关噪声',
  context_overload: '长上下文',
  encoding_obfuscation: '字符混淆',
  encoded_instruction: '编码指令',
  structured_injection: '结构化注入',
  social_engineering: '社会工程',
  tool_output: '工具输出',
}

export function EvaluationsView({ report, externalReport, telemetryReport, user, busy, onRun }: EvaluationsViewProps) {
  const isAdmin = user.roles.includes('admin')
  const [category, setCategory] = useState('all')
  const [suite, setSuite] = useState<'sealed' | 'external' | 'telemetry'>('sealed')
  const sealedSummaryMatchesCases = report
    ? report.case_count === report.cases.length && report.passed_count <= report.case_count
    : true
  const sealedCaseCount = report
    ? (sealedSummaryMatchesCases ? report.case_count : report.cases.length)
    : 0
  const sealedPassedCount = report
    ? (sealedSummaryMatchesCases ? report.passed_count : report.cases.filter((item) => item.passed).length)
    : 0
  const visibleCases = useMemo(
    () => report?.cases.filter((item) => category === 'all' || item.variant_category === category) ?? [],
    [category, report],
  )
  return (
    <div className="page-view eval-view">
      <header className="page-heading eval-heading">
        <div><span className="section-kicker">EVIDENCE-BOUND VALIDATION LAB</span><h1>验证实验室</h1><p>内部夹具验证控制面回归，轻量外部数据验证陌生输入边界，完整遥测验证多模态根因候选。三层结果分开呈现，不把自编题成绩或公开基准冒充生产效果。</p></div>
        {suite === 'sealed' && <div className="eval-actions">
          <button className="button secondary" type="button" disabled={busy || !isAdmin} onClick={() => onRun(true, 3)}>{busy ? <RefreshCw className="spin" size={17} /> : <FlaskConical size={17} />}真实 Qwen 抽样 3 项</button>
          <button className="button primary" type="button" disabled={busy || !isAdmin} onClick={() => onRun(false)}>{busy ? <RefreshCw className="spin" size={17} /> : <Play size={17} />}{busy ? '105 项隔离实验运行中…' : '运行全部 105 项'}</button>
          {!isAdmin && <small>需要 admin 角色启动评测</small>}
          {isAdmin && <small>fixture 全量约需 30–60 秒；真实模型抽样单独报告</small>}
        </div>}
      </header>

      <div className="evaluation-tabs" role="tablist" aria-label="评测套件">
        <button type="button" role="tab" aria-selected={suite === 'sealed'} className={suite === 'sealed' ? 'active' : ''} onClick={() => setSuite('sealed')}><FlaskConical size={15} /><span>内部密封回归</span><small>{report ? `${sealedPassedCount}/${sealedCaseCount}` : '未运行'}</small></button>
        <button type="button" role="tab" aria-selected={suite === 'external'} className={suite === 'external' ? 'active' : ''} onClick={() => setSuite('external')}><Database size={15} /><span>外部数据验证</span><small>{externalReport ? `${externalReport.aggregate.passed_gate_count}/${externalReport.aggregate.gate_count} 门槛` : '未运行'}</small></button>
        <button type="button" role="tab" aria-selected={suite === 'telemetry'} className={suite === 'telemetry' ? 'active' : ''} onClick={() => setSuite('telemetry')}><Activity size={15} /><span>完整遥测 RCA</span><small>{telemetryReport ? `${telemetryReport.gates.filter((item) => item.passed).length}/${telemetryReport.gates.length} 门槛` : '未运行'}</small></button>
      </div>

      {suite === 'telemetry' ? <TelemetryValidationPanel report={telemetryReport} /> : suite === 'external' ? <ExternalValidationPanel report={externalReport} /> : !report ? (
        <section className="empty-state panel"><FlaskConical size={28} /><h2>还没有密封评测证据</h2><p>管理员可先运行可复现夹具套件，再用本地 Qwen 做少量真实模型回归。</p></section>
      ) : (
        <>
          <section className="eval-scoreboard eval-v3-scoreboard">
            <article className="score-hero">
              <div className="score-ring" style={{ '--score': report.score } as CSSProperties}><span><strong>{report.score}</strong><small>总分</small></span></div>
              <div><span className="section-kicker">LATEST SEALED RUN</span><h2>{report.id}</h2><p>{formatDateTime(report.created_at)} · {sealedPassedCount}/{sealedCaseCount} 通过 · P95 {report.p95_case_latency_ms}ms</p><div className="suite-contract"><code className="suite-mode">{report.suite_mode}</code><code>{report.suite_version}</code></div><p className="confidence-copy">95% Wilson 区间 {report.task_success_ci_lower}%–{report.task_success_ci_upper}%</p></div>
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

          <section className="panel category-coverage" aria-labelledby="category-coverage-title">
            <div className="panel-heading"><div><span className="section-kicker">ATTACK SURFACE COVERAGE</span><h2 id="category-coverage-title">攻击类别与样本边界</h2></div><span className="panel-meta">全过也不等于未知生产分布 100% 安全</span></div>
            <ul>
              {Object.entries(report.category_breakdown).map(([name, metric]) => (
                <li key={name}><span>{categoryLabel[name] ?? name}</span><strong>{metric.passed_count}/{metric.case_count}</strong><small>通过率 {metric.pass_rate}% · 越权 {metric.unsafe_action_rate}% · CI {metric.ci_lower}–{metric.ci_upper}%</small></li>
              ))}
            </ul>
          </section>

          <section className="panel eval-cases">
            <div className="panel-heading"><div><span className="section-kicker">ORACLE COMPARISON</span><h2>逐例验证矩阵</h2></div><label className="case-filter">攻击类别<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="all">全部（{report.cases.length}）</option>{Object.entries(report.category_breakdown).map(([name, metric]) => <option key={name} value={name}>{categoryLabel[name] ?? name}（{metric.case_count}）</option>)}</select></label></div>
            <div className="table-wrap" tabIndex={0} aria-label="逐例验证矩阵，可横向滚动">
              <table className="data-table">
                <thead><tr><th>用例 / 变体</th><th>攻击面</th><th>密封故障</th><th>期望 / 实际工具</th><th>根因</th><th>结果</th><th>检索</th><th>安全门</th><th>抗注入</th><th>能力边界</th><th>无越权</th><th>结果</th></tr></thead>
                <tbody>
                  {visibleCases.map((item) => (
                    <tr key={item.case_id}>
                      <td><code>{item.case_id}</code><small className="case-latency">{categoryLabel[item.variant_category] ?? item.variant_category} · {item.latency_ms}ms</small></td>
                      <td><code>{item.attack_surface}</code></td>
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
            <article><span>03</span><div><h3>工具输出也不可信</h3><p>恶意 annotation 真实进入只读工具结果，但不能修改工具白名单、计划哈希、审批或能力令牌。</p></div></article>
            <article><span>04</span><div><h3>样本全过不是绝对安全</h3><p>页面同时展示样本量和 Wilson 区间；fixture 与真实 Qwen 分开报告，避免把控制面回归冒充模型准确率。</p></div></article>
          </section>
        </>
      )}
    </div>
  )
}

function BooleanMark({ value }: { value: boolean }) {
  return <span className={value ? 'boolean-mark yes' : 'boolean-mark no'}>{value ? <Check size={14} /> : <X size={14} />}{value ? '符合' : '不符'}</span>
}
