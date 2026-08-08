import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { RunRecord } from '../types'
import { RunPipeline } from './RunPipeline'

function approvalRun(decision: RunRecord['approval']['decision'], votes: number, required = 2) {
  return {
    current_node: decision === 'pending' ? 'approval' : 'completed',
    traces: [],
    approval: {
      required: decision !== 'not_required',
      decision,
      required_roles: ['on-call-lead'],
      required_approvals: required,
      separation_of_duties: true,
      votes: Array.from({ length: votes }, (_, index) => ({
        approver: `approver-${index}@harbor.local`,
        display_name: `审批人 ${index + 1}`,
        roles: ['on-call-lead'],
        tenant_id: 'xm-ops',
        plan_hash: 'sha256:test',
        decision: 'approve' as const,
        note: 'approved',
        decided_at: '2026-08-08T00:00:00Z',
      })),
    },
  } as unknown as RunRecord
}

describe('RunPipeline approval state', () => {
  it('shows live quorum progress while approval is pending', () => {
    render(<RunPipeline run={approvalRun('pending', 1)} />)
    expect(screen.getByText('1/2 票')).toBeInTheDocument()
  })

  it('shows the completed quorum instead of claiming approval was skipped', () => {
    render(<RunPipeline run={approvalRun('approved', 2)} />)
    expect(screen.getByText('2/2 票')).toBeInTheDocument()
    expect(screen.queryByText('已跳过')).not.toBeInTheDocument()
  })

  it('keeps the skipped label for a run that needed no approval', () => {
    render(<RunPipeline run={approvalRun('not_required', 0, 0)} />)
    expect(screen.getByText('已跳过')).toBeInTheDocument()
  })
})
