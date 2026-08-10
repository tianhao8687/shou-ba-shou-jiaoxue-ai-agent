import { useCallback, useEffect, useMemo, useState } from 'react'
import { CircleHelp, FilePlus2, LogOut, RefreshCw, ShipWheel, UserRound } from 'lucide-react'
import { ApiError, api, authSession } from './api'
import { IncidentComposer } from './components/IncidentComposer'
import { Sidebar } from './components/Sidebar'
import type {
  DashboardMetrics,
  DrillTemplate,
  EvaluationReport,
  ExternalValidationReport,
  HealthResponse,
  Incident,
  JobRecord,
  KnowledgeDoc,
  RunRecord,
  ToolSpec,
  UserIdentity,
  ViewName,
} from './types'
import { EvaluationsView } from './views/EvaluationsView'
import { KnowledgeView } from './views/KnowledgeView'
import { LoginView } from './views/LoginView'
import { OverviewView } from './views/OverviewView'
import { PolicyView } from './views/PolicyView'
import { RunView } from './views/RunView'

const pageTitles: Record<ViewName, string> = {
  overview: '运行总览',
  run: '运行控制台',
  evaluations: '验证实验室',
  knowledge: '知识与检索',
  policy: '策略与权限',
}

export function jobsForRun(
  runId: string | undefined,
  jobsByRun: Record<string, JobRecord[]>,
): JobRecord[] {
  return runId ? jobsByRun[runId] ?? [] : []
}

