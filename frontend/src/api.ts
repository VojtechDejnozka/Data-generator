// Thin API client for the FastAPI backend (proxied under /api by Vite, or VITE_API_BASE_URL).
// Every request carries the Supabase access token; job progress streams over fetch (not EventSource),
// so the token travels in the Authorization header and never in a URL.
/* eslint-disable @typescript-eslint/no-explicit-any */
import { accessToken } from './auth/supabase'

const BASE = ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  status: number
  detail: any
  constructor(status: number, message: string, detail?: any) {
    super(message)
    this.name = ''  // String(err) -> just the (Czech) message
    this.status = status
    this.detail = detail
  }
}

let unauthorized: (() => void) | null = null
/** Called on 401 from the API (expired / revoked session). */
export function setUnauthorizedHandler(fn: (() => void) | null) { unauthorized = fn }

let shareToken: string | null = null
/** Read-only share mode: run-scoped GETs (+ SQL) are served by /api/share/{token}. */
export function setShareToken(t: string | null) { shareToken = t }

const SHARE_PATH = /^\/api\/runs\/[^/?]+(\/(report|dashboard|schema|ground-truth|causal|query|tables\/[^?]+))?(\?.*)?$/

function rewrite(url: string): string {
  if (!shareToken || url === '/api/health') return url
  const m = url.match(SHARE_PATH)
  if (!m) throw new ApiError(403, 'Ve sdíleném náhledu není tato akce dostupná.')
  return `/api/share/${shareToken}${m[1] ?? ''}${m[3] ?? ''}`
}

async function authHeaders(): Promise<Record<string, string>> {
  if (shareToken) return {}
  const t = await accessToken()
  return t ? { Authorization: `Bearer ${t}` } : {}
}

function errorMessage(status: number, j: any, fallback: string): string {
  const d = j?.detail ?? j
  if (typeof d === 'string') return d
  if (d?.message) return d.message
  if (Array.isArray(d)) return 'Neplatný vstup: ' + d.map((x: any) => `${(x.loc ?? []).slice(1).join('.')} ${x.msg}`).join('; ')
  if (d?.errors) return d.errors.join('; ')
  if (status >= 500) return 'Server je dočasně nedostupný. Zkuste to prosím znovu.'
  return fallback
}

async function parseError(res: Response): Promise<ApiError> {
  let j: any = null
  try { j = await res.json() } catch { /* ignore */ }
  if (res.status === 401 && !shareToken) unauthorized?.()
  if (res.status === 429) window.dispatchEvent(new Event('s2d:usage'))
  return new ApiError(res.status, errorMessage(res.status, j, `${res.status} ${res.statusText}`), j?.detail)
}

export type Json = any

