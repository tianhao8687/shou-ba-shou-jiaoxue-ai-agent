import { describe, expect, it } from 'vitest'
import type { RunRecord, UserIdentity } from '../types'
import { approvalEligibility } from './RunView'

const run = {
  tenant_id: 'xm-ops',
  created_by: 'admin@harbor.local',
  approval: {
    required_roles: ['on-call-lead'],
    separation_of_duties: true,
    requester: 'admin@harbor.local',
    votes: [],
  },
} as unknown as RunRecord

function identity(username: string, roles: string[], tenantId = 'xm-ops'): UserIdentity {
  return { username, display_name: username, roles, tenant_id: tenantId }
}

describe('four-eyes approval eligibility', () => {
  it('rejects the requester even when the requester is an admin', () => {
    const result = approvalEligibility(run, identity('admin@harbor.local', ['admin']))
    expect(result.eligible).toBe(false)
    expect(result.reasons.join('')).toContain('申请人不能审批')
  })

  it('accepts an independent qualified approver, then rejects a duplicate subject', () => {
    const lead = identity('lead@harbor.local', ['observer', 'on-call-lead'])
    expect(approvalEligibility(run, lead).eligible).toBe(true)

    const afterFirstVote = {
      ...run,
      approval: {
        ...run.approval,
        votes: [{ approver: lead.username }],
      },
    } as RunRecord
    const repeated = approvalEligibility(afterFirstVote, lead)
    expect(repeated.eligible).toBe(false)
    expect(repeated.reasons.join('')).toContain('只能投一票')
  })

  it('rejects an identity from another tenant before it can act', () => {
    const result = approvalEligibility(
      run,
      identity('lead@other.local', ['on-call-lead'], 'other-tenant'),
    )
    expect(result.eligible).toBe(false)
    expect(result.reasons.join('')).toContain('不属于该运行的租户')
  })
})
