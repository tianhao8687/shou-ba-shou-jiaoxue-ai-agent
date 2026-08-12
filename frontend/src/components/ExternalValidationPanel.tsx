import { Check, Database, ExternalLink, FileCheck2, ShieldAlert, TriangleAlert, X } from 'lucide-react'
import type { ExternalValidationDataset, ExternalValidationReport } from '../types'
import { formatDateTime } from '../utils'

interface ExternalValidationPanelProps {
  report?: ExternalValidationReport
}

const sourceLabels: Record<string, string> = {
  'loghub-bgl-2k': 'LogPAI · BGL 超算日志',
  'numenta-nab-real': 'Numenta · NAB 真实时序',
  'aiops-challenge-2025': 'AIOps Challenge 2025',
}

const datasetTitles: Record<string, string> = {
  'loghub-bgl-holdout': '日志异常分流：行级留出',
  'nab-real-timeseries-holdout': '因果时序异常检测：整序列留出',
  'aiops-2025-no-evidence-safety': '无遥测安全转人工与真值隔离',
}

function numberMetric(dataset: ExternalValidationDataset, key: string): number {
  const value = dataset.metrics[key]
  return typeof value === 'number' ? value : 0
}

function percent(value: number): string {
  return `${(value * 100).toFixed(value === 0 || value >= 0.995 ? 0 : 1)}%`
}

function datasetMetrics(dataset: ExternalValidationDataset) {
  if (dataset.id === 'loghub-bgl-holdout') {
    const unseen = dataset.metrics.unseen_template_holdout as Record<string, unknown> | undefined
    return [
      ['留出 F1', percent(numberMetric(dataset, 'f1'))],
      ['平衡准确率', percent(numberMetric(dataset, 'balanced_accuracy'))],
      ['未见模板 F1', percent(typeof unseen?.f1 === 'number' ? unseen.f1 : 0)],
    ]
  }
  if (dataset.id === 'nab-real-timeseries-holdout') {
    return [
      ['事件召回', percent(numberMetric(dataset, 'event_recall'))],
      ['告警精确率', percent(numberMetric(dataset, 'alert_precision'))],
      ['每千点误报', numberMetric(dataset, 'false_alerts_per_1000_points').toFixed(2)],
    ]
  }
  return [
    ['安全转人工', percent(numberMetric(dataset, 'insufficient_evidence_abstention_rate'))],
    ['危险写操作', String(numberMetric(dataset, 'unsafe_write_actions'))],
    ['自动故障覆盖', percent(numberMetric(dataset, 'autonomous_taxonomy_coverage'))],
  ]
}

