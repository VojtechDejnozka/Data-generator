// Thin API client for the FastAPI backend (proxied under /api by Vite).
/* eslint-disable @typescript-eslint/no-explicit-any */

export type Json = any

export interface Column {
  name: string
  type: 'int' | 'float' | 'text' | 'date' | 'datetime' | 'bool'
  description: string
  pk: boolean
  fk: string | null
  nullable: boolean
  enum_values: string[]
  min: number | null
  max: number | null
}

export interface Table {
  name: string
  description: string
  strategy: 'simulation' | 'llm_rows' | 'llm_dimension'
  row_count: number
  columns: Column[]
  flags: string[]
}

export interface Knob {
  name: string
  label: string
  type: 'int' | 'float' | 'enum' | 'date' | 'bool' | 'entity'
  default: string
  min: number | null
  max: number | null
  options: string[]
  description: string
  applies_to: string[]
  value?: Json
}

export interface CausalStep {
  id: string
  mechanism: string
  affected_entities: string[]
  tables: string[]
  start: string | null
  ramp_profile: string
  expected_impact: string
  lag_days: number
}

export interface Distractor {
  id: string
  name: string
  kind: string
  description: string
  tables: string[]
  start: string | null
  end: string | null
  entities: string[]
}

export interface Evidence {
  id: string
  title: string
  step_id: string | null
  sql: string
  expected: string
  chart: string
  x: string | null
  y: string[]
  series: string | null
  caption: string
}

export interface Spec {
  meta: {
    name: string
    domain: string
    data_language: string
    period_start: string | null
    period_end: string | null
    seed: number
    archetype: string | null
    company: string
    environment: string
  }
  tables: Table[]
  story: { logline: string; causal_chain: CausalStep[]; ground_truth_facts: string[]; distractors: Distractor[] }
  knobs: Knob[]
  invariants: Json[]
  evidence: Evidence[]
  dashboard: { title: string; widgets: Json[] }
}

export interface Suggestion {
  id: string
  kind: string
  title: string
  description: string
  table?: Table | null
  column?: Json
  distractor?: Distractor | null
  causal_step?: CausalStep | null
  knob?: Knob | null
}

export interface PlannerResponse {
  spec: Spec
  questions: { id: string; question: string; why: string; default_answer: string }[]
  suggestions: Suggestion[]
  notes: string
}

export interface QueryResult {
  columns: string[]
  rows: Json[][]
  truncated?: boolean
  elapsed_ms?: number
  sql?: string
}

export interface JobEvent {
  i?: number
  t?: number
  phase?: string
  message?: string
  progress?: number
  status?: string
  final?: boolean
  result?: Json
  error?: string | null
  run_id?: string
  sql?: string
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const j = await res.json()
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j)
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => req<Json>('GET', '/api/health'),
  presets: () => req<Json[]>('GET', '/api/presets'),
  plan: (input: Json) => req<{ job_id: string }>('POST', '/api/plan', input),
  session: (sid: string) => req<Json>('GET', `/api/plan/${sid}`),
  replan: (sid: string, body: Json) => req<{ job_id: string }>('POST', `/api/plan/${sid}/replan`, body),
  validateSpec: (spec: Spec) => req<{ errors: string[] }>('POST', '/api/spec/validate', spec),
  startRun: (body: Json) => req<{ job_id: string }>('POST', '/api/runs', body),
  runs: () => req<Json[]>('GET', '/api/runs'),
  run: (id: string) => req<Json>('GET', `/api/runs/${id}`),
  llmCalls: (id: string) => req<Json[]>('GET', `/api/runs/${id}/llm-calls`),
  report: (id: string) => req<Json>('GET', `/api/runs/${id}/report`),
  dashboard: (id: string) => req<Json>('GET', `/api/runs/${id}/dashboard`),
  groundTruth: (id: string) => req<Json>('GET', `/api/runs/${id}/ground-truth`),
  schema: (id: string) => req<Json>('GET', `/api/runs/${id}/schema`),
  table: (id: string, t: string, params: Record<string, string | number | boolean | undefined>) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== '' && q.set(k, String(v)))
    return req<Json>('GET', `/api/runs/${id}/tables/${t}?${q}`)
  },
  query: (id: string, sql: string, limit = 500) => req<QueryResult>('POST', `/api/runs/${id}/query`, { sql, limit }),
  whatif: (id: string, knob_overrides: Record<string, string>, label = '') =>
    req<{ job_id: string }>('POST', `/api/runs/${id}/whatif`, { knob_overrides, label }),
  compare: (a: string, b: string) => req<Json>('GET', `/api/compare?a=${a}&b=${b}`),
  commentary: (id: string) => req<{ headline: string; paragraphs: string[] }>('POST', `/api/runs/${id}/commentary`),
  startDetective: (id: string) => req<{ job_id: string }>('POST', `/api/runs/${id}/detective`),
  detective: (id: string) => req<Json>('GET', `/api/runs/${id}/detective`),
  // documents pipeline
  docsPlan: (input: Json) => req<{ job_id: string }>('POST', '/api/docs/plan', input),
  docsSession: (sid: string) => req<Json>('GET', `/api/docs/plan/${sid}`),
  docsReplan: (sid: string, body: Json) => req<{ job_id: string }>('POST', `/api/docs/plan/${sid}/replan`, body),
  docsStartRun: (body: Json) => req<{ job_id: string }>('POST', '/api/docs/runs', body),
  docsRun: (id: string) => req<Json>('GET', `/api/docs/runs/${id}`),
  docsDocument: (id: string, docId: number) => req<Json>('GET', `/api/docs/runs/${id}/doc/${docId}`),
}

/** Subscribe to job progress over SSE. Returns an unsubscribe function. */
export function watchJob(jobId: string, onEvent: (e: JobEvent) => void): () => void {
  const es = new EventSource(`/api/jobs/${jobId}/events`)
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data) as JobEvent
    onEvent(ev)
    if (ev.final) es.close()
  }
  es.onerror = () => {
    // fall back to polling once if the stream breaks
    es.close()
    fetch(`/api/jobs/${jobId}`)
      .then((r) => r.json())
      .then((s) => {
        if (s.status === 'done' || s.status === 'error') onEvent({ final: true, status: s.status, result: s.result, error: s.error })
        else setTimeout(() => watchJob(jobId, onEvent), 1000)
      })
      .catch(() => undefined)
  }
  return () => es.close()
}

export const fmt = (v: Json): string => {
  if (v === null || v === undefined) return '–'
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString('cs-CZ') : v.toLocaleString('cs-CZ', { maximumFractionDigits: 2 })
  if (typeof v === 'boolean') return v ? 'ano' : 'ne'
  return String(v)
}
