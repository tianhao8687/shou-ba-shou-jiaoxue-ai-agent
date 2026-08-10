import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { EvaluationReport, UserIdentity } from '../types'
import { EvaluationsView } from './EvaluationsView'

const user: UserIdentity = {
  username: 'admin@harbor.local',
  display_name: '管理员',
  roles: ['observer', 'admin'],
  tenant_id: 'xm-ops',
}

const report: EvaluationReport = {
  id: 'EVAL-V4',
  tenant_id: 'xm-ops',
  created_at: '2026-08-10T00:00:00Z',
  score: 100,
  task_success_rate: 100,
  root_cause_accuracy: 100,
  tool_accuracy: 100,
  retrieval_recall: 100,
  safety_gate_accuracy: 100,
  injection_resistance: 100,
  unsafe_action_rate: 0,
  capability_enforcement: 100,
  p95_case_latency_ms: 40,
  suite_mode: 'sealed-fixture',
  suite_version: 'v4',
  suite_fingerprint: 'abc123',
  case_count: 2,
  passed_count: 2,
  confidence_level: 0.95,
  task_success_ci_lower: 65.76,
  task_success_ci_upper: 100,
  category_breakdown: {
    baseline: { case_count: 1, passed_count: 1, pass_rate: 100, unsafe_action_rate: 0, ci_lower: 20.65, ci_upper: 100 },
    tool_output: { case_count: 1, passed_count: 1, pass_rate: 100, unsafe_action_rate: 0, ci_lower: 20.65, ci_upper: 100 },
  },
  cases: [
    {
      case_id: 'sealed-cache-01::baseline',
      variant_id: 'baseline',
      variant_category: 'baseline',
      attack_surface: 'incident_text',
      passed: true,
      fault_kind: 'stale_cache',
      expected_tool: 'refresh_cache',
      selected_tools: ['refresh_cache'],
      root_cause_match: true,
      outcome_match: true,
      retrieval_hit: true,
      gate_correct: true,
      injection_resistant: true,
      unsafe_action: false,
      capability_enforced: true,
      latency_ms: 30,
      notes: [],
    },
    {
      case_id: 'sealed-cache-01::tool-output-injection',
      variant_id: 'tool-output-injection',
      variant_category: 'tool_output',
      attack_surface: 'tool_output',
      passed: true,
      fault_kind: 'stale_cache',
      expected_tool: 'refresh_cache',
      selected_tools: ['refresh_cache'],
      root_cause_match: true,
      outcome_match: true,
      retrieval_hit: true,
      gate_correct: true,
      injection_resistant: true,
      unsafe_action: false,
      capability_enforced: true,
      latency_ms: 40,
      notes: [],
    },
  ],
}

describe('EvaluationsView', () => {
  it('shows sample uncertainty and filters the matrix by attack category', () => {
    render(<EvaluationsView report={report} user={user} busy={false} onRun={vi.fn()} />)

    expect(screen.getByText(/95% Wilson 区间 65.76%–100%/)).toBeInTheDocument()
    expect(screen.getByText('sealed-cache-01::baseline')).toBeInTheDocument()
    expect(screen.getByText('sealed-cache-01::tool-output-injection')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('攻击类别'), { target: { value: 'tool_output' } })

    expect(screen.queryByText('sealed-cache-01::baseline')).not.toBeInTheDocument()
    expect(screen.getByText('sealed-cache-01::tool-output-injection')).toBeInTheDocument()
  })

  it('starts the full fixture suite without presenting it as a live-model run', () => {
    const onRun = vi.fn().mockResolvedValue(undefined)
    render(<EvaluationsView report={report} user={user} busy={false} onRun={onRun} />)

    fireEvent.click(screen.getByRole('button', { name: '运行全部 105 项' }))

    expect(onRun).toHaveBeenCalledWith(false)
  })
})
