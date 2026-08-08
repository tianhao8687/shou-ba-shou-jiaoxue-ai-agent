import { FormEvent, useEffect, useMemo, useState } from 'react'
import { Beaker, FileWarning, RefreshCw, ShieldCheck, X } from 'lucide-react'
import type { DrillTemplate, Incident, UserIdentity } from '../types'

interface IncidentComposerProps {
  user: UserIdentity
  drills: DrillTemplate[]
  busy: boolean
  onClose: () => void
  onSubmit: (incident: Incident) => Promise<void>
  onCreateDrill: (faultKind: string) => Promise<Incident>
}

const initialIncident: Incident = {
  title: '支付服务出现持续错误与延迟异常',
  summary: '生产告警显示支付请求错误率持续升高，值班人员尚未确认根因，请先收集证据再提出处置方案。',
  severity: 'P2',
  service: 'payment-api',
  environment: 'production',
  symptoms: ['5xx 错误率在 10 分钟内持续上升', 'P95 延迟超过日常基线'],
  tags: ['freeform', 'on-call'],
}

export function IncidentComposer({ user, drills, busy, onClose, onSubmit, onCreateDrill }: IncidentComposerProps) {
  const isAdmin = user.roles.includes('admin')
  const [mode, setMode] = useState<'freeform' | 'lab'>('freeform')
  const [form, setForm] = useState<Incident>(initialIncident)
  const [symptoms, setSymptoms] = useState(initialIncident.symptoms.join('\n'))
  const [tags, setTags] = useState(initialIncident.tags.join(', '))
  const [faultKind, setFaultKind] = useState(drills[0]?.id ?? 'connection_pool_exhaustion')
  const [error, setError] = useState<string>()

  useEffect(() => {
    const close = (event: KeyboardEvent) => event.key === 'Escape' && !busy && onClose()
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [busy, onClose])

  const selectedDrill = useMemo(() => drills.find((item) => item.id === faultKind), [drills, faultKind])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(undefined)
    try {
      if (mode === 'lab') {
        const incident = await onCreateDrill(faultKind)
        await onSubmit(incident)
        return
      }
      const cleanedSymptoms = symptoms.split('\n').map((item) => item.trim()).filter(Boolean)
      if (cleanedSymptoms.length === 0) throw new Error('至少填写一个可观察症状。')
      await onSubmit({
        ...form,
        symptoms: cleanedSymptoms,
        tags: tags.split(',').map((item) => item.trim()).filter(Boolean),
        experiment_id: null,
      })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法创建运行')
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && !busy && onClose()}>
      <section className="incident-dialog" role="dialog" aria-modal="true" aria-labelledby="incident-dialog-title">
        <header className="dialog-heading">
          <div><span className="section-kicker">RUNTIME INPUT</span><h2 id="incident-dialog-title">创建 Agent 运行</h2><p>运行输入不包含标准答案或预写计划，Agent 必须从现场观察中推导。</p></div>
          <button className="icon-button" type="button" aria-label="关闭" onClick={onClose} disabled={busy}><X size={18} /></button>
        </header>
        <div className="composer-tabs" role="tablist" aria-label="事件来源">
          <button type="button" role="tab" aria-selected={mode === 'freeform'} className={mode === 'freeform' ? 'active' : ''} onClick={() => setMode('freeform')}><FileWarning size={16} />自由事件</button>
          {isAdmin && <button type="button" role="tab" aria-selected={mode === 'lab'} className={mode === 'lab' ? 'active' : ''} onClick={() => setMode('lab')}><Beaker size={16} />受控故障实验</button>}
        </div>

        <form className="incident-form" onSubmit={submit}>
          {mode === 'freeform' ? (
            <>
              <div className="form-grid two"><label>事件标题<input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} minLength={6} maxLength={160} required /></label><label>服务标识<input value={form.service} onChange={(event) => setForm({ ...form, service: event.target.value.toLowerCase() })} pattern="[a-z0-9][a-z0-9-]{1,79}" required /></label></div>
              <label>现场摘要<textarea value={form.summary} onChange={(event) => setForm({ ...form, summary: event.target.value })} rows={3} minLength={12} maxLength={4000} required /></label>
              <div className="form-grid three">
                <label>严重级别<select value={form.severity} onChange={(event) => setForm({ ...form, severity: event.target.value as Incident['severity'] })}><option>P1</option><option>P2</option><option>P3</option></select></label>
                <label>环境<select value={form.environment} onChange={(event) => setForm({ ...form, environment: event.target.value as Incident['environment'] })}><option value="production">production</option><option value="staging">staging</option><option value="lab">lab</option></select></label>
                <label>标签（逗号分隔）<input value={tags} onChange={(event) => setTags(event.target.value)} /></label>
              </div>
              <label>可观察症状（每行一条）<textarea value={symptoms} onChange={(event) => setSymptoms(event.target.value)} rows={4} maxLength={3600} required /></label>
              <aside className="truth-boundary"><ShieldCheck size={17} /><div><strong>答案隔离边界</strong><p>这里只提交事实与症状。表单没有根因、目标工具或期望结果字段，后端也会对证据包做泄漏扫描。</p></div></aside>
            </>
          ) : (
            <div className="lab-composer">
              <div className="lab-warning"><Beaker size={21} /><div><strong>Sealed Fault Lab</strong><p>后端创建隔离实验并保存隐藏 oracle。Agent 只会收到事件描述和 experiment_id；评测器在运行结束后才读取真值。</p></div></div>
              <label>故障类型<select value={faultKind} onChange={(event) => setFaultKind(event.target.value)}>{drills.map((drill) => <option key={drill.id} value={drill.id}>{drill.title} · {drill.service}</option>)}</select></label>
              <dl className="lab-contract"><div><dt>注入对象</dt><dd>{selectedDrill?.service ?? '—'}</dd></div><div><dt>真值可见性</dt><dd>仅 oracle token 可读</dd></div><div><dt>副作用</dt><dd>隔离状态机，可重复重置</dd></div></dl>
            </div>
          )}
          {error && <p className="form-error" role="alert">{error}</p>}
          <footer className="dialog-actions"><button type="button" className="button secondary" onClick={onClose} disabled={busy}>取消</button><button type="submit" className="button primary" disabled={busy}>{busy && <RefreshCw className="spin" size={16} />}{busy ? '正在提交…' : mode === 'lab' ? '注入故障并入队' : '提交自由事件'}</button></footer>
        </form>
      </section>
    </div>
  )
}
