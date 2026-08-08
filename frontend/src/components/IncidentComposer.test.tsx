import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Incident, UserIdentity } from '../types'
import { IncidentComposer } from './IncidentComposer'

const admin: UserIdentity = {
  username: 'admin@harbor.local',
  display_name: '平台管理员',
  roles: ['observer', 'operator', 'on-call-lead', 'security-on-call', 'admin'],
  tenant_id: 'xm-ops',
}

describe('IncidentComposer', () => {
  it('submits facts without expected answer fields', async () => {
    const submit = vi.fn().mockResolvedValue(undefined)
    render(<IncidentComposer user={admin} drills={[]} busy={false} onClose={vi.fn()} onSubmit={submit} onCreateDrill={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /提交自由事件/ }))
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    const incident = submit.mock.calls[0][0] as Incident & Record<string, unknown>

    expect(incident.symptoms.length).toBeGreaterThan(0)
    expect(incident.experiment_id).toBeNull()
    expect(incident).not.toHaveProperty('expected_cause')
    expect(incident).not.toHaveProperty('expected_resolution')
    expect(incident).not.toHaveProperty('plan')
  })

  it('creates a sealed experiment before starting a lab run', async () => {
    const labIncident: Incident = {
      title: '受控连接池故障实验事件',
      summary: '实验环境连接池已进入受控耗尽状态，需要调查并恢复。',
      severity: 'P2',
      service: 'checkout-api',
      environment: 'lab',
      symptoms: ['连接获取超时'],
      experiment_id: 'EXP-12345678',
      tags: ['sealed-lab'],
    }
    const createDrill = vi.fn().mockResolvedValue(labIncident)
    const submit = vi.fn().mockResolvedValue(undefined)
    render(<IncidentComposer user={admin} drills={[{ id: 'connection_pool_exhaustion', service: 'checkout-api', title: '连接池耗尽' }]} busy={false} onClose={vi.fn()} onSubmit={submit} onCreateDrill={createDrill} />)

    fireEvent.click(screen.getByRole('tab', { name: /受控故障实验/ }))
    fireEvent.click(screen.getByRole('button', { name: /注入故障并入队/ }))

    await waitFor(() => expect(createDrill).toHaveBeenCalledWith('connection_pool_exhaustion'))
    expect(submit).toHaveBeenCalledWith(labIncident)
    expect(screen.getByText(/Agent 只会收到事件描述/)).toBeInTheDocument()
  })

  it('does not expose lab controls to a non-admin observer', () => {
    render(<IncidentComposer user={{ username: 'viewer@harbor.local', display_name: '只读观察员', roles: ['observer'], tenant_id: 'xm-ops' }} drills={[]} busy={false} onClose={vi.fn()} onSubmit={vi.fn()} onCreateDrill={vi.fn()} />)
    expect(screen.queryByRole('tab', { name: /受控故障实验/ })).not.toBeInTheDocument()
  })
})
