import { ShieldCheck } from 'lucide-react'
import type { VerificationResult } from '../../types'

export function VerificationPanel({ verification }: { verification: VerificationResult[] }) {
  return (
    <section className="panel verification-panel">
      <div className="panel-heading"><div><span className="section-kicker">INDEPENDENT RECHECK</span><h2>执行后重新验证</h2></div><span className="panel-meta">{verification.filter((item) => item.passed).length}/{verification.length} 通过</span></div>
      <div className="verification-list">
        {verification.map((item) => <article className={item.passed ? 'passed' : 'failed'} key={item.step_id}><ShieldCheck size={16} /><div><strong>{item.passed ? '验证通过' : '验证失败'} · {item.step_id}</strong><p>{item.summary}</p>{item.checks.map((check, index) => <code key={index}>{JSON.stringify(check)}</code>)}</div></article>)}
        {verification.length === 0 && <p className="panel-empty">副作用完成后会使用独立观测重新检查成功条件。</p>}
      </div>
    </section>
  )
}
