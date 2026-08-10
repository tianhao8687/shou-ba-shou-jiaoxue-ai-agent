import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { EvaluationReport, ExternalValidationReport, TelemetryValidationReport, UserIdentity } from '../types'
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

const telemetryMetrics = {
  case_count: 24,
  fault_type_top1_accuracy: 0.4167,
  fault_type_top3_accuracy: 0.7917,
  entity_top1_accuracy: 0.4167,
  entity_top3_accuracy: 0.5833,
  exact_rca_top1_accuracy: 0.2917,
  evidence_modality_recall: 0.8611,
  multimodal_case_coverage: 0.75,
  abstention_rate: 0,
  median_case_latency_ms: 43,
  p95_case_latency_ms: 61,
}

const telemetryReport: TelemetryValidationReport = {
  schema: 'harbor-telemetry-validation-evidence/v2',
  suite_version: 'telemetry-v4',
  ruleset_version: 'deterministic-rca-v3',
  manifest_fingerprint: 'manifest-fingerprint',
  prediction_fingerprint: 'e1dcd9bd3764105ebd666aa6670e23ea45bc638e752cac155c66825bdaa02b53',
  prediction_semantic_fingerprint: '3'.repeat(64),
  generated_at: '2026-08-11T00:00:00Z',
  verdict: 'pass',
  production_claim: false,
  raw_data_committed: false,
  replay_audit: {
    schema: 'harbor-telemetry-replay-audit/v1', audited_at: '2026-08-11T01:31:59+08:00', oracle_status: 'already-opened-no-retuning', tie_break_contract: 'stable ordering', replay_count: 2,
    semantic_fingerprint: '0'.repeat(64), semantic_match: true, full_artifact_hashes: ['1'.repeat(64), '2'.repeat(64)], volatile_fields_excluded: ['generated_at', 'latency_ms'],
    holdout: { ...telemetryMetrics, fault_type_top1_accuracy: 0.3333, fault_type_top3_accuracy: 0.6667, exact_rca_top1_accuracy: 0.25, evidence_modality_recall: 0.6806, multimodal_case_coverage: 0.5 },
    p95_case_latency_ms_range: [93, 97], passed_gates: 10, gate_count: 10, verdict: 'pass', production_claim: false, notes: ['not a second blind run'],
  },
  source: {
    id: 'aiops-challenge-2025', title: 'AIOps Challenge 2025 multimodal telemetry', repository_url: 'https://www.aiops.cn/gitlab/example', revision: 'a'.repeat(40), independence: 'third-party',
    license: { name: 'CC BY-NC 4.0', url: 'https://creativecommons.org/licenses/by-nc/4.0/', raw_redistribution: 'not-committed' },
  },
  coverage: {
    by_archive: {}, total_rows: { logs: 25_000_000, metrics: 4_000_000, traces: 25_426_202 }, all_rows: 54_426_202, archive_bytes: 1_897_744_494, calibration_cases: 16, validation_cases: 48, holdout_cases: 24,
  },
  protocol: {
    prediction_frozen_at: '2026-08-11T00:00:00Z', oracle_opened_at: '2026-08-11T00:00:01Z', oracle_opened_after_freeze: true, predictor_oracle_access: 'none', prediction_hash_algorithm: 'sha256', unsafe_write_actions: 0, timezone_contract: 'UTC rows',
  },
  calibration: { ...telemetryMetrics, case_count: 16 },
  validation: { ...telemetryMetrics, case_count: 48 },
  holdout: telemetryMetrics,
  gates: Array.from({ length: 10 }, (_, index) => ({ id: index === 0 ? 'archive_hashes_verified' : `gate-${index}`, passed: true, observed: true, operator: '==', threshold: true })),
  cases: [{ uuid: 'holdout-case-01', role: 'holdout', predicted_fault_type: 'code error', predicted_entity: 'cartservice', fault_type_top1: true, fault_type_top3: true, entity_top1: true, entity_top3: true, network_pair_match: null, exact_rca_top1: true, evidence_modality_recall: 1, evidence_modalities: ['log', 'metric'], abstained: false }],
  boundaries: ['第三方混沌注入数据不等于企业生产认证。'],
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

  it('shows the frozen real-telemetry holdout separately from fixture scores', () => {
    render(<EvaluationsView report={report} telemetryReport={telemetryReport} user={user} busy={false} onRun={vi.fn()} />)

    fireEvent.click(screen.getByRole('tab', { name: /完整遥测 RCA 10\/10 门槛/ }))

    expect(screen.getByRole('heading', { name: '完整遥测门槛通过，复现缺陷已闭环' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '并列排序已固定，2 次重放语义一致' })).toBeInTheDocument()
    expect(screen.getAllByText('66.67%')).toHaveLength(2)
    expect(screen.getByText(/答案解封后只做通用排序修复/)).toBeInTheDocument()
    expect(screen.getByText('holdout-case-01')).toBeInTheDocument()
    expect(screen.getByText(/预测 SHA-256 冻结后才打开答案/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '运行全部 105 项' })).not.toBeInTheDocument()
  })
})
