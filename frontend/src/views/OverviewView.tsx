import {
  ArrowUpRight,
  BadgeCheck,
  BookOpenCheck,
  Clock3,
  Cpu,
  Network,
  Radar,
  Route,
  ShieldCheck,
  UserRoundCheck,
} from 'lucide-react'
import { MetricCard } from '../components/MetricCard'
import { RunStatusPill } from '../components/StatusPill'
import type { DashboardMetrics, HealthResponse, RunRecord, ViewName } from '../types'
import { formatDateTime, formatDuration } from '../utils'

interface OverviewProps {
  metrics?: DashboardMetrics
  runs: RunRecord[]
  health?: HealthResponse
  onOpenRun: (runId: string) => void
  onNavigate: (view: ViewName) => void
}

export function OverviewView({ metrics, runs, health, onOpenRun, onNavigate }: OverviewProps) {
  const awaiting = runs.find((run) => run.status === 'awaiting_approval')
  return (
    <div className="overview-view">
      <section className="overview-hero">
        <div className="hero-copy">
          <span className="section-kicker">XIAMEN · AGENT CONTROL PLANE</span>
          <h1>让智能体敢于行动，<br /><em>也知道何时停下。</em></h1>
          <p>Harbor 把异步任务、现场观测、动态计划、策略编译、角色审批、最小权限工具和事后验证连成一条可恢复航道。这里不展示预写答案，只展示运行时证据。</p>
          <div className="runtime-summary">
            <span className={health?.model_runtime.status === 'ready' ? 'ready' : 'degraded'}><Cpu size={14} />模型 {health?.model_runtime.status ?? 'loading'}{health?.model_runtime.device ? ` · ${health.model_runtime.device}` : ''}</span>
            <span className={health?.tool_runtime.status === 'ready' ? 'ready' : 'degraded'}><Network size={14} />工具 {health?.tool_runtime.mode ?? 'loading'}</span>
            <span className={health?.vector_quality === 'semantic' ? 'ready' : 'degraded'}><BookOpenCheck size={14} />{health?.vector_quality === 'semantic' ? '语义向量检索' : '特征哈希检索基线'}</span>
            <span><BookOpenCheck size={14} />{health?.knowledge_chunks ?? 0} 个知识块</span>
          </div>
          <div className="hero-actions">
            <button className="button primary" type="button" onClick={() => awaiting && onOpenRun(awaiting.id)} disabled={!awaiting}>查看待审批运行 <ArrowUpRight size={17} /></button>
            <button className="button ghost" type="button" onClick={() => onNavigate('evaluations')}>查看评测结果</button>
          </div>
        </div>
        <div className="harbor-radar" aria-label="Agent 安全控制示意">
          <div className="radar-ring ring-1" /><div className="radar-ring ring-2" /><div className="radar-ring ring-3" />
          <span className="radar-sweep" />
          <span className="radar-center"><Radar size={24} /></span>
          <span className="radar-point point-a"><BookOpenCheck size={15} /><b>证据</b></span>
          <span className="radar-point point-b"><UserRoundCheck size={15} /><b>审批</b></span>
          <span className="radar-point point-c"><ShieldCheck size={15} /><b>策略</b></span>
          <span className="radar-caption"><small>CONTROL COVERAGE</small><strong>{metrics?.evaluation_score ?? 0}%</strong></span>
        </div>
      </section>

      {metrics && (
        <section className="metrics-row overview-metrics">
          <MetricCard icon={BadgeCheck} label="任务成功率" value={`${metrics.success_rate}%`} detail={`${metrics.completed_runs} 个已完成`} tone="green" />
          <MetricCard icon={ShieldCheck} label="自动评测" value={`${metrics.evaluation_score}%`} detail="检索 · 安全门 · 工具" />
          <MetricCard icon={BookOpenCheck} label="证据覆盖" value={`${metrics.citation_coverage}%`} detail="每次结论可追溯" tone="teal" />
          <MetricCard icon={Clock3} label="P95 节点延迟" value={formatDuration(metrics.p95_latency_ms)} detail="不含人工等待" tone="amber" />
          <MetricCard icon={Cpu} label="模型实时命中" value={`${metrics.model_live_rate}%`} detail={`显式降级 ${metrics.model_fallback_rate}%`} />
          <MetricCard icon={Network} label="恢复执行" value={`${metrics.recovered_runs}`} detail={`队列 ${metrics.queued_runs} · 运行 ${metrics.running_runs}`} tone="teal" />
        </section>
      )}

      <div className="overview-grid">
        <section className="panel recent-runs">
          <div className="panel-heading"><div><span className="section-kicker">LIVE OPERATIONS</span><h2>最近运行</h2></div><button className="text-button" onClick={() => onNavigate('run')}>进入控制台 <ArrowUpRight size={14} /></button></div>
          <div className="run-list">
            {runs.slice(0, 5).map((run) => (
              <button type="button" className="run-list-item" key={run.id} onClick={() => onOpenRun(run.id)}>
                <span className={`severity severity-${run.incident.severity.toLowerCase()}`}>{run.incident.severity}</span>
                <span className="run-list-copy"><strong>{run.incident.title}</strong><small>{run.incident.service} · {formatDateTime(run.updated_at)}</small></span>
                <RunStatusPill status={run.status} />
                <ArrowUpRight size={16} aria-hidden="true" />
              </button>
            ))}
          </div>
        </section>

        <section className="panel coverage-card">
          <div className="panel-heading"><div><span className="section-kicker">WHY IT MATTERS</span><h2>生产级能力覆盖</h2></div></div>
          <div className="coverage-route">
            {[
              ['01', 'Observe', '先执行受控只读调查计划'],
              ['02', 'Plan IR', '证据、依赖、验证与回滚齐全'],
              ['03', 'Compile', '服务端重算风险与计划哈希'],
              ['04', 'Authorize', '角色审批绑定不可变计划'],
              ['05', 'Act', '能力令牌、幂等与工具边界'],
              ['06', 'Recover', 'lease、heartbeat 与 fencing'],
            ].map(([number, title, copy], index) => (
              <div className="route-stop" key={number}>
                <span>{number}</span><div><strong>{title}</strong><small>{copy}</small></div>{index < 5 && <Route size={15} />}
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}
