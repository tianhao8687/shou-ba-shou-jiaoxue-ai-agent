import {
  ClipboardList,
  Eye,
  FileSearch,
  FileText,
  GitBranch,
  ScanSearch,
  SearchCheck,
  ShieldCheck,
  ShieldEllipsis,
  UserRoundCheck,
  Wrench,
} from 'lucide-react'
import type { RunRecord, TraceStatus } from '../types'
import { formatDuration } from '../utils'

const nodes = [
  { id: 'intake', label: 'Intake', cn: '接收', icon: ClipboardList },
  { id: 'retrieve', label: 'Retrieve', cn: '检索', icon: FileSearch },
  { id: 'investigate', label: 'Investigate', cn: '调查计划', icon: ScanSearch },
  { id: 'observe', label: 'Observe', cn: '只读观测', icon: Eye },
  { id: 'diagnose', label: 'Diagnose', cn: '动态诊断', icon: SearchCheck },
  { id: 'policy', label: 'Compile', cn: '策略编译', icon: GitBranch },
  { id: 'gate', label: 'Gate', cn: '风险判定', icon: ShieldEllipsis },
  { id: 'approval', label: 'Approval', cn: '角色审批', icon: UserRoundCheck },
  { id: 'execute', label: 'Act', cn: '受控执行', icon: Wrench },
  { id: 'verify', label: 'Verify', cn: '重新观测', icon: ShieldCheck },
  { id: 'finalize', label: 'Finalize', cn: '证据归档', icon: FileText },
]

function nodeStatus(run: RunRecord, nodeId: string): TraceStatus | 'pending' {
  const traceStatus = [...run.traces].reverse().find((trace) => trace.node === nodeId)?.status
  if (traceStatus) return traceStatus
  if (nodeId === 'approval') {
    if (run.approval.decision === 'pending') return 'waiting'
    if (run.approval.decision === 'approved') return 'completed'
    if (run.approval.decision === 'denied') return 'blocked'
    if (!run.approval.required && ['execute', 'verify', 'finalize', 'completed'].includes(run.current_node)) return 'completed'
  }
  return 'pending'
}

function nodeDetail(run: RunRecord, nodeId: string, status: TraceStatus | 'pending') {
  const trace = [...run.traces].reverse().find((item) => item.node === nodeId)
  if (trace) {
    if (trace.duration_ms) return formatDuration(trace.duration_ms)
    return trace.status === 'waiting' ? '等待人员' : '检查点'
  }
  if (nodeId === 'approval') {
    if (run.approval.decision === 'pending' || run.approval.decision === 'approved') {
      return `${run.approval.votes.length}/${run.approval.required_approvals} 票`
    }
    if (run.approval.decision === 'denied') return '已拒绝'
  }
  return status === 'completed' ? '已跳过' : '—'
}

export function RunPipeline({ run }: { run: RunRecord }) {
  return (
    <div className="pipeline-shell pipeline-v3" aria-label="Agent 十一阶段工作流">
      {nodes.map((node, index) => {
        const Icon = node.icon
        const status = nodeStatus(run, node.id)
        return (
          <div className="pipeline-segment" key={node.id}>
            {index > 0 && <span className={`pipeline-line ${status !== 'pending' ? 'reached' : ''}`} aria-hidden="true" />}
            <div className={`pipeline-node status-${status}`}>
              <span className="node-orbit"><Icon size={18} strokeWidth={1.8} aria-hidden="true" /></span>
              <strong>{node.label}</strong><span>{node.cn}</span>
              <small>{nodeDetail(run, node.id, status)}</small>
            </div>
          </div>
        )
      })}
    </div>
  )
}