export function ExternalValidationPanel({ report }: ExternalValidationPanelProps) {
  if (!report) {
    return (
      <section className="empty-state panel" role="status">
        <Database size={28} />
        <h2>还没有外部数据证据</h2>
        <p>运行 <code>python scripts/run-external-validation.py</code>，下载固定版本并核对哈希后才会生成报告。</p>
      </section>
    )
  }

  const passed = report.verdict === 'pass'
  const provenanceBySource = report.provenance.reduce<Record<string, ExternalValidationReport['provenance']>>(
    (groups, item) => ({ ...groups, [item.source_id]: [...(groups[item.source_id] ?? []), item] }),
    {},
  )
  return (
    <div className="external-validation">
      <section className={`external-verdict panel ${passed ? 'passed' : 'failed'}`} aria-labelledby="external-verdict-title">
        <span className="external-verdict-icon">{passed ? <FileCheck2 size={24} /> : <TriangleAlert size={24} />}</span>
        <div>
          <span className="section-kicker">PINNED THIRD-PARTY EVIDENCE</span>
          <h2 id="external-verdict-title">{passed ? '公开外部数据基线通过' : '外部数据门槛未通过'}</h2>
          <p>{report.aggregate.passed_gate_count}/{report.aggregate.gate_count} 项预先声明门槛通过；结果生成于 {formatDateTime(report.generated_at)}。这不是企业私有生产数据认证，也不是自动根因修复满分。</p>
        </div>
        <code>{report.suite_version}</code>
      </section>

      <section className="external-facts" aria-label="外部验证摘要">
        <article><Database size={17} /><span>独立来源</span><strong>{report.sources_verified}</strong><small>{report.source_files_verified} 个文件 SHA-256 匹配</small></article>
        <article><FileCheck2 size={17} /><span>外部记录</span><strong>{report.aggregate.total_external_records.toLocaleString()}</strong><small>{report.aggregate.holdout_records.toLocaleString()} 条进入留出评分</small></article>
        <article><ShieldAlert size={17} /><span>危险写操作</span><strong>{report.aggregate.unsafe_write_actions}</strong><small>无证据案例全部只读转人工</small></article>
        <article className="gap"><TriangleAlert size={17} /><span>外部故障自动覆盖</span><strong>{percent(report.aggregate.autonomous_taxonomy_coverage)}</strong><small>{report.aggregate.external_fault_type_count} 种外部故障尚无精确自动修复映射</small></article>
      </section>

      <section className="external-datasets" aria-label="逐数据集结果">
        {report.datasets.map((dataset) => (
          <article className="panel external-dataset" key={dataset.id}>
            <header>
              <div><span className="section-kicker">{sourceLabels[dataset.source_id] ?? dataset.source_id}</span><h3>{datasetTitles[dataset.id] ?? dataset.evaluation_kind}</h3></div>
              <span className={`external-state ${dataset.passed ? 'passed' : 'failed'}`}>{dataset.passed ? <Check size={13} /> : <X size={13} />}{dataset.passed ? '门槛通过' : '未通过'}</span>
            </header>
            <dl>
              {datasetMetrics(dataset).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
            </dl>
            <p>{dataset.record_count.toLocaleString()} 条记录 · {dataset.holdout_count.toLocaleString()} 条留出</p>
            <ul className="external-gates">
              {dataset.gates.map((gate) => <li key={gate.id}><span>{gate.passed ? <Check size={12} /> : <X size={12} />}{gate.id}</span><code>{String(gate.observed)} {gate.operator} {String(gate.threshold)}</code></li>)}
            </ul>
            <details><summary>限制与不能证明的事</summary><ul>{dataset.limitations.map((item) => <li key={item}>{item}</li>)}</ul></details>
          </article>
        ))}
      </section>

      <section className="panel external-provenance">
        <div className="panel-heading"><div><span className="section-kicker">PROVENANCE CHAIN</span><h2>来源、版本与完整性</h2></div><span className="panel-meta">原始数据未提交到仓库</span></div>
        <div className="table-wrap" tabIndex={0} aria-label="外部数据来源表，可横向滚动">
          <table className="data-table"><thead><tr><th>来源</th><th>固定提交</th><th>文件</th><th>哈希</th><th>获取方式</th></tr></thead><tbody>
            {Object.entries(provenanceBySource).map(([sourceId, items]) => {
              const files = items ?? []
              const first = files[0]
              return <tr key={sourceId}><td><strong>{sourceLabels[sourceId] ?? sourceId}</strong></td><td><code>{first?.revision.slice(0, 12)}</code></td><td>{files.length} 个 · {files.reduce((total, item) => total + item.bytes, 0).toLocaleString()} bytes</td><td><span className="boolean-mark yes"><Check size={13} />全部匹配</span></td><td>{first && <a href={first.url} target="_blank" rel="noreferrer">查看原始来源 <ExternalLink size={12} /></a>}</td></tr>
            })}
          </tbody></table>
        </div>
      </section>

      <section className="external-boundaries panel" aria-labelledby="external-boundaries-title">
        <div><TriangleAlert size={20} /><h2 id="external-boundaries-title">结论边界</h2></div>
        <ol>{report.boundaries.map((boundary) => <li key={boundary}>{boundary}</li>)}</ol>
      </section>
    </div>
  )
}
