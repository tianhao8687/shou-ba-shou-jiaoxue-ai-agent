import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { EvaluationReport, ExternalValidationReport, UserIdentity } from '../types'
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

const externalReport: ExternalValidationReport = {
  schema: 'harbor-external-validation-evidence/v1',
  suite_version: 'external-v1',
  manifest_fingerprint: 'frozen-manifest',
  generated_at: '2026-08-10T02:00:00Z',
  verdict: 'pass',
  production_claim: false,
  sources_verified: 3,
  source_files_verified: 8,
  raw_data_committed: false,
  aggregate: {
    dataset_count: 3,
    total_external_records: 34984,
    holdout_records: 29125,
    passed_dataset_count: 3,
    gate_count: 9,
    passed_gate_count: 9,
    all_source_hashes_verified: true,
    unsafe_write_actions: 0,
    external_fault_type_count: 18,
    autonomous_taxonomy_coverage: 0,
  },
  datasets: [
    {
      id: 'loghub-bgl-holdout', source_id: 'loghub-bgl-2k', data_class: 'logs', evaluation_kind: '日志异常留出', record_count: 2000, holdout_count: 1300, passed: true, limitations: ['只使用 2k 样本'],
      metrics: { f1: 0.9022, balanced_accuracy: 0.9566, unseen_template_holdout: { f1: 0.75 } },
      gates: [{ id: 'log_holdout_f1', passed: true, observed: 0.9022, operator: '>=', threshold: 0.6 }],
    },
    {
      id: 'nab-real-timeseries-holdout', source_id: 'numenta-nab-real', data_class: 'timeseries', evaluation_kind: '时序异常留出', record_count: 32584, holdout_count: 27425, passed: true, limitations: ['不是完整 NAB 榜单分数'],
      metrics: { event_recall: 0.8333, alert_precision: 0.2185, false_alerts_per_1000_points: 4.3026 },
      gates: [{ id: 'timeseries_event_recall', passed: true, observed: 0.8333, operator: '>=', threshold: 0.5 }],
    },
    {
      id: 'aiops-2025-no-evidence-safety', source_id: 'aiops-challenge-2025', data_class: 'incidents', evaluation_kind: '无证据安全转人工', record_count: 400, holdout_count: 400, passed: true, limitations: ['未下载完整遥测'],
      metrics: { insufficient_evidence_abstention_rate: 1, unsafe_write_actions: 0, autonomous_taxonomy_coverage: 0 },
      gates: [{ id: 'unsafe_write_actions', passed: true, observed: 0, operator: '<=', threshold: 0 }],
    },
  ],
  provenance: [],
  boundaries: ['公开数据通过不等于企业生产验证。'],
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

  it('derives legacy run totals from case details when the stored summary is inconsistent', () => {
    const legacyReport = { ...report, case_count: 0, passed_count: 0 }

    render(<EvaluationsView report={legacyReport} user={user} busy={false} onRun={vi.fn()} />)

    expect(screen.getByRole('tab', { name: /内部密封回归 2\/2/ })).toBeInTheDocument()
    expect(screen.getByText(/2\/2 通过/)).toBeInTheDocument()
  })

  it('separates external evidence from the sealed fixture score and shows capability gaps', () => {
    render(<EvaluationsView report={report} externalReport={externalReport} user={user} busy={false} onRun={vi.fn()} />)

    fireEvent.click(screen.getByRole('tab', { name: /外部数据验证/ }))

    expect(screen.getByRole('heading', { name: '公开外部数据基线通过' })).toBeInTheDocument()
    expect(screen.getByText('21.9%')).toBeInTheDocument()
    expect(screen.getByText('0%', { selector: 'strong' })).toBeInTheDocument()
    expect(screen.getByText(/不是企业私有生产数据认证/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '运行全部 105 项' })).not.toBeInTheDocument()
  })
})
