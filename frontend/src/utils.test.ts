import { describe, expect, it } from 'vitest'
import { formatDuration, initials, riskLabel, statusLabel } from './utils'

describe('display utilities', () => {
  it('formats short and long durations', () => {
    expect(formatDuration(420)).toBe('420ms')
    expect(formatDuration(4200)).toBe('4.2s')
    expect(formatDuration(12_600)).toBe('13s')
  })

  it('maps domain states to beginner-friendly Chinese labels', () => {
    expect(statusLabel.awaiting_approval).toBe('等待人工审批')
    expect(riskLabel.high).toBe('高风险')
  })

  it('builds compact actor initials', () => {
    expect(initials('on-call.lead')).toBe('OC')
  })
})

