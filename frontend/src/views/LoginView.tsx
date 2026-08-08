import { FormEvent, useState } from 'react'
import { Anchor, ArrowRight, KeyRound, LockKeyhole, ShieldCheck } from 'lucide-react'
import type { HealthResponse } from '../types'

const accounts = [
  ['viewer@harbor.local', '只读观察员', '只看运行与证据'],
  ['operator@harbor.local', '值班操作员', '执行低风险动作'],
  ['lead@harbor.local', '值班负责人', '审批常规高风险动作'],
  ['security@harbor.local', '安全值班', '审批凭据类动作'],
  ['approver@harbor.local', '独立审批人', '作为第二主体完成四眼审批'],
  ['admin@harbor.local', '平台管理员', '故障实验与评测'],
  ['other@harbor.local', '其他租户', '验证跨租户数据不可见'],
] as const

interface LoginViewProps {
  health?: HealthResponse
  busy: boolean
  error?: string
  onLogin: (username: string, password: string) => Promise<void>
}

export function LoginView({ health, busy, error, onLogin }: LoginViewProps) {
  const [username, setUsername] = useState('admin@harbor.local')
  const [password, setPassword] = useState('harbor-demo-2026')

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void onLogin(username, password)
  }

  return (
    <main className="login-page">
      <section className="login-story" aria-labelledby="login-title">
        <div className="login-brand"><span><Anchor size={25} /></span><strong>Harbor AgentOps</strong><small>PRODUCTION CONTROL PLANE</small></div>
        <div className="login-copy">
          <span className="section-kicker">XIAMEN · LOCAL-FIRST AIOPS AGENT</span>
          <h1 id="login-title">让 Agent 的每一步，<br /><em>都有证据和边界。</em></h1>
          <p>自由事故输入进入持久任务队列。模型先调查、再诊断；策略编译器验证计划，高风险动作必须由两个独立主体审批，最后回到原始指标验证结果。</p>
        </div>
        <ol className="login-contract">
          <li><span>01</span><div><strong>证据约束计划</strong><small>每个动作绑定 observation 与 Runbook 引用</small></div></li>
          <li><span>02</span><div><strong>职责分离执行</strong><small>租户边界 + 两个独立审批主体 + 短效能力令牌</small></div></li>
          <li><span>03</span><div><strong>可恢复与可证明</strong><small>lease + fencing + 幂等 + 隐藏真值评测</small></div></li>
        </ol>
        <div className="login-runtime">
          <span className={health?.model_runtime.status === 'ready' ? 'ready' : 'degraded'}>{health?.model_runtime.status === 'ready' ? '●' : '▲'} 模型 {String(health?.model_runtime.status ?? '检测中')}</span>
          <span>{health?.version ? `API v${health.version}` : 'API 检测中'}</span>
          <span>{health?.vector_quality === 'semantic' ? '语义向量' : '检索基线已如实标记'}</span>
        </div>
      </section>

      <section className="login-panel" aria-label="登录 Harbor AgentOps">
        <div className="login-panel-heading"><span><LockKeyhole size={20} /></span><div><h2>身份验证</h2><p>选择一个角色，观察服务端权限如何改变操作边界。</p></div></div>
        <div className="account-picker" role="radiogroup" aria-label="演示身份">
          {accounts.map(([email, name, detail]) => (
            <button key={email} type="button" role="radio" aria-checked={username === email} className={username === email ? 'account-option selected' : 'account-option'} onClick={() => setUsername(email)}>
              <span>{name.slice(0, 1)}</span><div><strong>{name}</strong><small>{detail}</small></div><code>{email.split('@')[0]}</code>
            </button>
          ))}
        </div>
        <form className="login-form" onSubmit={submit}>
          <label>账号<input type="email" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required /></label>
          <label>演示密码<span className="password-field"><KeyRound size={16} /><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required minLength={8} /></span></label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="button primary login-submit" type="submit" disabled={busy}>{busy ? '验证中…' : '进入控制塔'}<ArrowRight size={17} /></button>
        </form>
        <p className="login-security"><ShieldCheck size={14} /> 密码只用于本地演示；服务端签发带过期时间的 HMAC 身份令牌。</p>
      </section>
    </main>
  )
}
