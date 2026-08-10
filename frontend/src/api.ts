import type {
  AuthResponse,
  DashboardMetrics,
  DrillDescriptor,
  DrillTemplate,
  EvaluationReport,
  ExternalValidationReport,
  EvidenceBundle,
  HealthResponse,
  Incident,
  JobRecord,
  KnowledgeDoc,
  RunRecord,
  ToolSpec,
  UserIdentity,
} from './types'

const API_BASE = import.meta.env.VITE_API_URL ?? ''
const TOKEN_KEY = 'harbor.access_token'

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
    this.name = 'ApiError'
  }
}

export const authSession = {
  token: () => window.sessionStorage.getItem(TOKEN_KEY),
  save: (token: string) => window.sessionStorage.setItem(TOKEN_KEY, token),
  clear: () => window.sessionStorage.removeItem(TOKEN_KEY),
}

async function request<T>(path: string, init?: RequestInit, authenticated = true): Promise<T> {
  const token = authSession.token()
  const headers = new Headers(init?.headers)
  if (init?.body) headers.set('Content-Type', 'application/json')
  if (authenticated && token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }))
    const detail = typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail)
    throw new ApiError(detail || `请求失败：${response.status}`, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  login: (username: string, password: string) =>
    request<AuthResponse>('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }, false),
  me: () => request<UserIdentity>('/api/auth/me'),
  health: () => request<HealthResponse>('/api/status', undefined, false),
  drills: () => request<DrillTemplate[]>('/api/drills'),
  createDrill: (faultKind: string) =>
    request<DrillDescriptor>('/api/drills', { method: 'POST', body: JSON.stringify({ fault_kind: faultKind }) }),
  runs: () => request<RunRecord[]>('/api/runs'),
  run: (runId: string) => request<RunRecord>(`/api/runs/${runId}`),
  jobs: (runId: string) => request<JobRecord[]>(`/api/runs/${runId}/jobs`),
  evidence: (runId: string) => request<EvidenceBundle>(`/api/runs/${runId}/evidence`),
  metrics: () => request<DashboardMetrics>('/api/metrics'),
  knowledge: () => request<KnowledgeDoc[]>('/api/knowledge'),
  tools: () => request<ToolSpec[]>('/api/tools'),
  latestEvaluation: () => request<EvaluationReport | null>('/api/evaluations/latest'),
  latestExternalValidation: () => request<ExternalValidationReport | null>('/api/evaluations/external/latest'),
  startRun: (incident: Incident) =>
    request<RunRecord>('/api/runs', { method: 'POST', body: JSON.stringify({ incident }) }),
  decide: (runId: string, decision: 'approve' | 'deny', note: string, expectedVersion: number) =>
    request<RunRecord>(`/api/runs/${runId}/decision`, {
      method: 'POST',
      body: JSON.stringify({ decision, note, expected_version: expectedVersion }),
    }),
  retry: (runId: string, expectedVersion: number) =>
    request<RunRecord>(`/api/runs/${runId}/retry?expected_version=${expectedVersion}`, { method: 'POST' }),
  cancel: (runId: string, expectedVersion: number) =>
    request<RunRecord>(`/api/runs/${runId}/cancel?expected_version=${expectedVersion}`, { method: 'POST' }),
  runEvaluation: (liveModel: boolean, caseLimit?: number) => {
    const query = new URLSearchParams({ live_model: String(liveModel) })
    if (caseLimit) query.set('case_limit', String(caseLimit))
    return request<EvaluationReport>(`/api/evaluations/run?${query}`, { method: 'POST' })
  },
}
