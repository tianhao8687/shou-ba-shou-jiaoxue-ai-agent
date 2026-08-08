import {
  Anchor,
  BookOpen,
  ChartNoAxesCombined,
  Gauge,
  Play,
  ShieldCheck,
} from 'lucide-react'
import type { ViewName } from '../types'
import type { HealthResponse } from '../types'

interface SidebarProps {
  view: ViewName
  onChange: (view: ViewName) => void
  health?: HealthResponse
}

const items: Array<{ id: ViewName; label: string; icon: typeof Gauge }> = [
  { id: 'overview', label: '总览', icon: Gauge },
  { id: 'run', label: '运行', icon: Play },
  { id: 'evaluations', label: '评测', icon: ChartNoAxesCombined },
  { id: 'knowledge', label: '知识库', icon: BookOpen },
  { id: 'policy', label: '策略', icon: ShieldCheck },
]

export function Sidebar({ view, onChange, health }: SidebarProps) {
  return (
    <aside className="sidebar">
      <div className="brand" aria-label="Harbor AgentOps">
        <span className="brand-mark"><Anchor aria-hidden="true" size={23} /></span>
        <span className="brand-copy"><strong>Harbor</strong><strong>AgentOps</strong></span>
      </div>

      <nav className="primary-nav" aria-label="主导航">
        {items.map((item) => {
          const Icon = item.icon
          return (
            <button
              key={item.id}
              type="button"
              className={view === item.id ? 'nav-item active' : 'nav-item'}
              onClick={() => onChange(item.id)}
              aria-current={view === item.id ? 'page' : undefined}
            >
              <Icon size={19} strokeWidth={1.8} aria-hidden="true" />
              <span>{item.label}</span>
            </button>
          )
        })}
      </nav>

      <div className="sidebar-status">
        <span className={`live-dot ${health?.model_runtime.status === 'ready' ? '' : 'degraded'}`} aria-hidden="true" />
        <div><strong>{health?.model_runtime.status === 'ready' ? '本地模型' : '安全降级'}</strong><span>{health?.tool_runtime.status === 'ready' ? '工具通道正常' : '工具通道待连接'}</span></div>
      </div>
    </aside>
  )
}
