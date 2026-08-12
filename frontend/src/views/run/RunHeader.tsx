import { Ban, Download, RefreshCw, RotateCcw, ShieldAlert } from 'lucide-react'
import { RunStatusPill } from '../../components/StatusPill'
import type { RunRecord } from '../../types'

interface RunHeaderProps {
  run: RunRecord
  busy: boolean
  canRetry: boolean
  canCancel: boolean
  onRetry: () => Promise<void>
  onCancel: () => Promise<void>
  onDownloadEvidence: () => Promise<void>
  onRefresh: () => Promise<void>
}

export function RunHeader({
  run,
  busy,
  canRetry,
  canCancel,
  onRetry,
  onCancel,
  onDownloadEvidence,
  onRefresh,
}: RunHeaderProps) {
  const { incident } = run

  return (
    <>
      <header className="run-heading run-heading-v3">
        <div>
          <div className="eyebrow-row">
            <span className={`severity severity-${incident.severity.toLowerCase()}`}>{incident.severity}</span>
            <span>{incident.service}</span><span className="dot-separator" /><span>{incident.environment}</span>
            {incident.experiment_id && <code className="experiment-chip">{incident.experiment_id}</code>}
          </div>
          <h1>{incident.title}</h1>
          <div className="run-subline">
            <span className="mono">{run.id}</span><span className="tenant-chip">TENANT {run.tenant_id}</span>
            <span className="version-chip">CAS v{run.version}</span><RunStatusPill status={run.status} />
            <span className={`mode-chip ${run.run_mode}`}>{run.run_mode}</span>
          </div>
        </div>
        <div className="run-header-actions">
          <button className="button ghost" type="button" onClick={onRefresh} disabled={busy}><RefreshCw size={15} />刷新</button>
          {canRetry && <button className="button secondary" type="button" onClick={onRetry} disabled={busy}><RotateCcw size={15} />失败重试</button>}
          {canCancel && <button className="button secondary" type="button" onClick={onCancel} disabled={busy}><Ban size={15} />安全取消</button>}
          <button className="button secondary" type="button" onClick={onDownloadEvidence}><Download size={15} />证据包</button>
        </div>
      </header>

      {(run.status === 'queued' || run.status === 'running') && (
        <div className="run-progress-banner" role="status">
          <RefreshCw className="spin" size={16} />
          <div><strong>{run.status === 'queued' ? '等待 durable worker 认领' : `正在执行 ${run.current_node} 检查点`}</strong><span>前端只轮询状态；执行不依赖浏览器连接，刷新或关闭页面不会中断任务。</span></div>
        </div>
      )}
      {(run.status === 'failed' || run.status === 'handed_off') && (
        <div className="run-failure-banner" role="alert">
          <ShieldAlert size={17} />
          <div><strong>{run.status === 'failed' ? `${run.error_code ?? 'RUN_FAILED'} · 自动执行失败` : '系统选择转人工'}</strong><span>{run.error_detail || run.resolution || '失败边界已记录，没有继续猜测。'}</span></div>
        </div>
      )}
    </>
  )
}