export interface BackendHealth {
  ok: boolean
  mock?: boolean
  provider?: string
  strong_model?: string
  cheap_model?: string
  strong_fallbacks?: string[]
  cheap_fallbacks?: string[]
  signup_mode?: string
}

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
  /** Executable structural causal model (backend app.causal); when present it defines the data. */
  scm?: Json | null
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
  const target = rewrite(url)
  let res: Response
  try {
    res = await fetch(BASE + target, {
      method,
      headers: { ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...(await authHeaders()) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new ApiError(0, 'Nepodařilo se spojit se serverem.')
  }
  if (!res.ok) throw await parseError(res)
  return res.json() as Promise<T>
}

export interface Me {
  id: string
  email: string
  role: 'user' | 'admin'
  display_name: string
  signup_mode: string
  auth_disabled: boolean
  quota: {
    unlimited: boolean
    runs_used: number; runs_limit: number; runs_left: number | null
    tokens_used: number; tokens_limit: number; tokens_left: number | null
    resets_at: string
  }
}

export type DownloadWhat = 'db' | 'zip' | 'obs_db' | 'doc_file' | 'docs_zip'

/** Ask the backend for a short-lived URL (signed Storage URL or one-time ticket). */
export async function downloadUrl(runId: string, what: DownloadWhat, extra: { obs_id?: string; file?: string; inline?: boolean } = {}) {
  const r = await req<{ url: string; filename: string }>('POST', `/api/runs/${runId}/download`, { what, ...extra })
  return { url: r.url.startsWith('/') ? BASE + r.url : r.url, filename: r.filename }
}

/** Trigger a browser download without putting any credential into the page. */
export async function download(runId: string, what: DownloadWhat | 'scm', extra: { obs_id?: string; file?: string } = {}) {
  const { url, filename } = what === 'scm' ? {
    url: URL.createObjectURL(new Blob([JSON.stringify(await req<Json>('GET', `/api/runs/${runId}/scm.json`), null, 2)],
      { type: 'application/json' })),
    filename: `scm-${runId}.json`,
  } : await downloadUrl(runId, what, extra)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  if (what === 'scm') window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export const api = {
  health: async (signal?: AbortSignal): Promise<BackendHealth> => {
    const response = await fetch(`${BASE}/api/health`, { cache: 'no-store', signal })
    if (!response.ok) throw new ApiError(response.status, 'Kontrola dostupnosti backendu selhala.')
    const health = await response.json()
    if (typeof health?.ok !== 'boolean') throw new Error('Neplatná odpověď kontroly backendu.')
    return health
  },
  me: () => req<Me>('GET', '/api/me'),
  updateMe: (display_name: string) => req<Json>('PATCH', '/api/me', { display_name }),
  share: (id: string, enabled: boolean) => req<{ share_token: string | null }>('POST', `/api/runs/${id}/share`, { enabled }),
  deleteRun: (id: string) => req<Json>('DELETE', `/api/runs/${id}`),
  cancelJob: (id: string) => req<Json>('POST', `/api/jobs/${id}/cancel`),
  adminUsers: () => req<Json>('GET', '/api/admin/users'),
  adminUpdateUser: (id: string, patch: Json) => req<Json>('PATCH', `/api/admin/users/${id}`, patch),
  adminInvite: (email: string) => req<Json>('POST', '/api/admin/invite', { email }),
  adminPreset: (id: string, is_preset: boolean) => req<Json>('POST', `/api/admin/runs/${id}/preset`, { is_preset }),
  presets: () => req<Json[]>('GET', '/api/presets'),
  plan: (input: Json) => req<{ job_id: string }>('POST', '/api/plan', input),
  scmImport: (model: Json, observation: Json) =>
    req<{ session_id: string }>('POST', '/api/scm/import', { model, observation }),
  scmValidate: (scm: Json) => req<Json>('POST', '/api/scm/validate', scm),
  scmMechanisms: () => req<Json[]>('GET', '/api/scm/mechanisms'),
  scmEdit: (scm: Json, operations: Json[]) => req<Json>('POST', '/api/scm/edit', { scm, operations }),
  session: (sid: string) => req<Json>('GET', `/api/plan/${sid}`),
  replan: (sid: string, body: Json) => req<{ job_id: string }>('POST', `/api/plan/${sid}/replan`, body),
  validateSpec: (spec: Spec) => req<{ errors: string[] }>('POST', '/api/spec/validate', spec),
  startRun: (body: Json) => req<{ job_id: string }>('POST', '/api/runs', body),
  reviewRun: (id: string) => req<{ session_id: string }>('POST', `/api/runs/${id}/review`),
  runs: () => req<Json[]>('GET', '/api/runs'),
  run: (id: string) => req<Json>('GET', `/api/runs/${id}`),
  llmCalls: (id: string) => req<Json[]>('GET', `/api/runs/${id}/llm-calls`),
  report: (id: string) => req<Json>('GET', `/api/runs/${id}/report`),
  dashboard: (id: string) => req<Json>('GET', `/api/runs/${id}/dashboard`),
  startScmDashboard: (id: string) => req<{ job_id: string }>('POST', `/api/runs/${id}/dashboard`),
  groundTruth: (id: string) => req<Json>('GET', `/api/runs/${id}/ground-truth`),
  schema: (id: string) => req<Json>('GET', `/api/runs/${id}/schema`),
  table: (id: string, t: string, params: Record<string, string | number | boolean | undefined>) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== '' && q.set(k, String(v)))
    return req<Json>('GET', `/api/runs/${id}/tables/${t}?${q}`)
  },
  query: (id: string, sql: string, limit = 500) => req<QueryResult>('POST', `/api/runs/${id}/query`, { sql, limit }),
  compare: (a: string, b: string) => req<Json>('GET', `/api/compare?a=${a}&b=${b}`),
  commentary: (id: string) => req<{ headline: string; paragraphs: string[] }>('POST', `/api/runs/${id}/commentary`),
  startDetective: (id: string) => req<{ job_id: string }>('POST', `/api/runs/${id}/detective`),
  detective: (id: string) => req<Json>('GET', `/api/runs/${id}/detective`),
  // documents pipeline
  docSources: (id: string) => req<Json[]>('GET', `/api/runs/${id}/doc-sources`),
  docsPlan: (input: Json) => req<{ job_id: string }>('POST', '/api/docs/plan', input),
  docsSession: (sid: string) => req<Json>('GET', `/api/docs/plan/${sid}`),
  docsReplan: (sid: string, body: Json) => req<{ job_id: string }>('POST', `/api/docs/plan/${sid}/replan`, body),
  docsStartRun: (body: Json) => req<{ job_id: string }>('POST', '/api/docs/runs', body),
  docsRun: (id: string) => req<Json>('GET', `/api/docs/runs/${id}`),
  docsDocument: (id: string, docId: number) => req<Json>('GET', `/api/docs/runs/${id}/doc/${docId}`),
}

/**
 * Drive a job and subscribe to its progress. Each POST /advance runs one stage of the job on the server
 * (or follows it) and streams SSE frames over fetch; we call it again until the job is final.
 * Returns an unsubscribe function (the job keeps its state on the server and can be resumed later).
 */
export function watchJob(jobId: string, onEvent: (e: JobEvent) => void): () => void {
  let stopped = false
  const ctrl = new AbortController()
  let since = 0
  const finish = (e: JobEvent) => { if (!stopped) { onEvent(e); window.dispatchEvent(new Event('s2d:usage')) } }

  const run = async () => {
    let failures = 0
    while (!stopped) {
      try {
        const res = await fetch(`${BASE}/api/jobs/${jobId}/advance?since=${since}`, {
          method: 'POST', headers: await authHeaders(), signal: ctrl.signal,
        })
        if (!res.ok || !res.body) throw await parseError(res)
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buf = ''
        let tail: JobEvent | null = null
        for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          let cut
          while ((cut = buf.indexOf('\n\n')) >= 0) {
            const frame = buf.slice(0, cut)
            buf = buf.slice(cut + 2)
            const data = frame.split('\n').filter((l) => l.startsWith('data: ')).map((l) => l.slice(6)).join('\n')
            if (!data) continue  // heartbeat comment
            const ev = JSON.parse(data) as JobEvent
            if ('final' in ev) { tail = ev; continue }
            if (typeof ev.i === 'number') since = Math.max(since, ev.i + 1)
            if (!stopped) onEvent(ev)
          }
        }
        if (tail?.final) { finish({ final: true, status: tail.status, result: tail.result, error: tail.error }); return }
        failures = 0
      } catch (e) {
        if (stopped || (e as Error)?.name === 'AbortError') return
        const status = (e as ApiError)?.status ?? 0
        if (status === 401 || status === 403 || status === 404 || ++failures > 6) {
          finish({ final: true, status: 'error', error: String(e) })
          return
        }
        await new Promise((r) => setTimeout(r, 1000 * failures))
      }
    }
  }
  void run()
  return () => { stopped = true; ctrl.abort() }
}

export const fmt = (v: Json): string => {
  if (v === null || v === undefined) return '–'
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString('cs-CZ') : v.toLocaleString('cs-CZ', { maximumFractionDigits: 2 })
  if (typeof v === 'boolean') return v ? 'ano' : 'ne'
  return String(v)
}
