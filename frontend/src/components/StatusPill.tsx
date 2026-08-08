import { Ban, CircleCheck, CircleDotDashed, Clock3, Hand, OctagonX, RotateCw } from 'lucide-react'
import type { RunStatus, TraceStatus } from '../types'
import { statusLabel, traceStatusLabel } from '../utils'

const runIcons = {
  queued: Clock3,
  running: RotateCw,
  awaiting_approval: Hand,
  completed: CircleCheck,
  failed: OctagonX,
  handed_off: CircleDotDashed,
  cancelled: Ban,
}

export function RunStatusPill({ status }: { status: RunStatus }) {
  const Icon = runIcons[status]
  return (
    <span className={`status-pill run-${status}`}>
      <Icon size={13} strokeWidth={2} aria-hidden="true" />{statusLabel[status]}
    </span>
  )
}

export function TraceStatusPill({ status }: { status: TraceStatus }) {
  return <span className={`trace-pill trace-${status}`}><i />{traceStatusLabel[status]}</span>
}
