export type RunStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'completed'
  | 'failed'
  | 'handed_off'
  | 'cancelled'
export type JobStatus = 'queued' | 'claimed' | 'succeeded' | 'failed' | 'cancelled'
export type TraceStatus = 'running' | 'completed' | 'waiting' | 'blocked' | 'failed'
export type RiskLevel = 'low' | 'medium' | 'high'
export type Severity = 'P1' | 'P2' | 'P3'

export interface UserIdentity {
  username: string
  display_name: string
  roles: string[]
  tenant_id: string
}

export interface AuthResponse {
  access_token: string
  token_type: 'bearer'
  expires_at: string
  user: UserIdentity
}

export interface Incident {
  title: string
  summary: string
  severity: Severity
  service: string
  environment: 'production' | 'staging' | 'lab'
  symptoms: string[]
  experiment_id?: string | null
  tags: string[]
}

export interface Check {
  field: string
  operator: 'eq' | 'ne' | 'lt' | 'lte' | 'gt' | 'gte' | 'contains' | 'in'
  value: unknown
  description: string
}

export interface RollbackPlan {
  mode: 'tool' | 'manual'
  tool_name?: string | null
  tool_input: Record<string, unknown>
  rationale: string
}

export interface PlanStep {
  id: string
  title: string
  objective: string
  tool_name: string
  tool_input: Record<string, unknown>
  evidence_ids: string[]
  depends_on: string[]
  preconditions: Check[]
  success_criteria: Check[]
  rollback: RollbackPlan
  risk: RiskLevel
  rationale: string
}

export interface PolicyIssue {
  code: string
  step_id?: string | null
  message: string
  blocking: boolean
}

export interface PolicyDecision {
  accepted: boolean
  plan_hash?: string | null
  effective_risk: RiskLevel
  required_roles: string[]
  issues: PolicyIssue[]
  compiled_at: string
}

export interface SourceHit {
  chunk_id: string
  doc_id: string
  title: string
  section: string
  excerpt: string
  score: number
  uri: string
  retrieval_channel: 'lexical' | 'semantic' | 'hybrid' | 'routed' | 'pinned'
}

export interface Observation {
  id: string
  step_id: string
  tool_name: string
  summary: string
  data: Record<string, unknown>
  captured_at: string
  transport: string
  source_uri?: string | null
}

export interface ToolResult {
  step_id: string
  tool_name: string
  status: 'succeeded' | 'failed' | 'skipped' | 'unknown'
  summary: string
  output: Record<string, unknown>
  idempotency_key: string
  duration_ms: number
  error?: string | null
  attempt: number
  transport: string
  capability_jti?: string | null
}

export interface VerificationResult {
  step_id: string
  passed: boolean
  checks: Array<Record<string, unknown>>
  summary: string
}

export interface ModelProposal {
  phase: 'investigation' | 'remediation'
  diagnosis: string
  confidence: number
  evidence_ids: string[]
  plan: PlanStep[]
  safety_notes: string[]
  needs_handoff: boolean
  handoff_reason?: string | null
}

export interface ModelInvocation {
  id: string
  provider: string
  model: string
  phase: 'investigation' | 'remediation'
  status: 'succeeded' | 'fallback' | 'failed' | 'disabled'
  schema_name: string
  prompt_version: string
  latency_ms: number
  queue_wait_ms: number
  input_characters: number
  output_characters: number
  error?: string | null
  fallback_reason?: string | null
}

export interface TraceStep {
  id: string
  node: string
  label: string
  status: TraceStatus
  started_at: string
  duration_ms: number
  summary: string
  input_preview: string
  output_preview: string
  metadata: Record<string, unknown>
}

export interface AuditEvent {
  id: string
  timestamp: string
  actor: string
  action: string
  detail: string
  metadata: Record<string, unknown>
}

export interface Approval {
  required: boolean
  decision: 'pending' | 'approved' | 'denied' | 'not_required'
  plan_hash?: string | null
  required_roles: string[]
  required_approvals: number
  separation_of_duties: boolean
  requester?: string | null
  votes: ApprovalVote[]
  decided_by?: string | null
  decided_roles: string[]
  decided_at?: string | null
  note?: string | null
}

export interface ApprovalVote {
  approver: string
  display_name: string
  roles: string[]
  tenant_id: string
  plan_hash: string
  decision: 'approve' | 'deny'
  note: string
  decided_at: string
}

export interface WorkerLeaseEvent {
  worker_id: string
  fencing_token: number
  action: 'claimed' | 'heartbeat' | 'released' | 'recovered'
  timestamp: string
}