export default function App() {
  const [view, setView] = useState<ViewName>('run')
  const [user, setUser] = useState<UserIdentity>()
  const [runs, setRuns] = useState<RunRecord[]>([])
  // Keep job responses keyed by run so an older, slower request can never be
  // rendered under a newly selected run.
  const [jobsByRun, setJobsByRun] = useState<Record<string, JobRecord[]>>({})
  const [drills, setDrills] = useState<DrillTemplate[]>([])
  const [metrics, setMetrics] = useState<DashboardMetrics>()
  const [evaluation, setEvaluation] = useState<EvaluationReport | null>(null)
  const [externalValidation, setExternalValidation] = useState<ExternalValidationReport | null>(null)
  const [health, setHealth] = useState<HealthResponse>()
  const [documents, setDocuments] = useState<KnowledgeDoc[]>([])
  const [tools, setTools] = useState<ToolSpec[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string>()
  const [composerOpen, setComposerOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [toast, setToast] = useState<string>()

  const clearProtectedState = useCallback(() => {
    setRuns([])
    setJobsByRun({})
    setDrills([])
    setMetrics(undefined)
    setEvaluation(null)
    setExternalValidation(null)
    setDocuments([])
    setTools([])
    setSelectedRunId(undefined)
    setComposerOpen(false)
  }, [])

  const logout = useCallback((message?: string) => {
    authSession.clear()
    setUser(undefined)
    clearProtectedState()
    setError(message)
  }, [clearProtectedState])

  const handleFailure = useCallback((cause: unknown, fallback: string) => {
    if (cause instanceof ApiError && cause.status === 401) {
      logout('登录已过期，请重新验证身份。')
      return
    }
    setError(cause instanceof Error ? cause.message : fallback)
  }, [logout])

  const loadProtected = useCallback(async () => {
    const [nextRuns, nextMetrics, nextEvaluation, nextExternalValidation, nextDocuments, nextTools, nextDrills] = await Promise.all([
      api.runs(), api.metrics(), api.latestEvaluation(), api.latestExternalValidation(), api.knowledge(), api.tools(), api.drills(),
    ])
    setRuns(nextRuns)
    setMetrics(nextMetrics)
    setEvaluation(nextEvaluation)
    setExternalValidation(nextExternalValidation)
    setDocuments(nextDocuments)
    setTools(nextTools)
    setDrills(nextDrills)
    setSelectedRunId((current) => current ?? nextRuns.find((run) => run.status === 'awaiting_approval')?.id ?? nextRuns[0]?.id)
  }, [])

  useEffect(() => {
    let active = true
    const bootstrap = async () => {
      try {
        const nextHealth = await api.health()
        if (active) setHealth(nextHealth)
      } catch { /* login remains usable while the status panel reports no health */ }
      if (!authSession.token()) {
        if (active) setLoading(false)
        return
      }
      try {
        const identity = await api.me()
        if (!active) return
        setUser(identity)
        await loadProtected()
        if (active) setError(undefined)
      } catch (cause) {
        if (active) handleFailure(cause, '无法恢复登录状态')
      } finally {
        if (active) setLoading(false)
      }
    }
    void bootstrap()
    return () => { active = false }
  }, [handleFailure, loadProtected])

  const selectedRun = useMemo(() => runs.find((run) => run.id === selectedRunId), [runs, selectedRunId])
  const selectedJobs = jobsForRun(selectedRunId, jobsByRun)
  const hasActiveRun = runs.some((run) => run.status === 'queued' || run.status === 'running')
  const canOperate = Boolean(user && (user.roles.includes('operator') || user.roles.includes('admin')))

  const refreshOperations = useCallback(async () => {
    if (!user) return
    try {
      const [nextRuns, nextMetrics, nextHealth] = await Promise.all([api.runs(), api.metrics(), api.health()])
      setRuns(nextRuns)
      setMetrics(nextMetrics)
      setHealth(nextHealth)
      if (selectedRunId) {
        const nextJobs = await api.jobs(selectedRunId)
        setJobsByRun((current) => ({ ...current, [selectedRunId]: nextJobs }))
      }
      setError(undefined)
    } catch (cause) {
      handleFailure(cause, '刷新运行失败')
    }
  }, [handleFailure, selectedRunId, user])

  useEffect(() => {
    if (!user) return
    const timer = window.setInterval(() => { void refreshOperations() }, hasActiveRun ? 2_000 : 10_000)
    return () => window.clearInterval(timer)
  }, [hasActiveRun, refreshOperations, user])

  useEffect(() => {
    if (!user || !selectedRunId) return
    const runId = selectedRunId
    // Blank the first render for an unseen run instead of leaking the
    // previously selected run's lease and fencing metadata into this view.
    setJobsByRun((current) => runId in current ? current : { ...current, [runId]: [] })
    void api.jobs(runId)
      .then((nextJobs) => setJobsByRun((current) => ({ ...current, [runId]: nextJobs })))
      .catch((cause) => handleFailure(cause, '无法加载任务租约'))
  }, [handleFailure, selectedRunId, user])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(undefined), 3600)
    return () => window.clearTimeout(timer)
  }, [toast])

  const login = async (username: string, password: string) => {
    setBusy(true)
    setError(undefined)
    try {
      const auth = await api.login(username, password)
      authSession.save(auth.access_token)
      // Never render one tenant's cached protected state under another identity.
      clearProtectedState()
      setUser(auth.user)
      await loadProtected()
      setToast(`已以${auth.user.display_name}身份登录。`)
    } catch (cause) {
      authSession.clear()
      setError(cause instanceof Error ? cause.message : '登录失败')
    } finally {
      setBusy(false)
    }
  }

  const startRun = async (incident: Incident) => {
    setBusy(true)
    setError(undefined)
    try {
      const run = await api.startRun(incident)
      setJobsByRun((current) => ({ ...current, [run.id]: [] }))
      setSelectedRunId(run.id)
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)])
      setComposerOpen(false)
      setView('run')
      await refreshOperations()
      setToast('事件已写入持久任务队列，worker 将异步认领。')
    } catch (cause) {
      handleFailure(cause, '启动失败')
      throw cause
    } finally {
      setBusy(false)
    }
  }

  const createDrill = async (faultKind: string): Promise<Incident> => {
    const descriptor = await api.createDrill(faultKind)
    return descriptor.incident
  }

  const decide = async (decision: 'approve' | 'deny', note: string) => {
    if (!selectedRun) return
    setBusy(true)
    try {
      const updated = await api.decide(selectedRun.id, decision, note, selectedRun.version)
      setRuns((current) => current.map((run) => run.id === updated.id ? updated : run))
      await refreshOperations()
      setToast(decision === 'approve' ? '审批绑定当前计划哈希，恢复任务已入队。' : '执行已拒绝，写操作保持未执行。')
    } catch (cause) {
      handleFailure(cause, '审批失败')
      await refreshOperations()
    } finally {
      setBusy(false)
    }
  }

  const retry = async () => {
    if (!selectedRun) return
    setBusy(true)
    try {
      const updated = await api.retry(selectedRun.id, selectedRun.version)
      setRuns((current) => current.map((run) => run.id === updated.id ? updated : run))
      setToast('重试任务已入队；幂等键会阻止重复副作用。')
      await refreshOperations()
    } catch (cause) { handleFailure(cause, '重试失败') } finally { setBusy(false) }
  }

  const cancel = async () => {
    if (!selectedRun) return
    setBusy(true)
    try {
      const updated = await api.cancel(selectedRun.id, selectedRun.version)
      setRuns((current) => current.map((run) => run.id === updated.id ? updated : run))
      setToast('取消请求已记录，worker 会在安全检查点停止。')
      await refreshOperations()
    } catch (cause) { handleFailure(cause, '取消失败') } finally { setBusy(false) }
  }

  const runEvaluation = async (liveModel: boolean, caseLimit?: number) => {
    setBusy(true)
    try {
      const report = await api.runEvaluation(liveModel, caseLimit)
      setEvaluation(report)
      setMetrics(await api.metrics())
      setToast(`密封评测完成：${report.score} 分，${report.cases.length} 个隔离实验。`)
    } catch (cause) { handleFailure(cause, '评测失败') } finally { setBusy(false) }
  }

  const downloadEvidence = async () => {
    if (!selectedRun) return
    try {
      const bundle = await api.evidence(selectedRun.id)
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `${selectedRun.id}-evidence.json`
      link.click()
      URL.revokeObjectURL(url)
      setToast(bundle.runtime_leak_check.passed ? '证据包已导出，答案泄漏扫描通过。' : '证据包已导出，但泄漏扫描未通过。')
    } catch (cause) { handleFailure(cause, '证据导出失败') }
  }

  const openRun = (runId: string) => { setSelectedRunId(runId); setView('run') }

  if (loading) return <section className="loading-state app-boot"><span className="loading-radar" /><strong>正在校验本地运行时</strong><p>检查 API、身份会话与任务存储…</p></section>
  if (!user) return <LoginView health={health} busy={busy} error={error} onLogin={login} />

  return (
    <div className="app-shell">
      <Sidebar view={view} onChange={setView} health={health} />
      <div className="app-main">
        <header className="topbar">
          <div className="topbar-title"><ShipWheel size={20} aria-hidden="true" /><strong>{pageTitles[view]}</strong></div>
          <div className="topbar-actions">
            <span className={`runtime-chip ${health?.model_runtime.status === 'ready' ? 'ready' : 'degraded'}`}><i />{health?.model_runtime.status === 'ready' ? String(health.model_runtime.model ?? '本地模型') : `模型 ${String(health?.model_runtime.status ?? '未知')}`}</span>
            {canOperate && <button className="button primary topbar-run" type="button" onClick={() => setComposerOpen(true)} disabled={busy}><FilePlus2 size={16} />新建事件</button>}
            <span className="identity-chip"><UserRound size={15} /><span><strong>{user.display_name}</strong><small>{user.tenant_id} · {user.roles.join(' · ')}</small></span></span>
            <button className="icon-button" type="button" onClick={() => logout()} aria-label="退出登录"><LogOut size={17} /></button>
            <a className="icon-button topbar-docs" href="http://localhost:8000/docs" target="_blank" rel="noreferrer" aria-label="打开 API 文档"><CircleHelp size={18} /></a>
          </div>
        </header>
        <div className="coordinate-ruler" aria-hidden="true"><span>118°00′E</span><span>JOB LEASE</span><span>PLAN HASH</span><span>118°15′E</span></div>

        <main className="content-area">
          {error && <div className="inline-alert" role="alert"><strong>操作未完成</strong><span>{error}</span><button type="button" onClick={() => setError(undefined)}>关闭</button></div>}
          {view === 'overview' && <OverviewView metrics={metrics} runs={runs} health={health} onOpenRun={openRun} onNavigate={setView} />}
          {view === 'run' && <RunView run={selectedRun} jobs={selectedJobs} metrics={metrics} health={health} user={user} busy={busy} onDecision={decide} onRetry={retry} onCancel={cancel} onDownloadEvidence={downloadEvidence} onRefresh={refreshOperations} onCreate={() => setComposerOpen(true)} />}
          {view === 'evaluations' && <EvaluationsView report={evaluation ?? undefined} externalReport={externalValidation ?? undefined} user={user} busy={busy} onRun={runEvaluation} />}
          {view === 'knowledge' && <KnowledgeView documents={documents} health={health} />}
          {view === 'policy' && <PolicyView tools={tools} user={user} />}
        </main>
      </div>
      {canOperate && composerOpen && <IncidentComposer user={user} drills={drills} busy={busy} onClose={() => setComposerOpen(false)} onSubmit={startRun} onCreateDrill={createDrill} />}
      {toast && <div className="toast" role="status">{toast}</div>}
      {hasActiveRun && <div className="poll-indicator" aria-live="polite"><RefreshCw className="spin" size={12} />2 秒轮询任务状态</div>}
    </div>
  )
}
