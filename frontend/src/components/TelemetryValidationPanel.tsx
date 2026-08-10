import { Activity, Check, Database, FileCheck2, Gauge, GitCompareArrows, ShieldCheck, TriangleAlert, X } from 'lucide-react'
import type { TelemetryValidationReport } from '../types'

interface TelemetryValidationPanelProps {
  report?: TelemetryValidationReport
}

const metricLabel: Record<string, string> = {
  archive_hashes_verified: '归档哈希一致',
  holdout_case_count: '留出样本量',
  prediction_freeze_verified: '预测先于答案冻结',
  oracle_leaks: '答案字段泄漏',
  unsafe_write_actions: '危险写操作',
  multimodal_case_coverage: '多模态证据覆盖',
  entity_top3_accuracy: '实体 Top-3',
  fault_type_top3_accuracy: '故障类型 Top-3',
  exact_rca_top1_accuracy: '完整 RCA Top-1',
  evidence_modality_recall: '证据模态召回',
}

function percent(value: number) {
  return `${(value * 100).toFixed(2)}%`
}

function count(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value)
}

function gigabytes(value: number) {
  return `${(value / 1_000_000_000).toFixed(2)} GB`
}

const boundaryTranslations: Record<string, string> = {
  'AIOps2025 is a third-party chaos-injected benchmark, not private enterprise production traffic.': 'AIOps2025 是第三方混沌注入基准，不是企业私有生产流量。',
  'The deterministic baseline diagnoses only; it performs no remediation or write operation.': '确定性基线只做诊断，不执行修复或写操作。',
  'The manifest separates calibration, one or more consumed validation days, and one untouched final holdout day.': '清单把校准日、已使用的验证日和未见的最终留出日严格分开。',
  'A failed quality gate is retained as evidence instead of being hidden or retuned on the holdout.': '失败门槛会保留为证据，不会在最终留出集上继续调参。',
  'CC BY-NC 4.0 raw archives remain in the ignored local cache and are not committed.': 'CC BY-NC 4.0 原始归档只留在 Git 忽略的本地缓存中。',
}

