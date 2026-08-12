import { KeyRound } from 'lucide-react'
import type { RunRecord } from '../../types'
import { formatDuration } from '../../utils'

export function ToolExecutionPanel({ run }: { run: RunRecord }) {
  const capabilityCount = run.tool_results.filter((item) => item.capability_jti).length

  return (
    <section className="panel execution-panel">
      <div className="panel-heading"><div><span className="section-kicker">SIDE-EFFECT PROOF</span><h2>受控工具执行</h2></div><span className="panel-meta"><KeyRound size={14} /> {capabilityCount} capability</span></div>
      <div className="tool-result-list">
        {run.tool_results.map((result, index) => <article key={`${result.step_id}-${index}`}><div className="result-head"><span className={`result-state ${result.status}`}>{result.status}</span><strong>{result.tool_name}</strong><small>{formatDuration(result.duration_ms)}</small></div><p>{result.summary}</p><dl><div><dt>idempotency</dt><dd><code>{result.idempotency_key}</code></dd></div><div><dt>capability jti</dt><dd><code>{result.capability_jti ?? 'read-only / none'}</code></dd></div><div><dt>transport</dt><dd>{result.transport} · attempt {result.attempt}</dd></div></dl></article>)}
        {run.tool_results.length === 0 && <p className="panel-empty">工具尚未执行；调查与审批不会被伪装成副作用成功。</p>}
      </div>
    </section>
  )
}
