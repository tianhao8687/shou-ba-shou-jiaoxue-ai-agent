import { Braces, KeyRound, LockKeyhole, ShieldCheck, TerminalSquare } from 'lucide-react'
import type { ToolSpec, UserIdentity } from '../types'
import { riskLabel } from '../utils'

export function PolicyView({ tools, user }: { tools: ToolSpec[]; user: UserIdentity }) {
  return (
    <div className="page-view policy-view">
      <header className="page-heading"><div><span className="section-kicker">POLICY AS CODE</span><h1>工具与安全策略</h1><p>模型只能生成候选 Plan IR。服务端重新校验依赖、证据、参数与风险，再把获批计划压缩成短效能力令牌；工具端不信任 Agent 身份。</p></div><div className="active-identity"><small>当前身份</small><strong>{user.display_name}</strong><code>{user.roles.join(' + ')}</code></div></header>
      <section className="policy-principles">
        <article><span><Braces size={19} /></span><div><h2>Schema 校验</h2><p>参数字段、类型和范围不符合就拒绝。</p></div></article>
        <article><span><LockKeyhole size={19} /></span><div><h2>最小权限</h2><p>只授予完成当前动作必需的角色。</p></div></article>
        <article><span><KeyRound size={19} /></span><div><h2>能力令牌</h2><p>绑定 run、plan hash、step、参数哈希、角色、过期和 jti。</p></div></article>
        <article><span><ShieldCheck size={19} /></span><div><h2>幂等与未知结果</h2><p>事务内保存副作用；响应丢失时不换 key 盲目重试。</p></div></article>
      </section>
      <section className="panel tools-panel">
        <div className="panel-heading"><div><span className="section-kicker">REGISTERED TOOLS</span><h2>工具注册表</h2></div><span className="panel-meta">{tools.length} 个受控工具</span></div>
        <div className="tool-grid">
          {tools.map((tool) => {
            const fields = Object.keys(tool.input_schema.properties ?? {})
            return (
              <article className="tool-card" key={tool.name}>
                <div className="tool-card-head"><span><TerminalSquare size={18} /></span><div><code>{tool.name}</code><h3>{tool.description}</h3></div><b className={`risk-badge ${tool.risk}`}>{riskLabel[tool.risk]}</b></div>
                <dl><div><dt>所需角色</dt><dd>{tool.required_role}</dd></div><div><dt>执行类型</dt><dd>{tool.read_only ? 'READ ONLY' : 'STATE MUTATION'}</dd></div><div><dt>必填参数</dt><dd>{tool.input_schema.required?.length ?? 0} / {fields.length}</dd></div></dl>
                <p className="tool-applicability"><strong>适用条件</strong>{tool.applicability}</p>
                <div className="schema-fields">{fields.map((field) => <code key={field}>{field}</code>)}</div>
              </article>
            )
          })}
        </div>
      </section>
    </div>
  )
}