export interface RunRecord {
  id: string
  incident: Incident
  tenant_id: string
  created_by: string
  status: RunStatus
  run_mode: 'live-model' | 'test-fixture' | 'model-degraded'
  current_node: string
  created_at: string
  updated_at: string
  version: number
  attempt: number
  max_attempts: number
  cancellation_requested: boolean
  diagnosis: string
  confidence: number
  model_proposal?: ModelProposal | null
  model_calls: ModelInvocation[]
  sources: SourceHit[]
  investigation_plan: PlanStep[]
  investigation_policy: PolicyDecision
  observations: Observation[]
  plan: PlanStep[]
  policy: PolicyDecision
  risk_level: RiskLevel
  approval: Approval
  tool_results: ToolResult[]
  verification: VerificationResult[]
  resolution: string
  traces: TraceStep[]
  audit: AuditEvent[]
  lease_history: WorkerLeaseEvent[]
  error_code?: string | null
  error_detail?: string | null
  estimated_cost: number
}

export interface JobRecord {
  id: string
  run_id: string
  stage: 'start' | 'resume' | 'retry'
  status: JobStatus
  owner?: string | null
  lease_until?: string | null
  fencing_token: number
  attempts: number
  available_at: string
  created_at: string
  updated_at: string
  last_error?: string | null
}

export interface DashboardMetrics {
  total_runs: number
  completed_runs: number
  queued_runs: number
  running_runs: number
  awaiting_approval: number
  recovered_runs: number
  success_rate: number
  tool_success_rate: number
  approval_rate: number
  citation_coverage: number
  p95_latency_ms: number
  model_live_rate: number
  model_fallback_rate: number
  average_model_latency_ms: number
  remote_tool_rate: number
  unsafe_action_rate: number
  evaluation_score: number
  trend: Array<{ date: string; success_rate: number; p95_ms: number }>
}

export interface EvaluationCaseResult {
  case_id: string
  passed: boolean
  fault_kind: string
  expected_tool?: string | null
  selected_tools: string[]
  root_cause_match: boolean
  outcome_match: boolean
  retrieval_hit: boolean
  gate_correct: boolean
  injection_resistant: boolean
  unsafe_action: boolean
  capability_enforced: boolean
  latency_ms: number
  notes: string[]
}

export interface EvaluationReport {
  id: string
  tenant_id: string
  created_at: string
  score: number
  task_success_rate: number
  root_cause_accuracy: number
  tool_accuracy: number
  retrieval_recall: number
  safety_gate_accuracy: number
  injection_resistance: number
  unsafe_action_rate: number
  capability_enforcement: number
  p95_case_latency_ms: number
  suite_mode: 'sealed-fixture' | 'sealed-live-model'
  cases: EvaluationCaseResult[]
}

export interface KnowledgeDoc {
  id: string
  title: string
  section: string
  uri: string
  characters: number
  preview: string
}

export interface ToolSpec {
  name: string
  description: string
  risk: RiskLevel
  required_role: string
  read_only: boolean
  applicability: string
  input_schema: {
    properties?: Record<string, { title?: string; type?: string; minimum?: number; maximum?: number }>
    required?: string[]
  }
}

export interface HealthResponse {
  status: string
  ready: boolean
  purpose: 'runtime-status' | 'readiness'
  app: string
  version: string
  mode: string
  database: string
  database_status: Record<string, unknown> & { status?: string; detail?: string }
  vector_backend: string
  vector_quality: 'semantic' | 'lexical-feature-baseline' | 'unavailable'
  knowledge_documents: number
  knowledge_chunks: number
  model_runtime: Record<string, unknown> & { status?: string; provider?: string; model?: string; loaded?: boolean; device?: string | null; detail?: string }
  tool_runtime: Record<string, unknown> & { status?: string; mode?: string; detail?: string }
  worker_runtime: Record<string, unknown> & { status?: string; worker_id?: string; running?: boolean; claims?: number; recoveries?: number }
  readiness_checks: Record<string, boolean>
}

export interface DrillTemplate {
  id: string
  service: string
  title: string
}

export interface DrillDescriptor {
  experiment_id: string
  incident: Incident
}

export interface EvidenceBundle {
  schema: string
  generated_at: string
  run: RunRecord
  jobs: JobRecord[]
  runtime_leak_check: { forbidden_fields: string[]; hits: string[]; passed: boolean }
  contract: { model_mode: string; vector_quality: string; tool_transport: string; unsafe_action_policy: string }
}

export type ViewName = 'overview' | 'run' | 'evaluations' | 'knowledge' | 'policy'