export function TelemetryValidationPanel({ report }: TelemetryValidationPanelProps) {
  if (!report) return (
    <section className="empty-state panel" role="status">
      <Activity size={28} />
      <h2>还没有完整遥测证据</h2>
      <p>运行 telemetry-v4 后，这里会显示真实日志、指标和调用链的冻结盲测结果。</p>
    </section>
  )

  const holdoutCases = report.cases.filter((item) => item.role === 'holdout')
  const passedGates = report.gates.filter((item) => item.passed).length
  const replay = report.replay_audit
  const currentMetrics = replay?.holdout ?? report.holdout
  const allPassed = report.verdict === 'pass' && (!replay || replay.verdict === 'pass')
  return (
    <div className="telemetry-validation">
      <section className={`panel telemetry-verdict ${allPassed ? 'passed' : 'failed'}`} aria-labelledby="telemetry-verdict-title">
        <span className="telemetry-verdict-icon" aria-hidden="true">{allPassed ? <FileCheck2 /> : <TriangleAlert />}</span>
        <div>
          <span className="section-kicker">BLIND SNAPSHOT + REPRODUCIBLE REPLAY</span>
          <h2 id="telemetry-verdict-title">{allPassed ? '完整遥测门槛通过，复现缺陷已闭环' : '完整遥测验证仍有未通过项'}</h2>
          <p>{count(report.coverage.all_rows)} 行第三方遥测 · {report.holdout.case_count} 个最终案例 · 原始盲测与修复后重放均为 {passedGates}/{report.gates.length} 门槛。分数下降和工程缺陷都如实保留。</p>
        </div>
        <code>{(replay?.semantic_fingerprint ?? report.prediction_fingerprint).slice(0, 12)}</code>
      </section>

      {replay && <section className="panel telemetry-replay" aria-labelledby="telemetry-replay-title">
        <div>
          <span className="section-kicker">POST-FREEZE REPRODUCIBILITY AUDIT</span>
          <h2 id="telemetry-replay-title">并列排序已固定，{replay.replay_count} 次重放语义一致</h2>
          <p>完整文件会因生成时间和实测耗时而不同；排除这两个运行字段后，语义 SHA-256 都是 <code>{replay.semantic_fingerprint.slice(0, 16)}…</code>。</p>
        </div>
        <dl>
          <div><dt>故障 Top-3</dt><dd>{percent(report.holdout.fault_type_top3_accuracy)} <span>→</span> <strong>{percent(replay.holdout.fault_type_top3_accuracy)}</strong></dd></div>
          <div><dt>完整 Top-1</dt><dd>{percent(report.holdout.exact_rca_top1_accuracy)} <span>→</span> <strong>{percent(replay.holdout.exact_rca_top1_accuracy)}</strong></dd></div>
          <div><dt>重放门槛</dt><dd><strong>{replay.passed_gates}/{replay.gate_count}</strong></dd></div>
        </dl>
        <p className="telemetry-replay-note">答案解封后只做通用排序修复，没有按标签调分；所以右侧是“可复现重放审计”，不冒充第二次盲测。</p>
      </section>}

      <section className="telemetry-scoreline" aria-label={replay ? '修复后可复现运行线关键指标' : '最终留出集关键指标'}>
        <article><Gauge size={17} /><span>故障类型 Top-3</span><strong>{percent(currentMetrics.fault_type_top3_accuracy)}</strong><small>Top-1 {percent(currentMetrics.fault_type_top1_accuracy)} · 可复现线</small></article>
        <article><GitCompareArrows size={17} /><span>实体 Top-3</span><strong>{percent(currentMetrics.entity_top3_accuracy)}</strong><small>Top-1 {percent(currentMetrics.entity_top1_accuracy)} · 可复现线</small></article>
        <article><ShieldCheck size={17} /><span>完整 RCA Top-1</span><strong>{percent(currentMetrics.exact_rca_top1_accuracy)}</strong><small>实体与故障同时正确</small></article>
        <article><Activity size={17} /><span>证据模态召回</span><strong>{percent(currentMetrics.evidence_modality_recall)}</strong><small>P95 {replay ? `${replay.p95_case_latency_ms_range[0]}–${replay.p95_case_latency_ms_range[1]}` : report.holdout.p95_case_latency_ms}ms</small></article>
      </section>

      <section className="panel telemetry-protocol" aria-labelledby="telemetry-protocol-title">
        <div className="panel-heading"><div><span className="section-kicker">LEAKAGE-CONTROLLED PROTOCOL</span><h2 id="telemetry-protocol-title">校准、验证、最终留出严格分层</h2></div><span className="panel-meta">预测 SHA-256 冻结后才打开答案</span></div>
        <ol>
          <li><span>01</span><strong>校准</strong><small>{report.calibration.case_count} 例 · 规则设计</small></li>
          <li><span>02</span><strong>验证</strong><small>{report.validation.case_count} 例 · 错误分析</small></li>
          <li><span>03</span><strong>冻结预测</strong><small><code>{report.prediction_fingerprint.slice(0, 12)}…</code></small></li>
          <li><span>04</span><strong>最终留出</strong><small>{report.holdout.case_count} 例 · 不再调参</small></li>
          <li><span>05</span><strong>只读评分</strong><small>写操作 {report.protocol.unsafe_write_actions}</small></li>
        </ol>
      </section>

      <div className="telemetry-detail-grid">
        <section className="panel telemetry-modalities" aria-labelledby="telemetry-modalities-title">
          <div className="panel-heading"><div><span className="section-kicker">ACTUAL ROW COVERAGE</span><h2 id="telemetry-modalities-title">三类真实遥测</h2></div><Database size={18} /></div>
          <dl>
            <div><dt>日志 Logs</dt><dd>{count(report.coverage.total_rows.logs)}</dd></div>
            <div><dt>指标 Metrics</dt><dd>{count(report.coverage.total_rows.metrics)}</dd></div>
            <div><dt>链路 Traces</dt><dd>{count(report.coverage.total_rows.traces)}</dd></div>
          </dl>
          <p>{Object.keys(report.coverage.by_archive).length} 个官方日包 · {gigabytes(report.coverage.archive_bytes)} 压缩归档 · 原始数据未提交到 Git</p>
        </section>
        <section className="panel telemetry-gates" aria-labelledby="telemetry-gates-title">
          <div className="panel-heading"><div><span className="section-kicker">PREDECLARED QUALITY GATES</span><h2 id="telemetry-gates-title">冻结门槛</h2></div><strong>{passedGates}/{report.gates.length}</strong></div>
          <ul>{report.gates.map((gate) => <li key={gate.id}><span>{gate.passed ? <Check size={13} /> : <X size={13} />}{metricLabel[gate.id] ?? gate.id}</span><code>{String(gate.observed)} {gate.operator} {String(gate.threshold)}</code></li>)}</ul>
        </section>
      </div>

      <section className="panel telemetry-cases" aria-labelledby="telemetry-cases-title">
        <div className="panel-heading"><div><span className="section-kicker">ORIGINAL BLIND CASE LEDGER</span><h2 id="telemetry-cases-title">原始盲测逐例账本</h2></div><span className="panel-meta">冻结快照保持不变；不展示答案文本</span></div>
        <div className="table-wrap" tabIndex={0} aria-label="最终留出逐例账本，可横向滚动">
          <table className="data-table"><thead><tr><th>案例</th><th>预测故障</th><th>预测实体</th><th>证据</th><th>故障 Top-3</th><th>实体 Top-3</th><th>完整 Top-1</th></tr></thead>
            <tbody>{holdoutCases.map((item) => <tr key={item.uuid}><td><code>{item.uuid}</code></td><td>{item.predicted_fault_type}</td><td><code>{item.predicted_entity}</code></td><td>{item.evidence_modalities.join(' · ') || '无'}</td><td><Result value={item.fault_type_top3} /></td><td><Result value={item.entity_top3} /></td><td><Result value={item.exact_rca_top1} /></td></tr>)}</tbody>
          </table>
        </div>
      </section>

      <section className="panel telemetry-boundary"><TriangleAlert size={18} /><div><h2>能力边界仍然保留</h2><p>{report.boundaries.map((item) => boundaryTranslations[item] ?? item).join(' ')}</p></div><a href={report.source.repository_url} target="_blank" rel="noreferrer">查看官方数据源</a></section>
    </div>
  )
}

function Result({ value }: { value: boolean }) {
  return <span className={`telemetry-result ${value ? 'yes' : 'no'}`}>{value ? <Check size={13} /> : <X size={13} />}{value ? '命中' : '未命中'}</span>
}
