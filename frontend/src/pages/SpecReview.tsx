import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { api, type Column, type Json, type PlannerResponse, type Spec, type Suggestion, type Table } from '../api'
import JobProgress from '../components/JobProgress'
import ScmPanel from '../components/ScmPanel'
import RelationalSchema from '../components/RelationalSchema'
import StoryTimeline from '../components/StoryTimeline'

const STRATEGY: Record<string, [string, string]> = {
  simulation: ['simulace', 'bg-violet-50 text-violet-800 border-violet-200'],
  llm_dimension: ['LLM master data', 'bg-sky-50 text-sky-800 border-sky-200'],
  llm_rows: ['LLM řádky', 'bg-teal-50 text-teal-800 border-teal-200'],
}
export const StrategyChip = ({ s }: { s: string }) => (
  <span className={`chip border ${STRATEGY[s]?.[1] ?? ''}`}>{STRATEGY[s]?.[0] ?? s}</span>
)

const GROUPS: [Table['strategy'], string][] = [
  ['llm_dimension', 'Master data (generuje model)'],
  ['simulation', 'Simulované tabulky'],
  ['llm_rows', 'Řádky generované modelem'],
]

const SUGGESTION_KIND: Record<string, string> = {
  add_table: 'Nová tabulka', add_column: 'Nový sloupec', add_distractor: 'Nová falešná stopa',
  add_causal_step: 'Nový kauzální krok', add_knob: 'Nový parametr',
}
const suggestionTarget = (s: Suggestion) =>
  s.kind === 'add_column' ? `${s.column?.table}.${s.column?.column?.name}`
    : s.table?.name ?? s.causal_step?.id ?? s.knob?.name ?? s.distractor?.name ?? null

function applySuggestion(spec: Spec, s: Suggestion): Spec {
  const out: Spec = structuredClone(spec)
  if (s.kind === 'add_table' && s.table && !out.tables.find((t) => t.name === s.table!.name)) out.tables.push(s.table)
  if (s.kind === 'add_column' && s.column) {
    const t = out.tables.find((x) => x.name === s.column.table)
    if (t && t.strategy !== 'simulation' && !t.columns.find((c) => c.name === s.column.column.name)) t.columns.push(s.column.column)
  }
  if (s.kind === 'add_distractor' && s.distractor) out.story.distractors.push(s.distractor)
  if (s.kind === 'add_causal_step' && s.causal_step) out.story.causal_chain.push(s.causal_step)
  if (s.kind === 'add_knob' && s.knob) out.knobs.push(s.knob)
  return out
}

const newColumn = (): Column => ({ name: 'new_column', type: 'text', description: '', pk: false, fk: null, nullable: true, enum_values: [], min: null, max: null })

function TableRow({ t, steps, highlight, onChange, onRemove }: {
  t: Table; steps: string[]; highlight: boolean; onChange: (t: Table) => void; onRemove: () => void
}) {
  const [open, setOpen] = useState(false)
  const locked = t.strategy === 'simulation' || t.flags.includes('module')
  const setCol = (i: number, patch: Partial<Column>) => onChange({ ...t, columns: t.columns.map((c, j) => (j === i ? { ...c, ...patch } : c)) })
  return (
    <li className={`rounded-lg transition-colors ${highlight ? 'bg-blue-50 ring-1 ring-blue-200' : ''}`}>
      <div className="flex items-center gap-3 px-2 py-1.5">
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
          className="flex items-center gap-2 min-w-0 flex-1 text-left cursor-pointer rounded focus-visible:outline-2 focus-visible:outline-blue-500">
          <span aria-hidden="true" className="text-slate-400 w-3 text-xs">{open ? '▾' : '▸'}</span>
          <span className="mono text-[13px] font-semibold text-slate-900">{t.name}</span>
          <span className="text-xs text-slate-500 truncate hidden sm:inline">{t.description}</span>
        </button>
        {steps.length > 0 && <span className="flex gap-1" title="Kauzální kroky, které tabulku ovlivňují">
          {steps.map((id) => <span key={id} className="mono text-[11px] font-semibold text-blue-700 bg-blue-50 rounded px-1">{id}</span>)}
        </span>}
        <span className="text-xs text-slate-500 tabular-nums w-20 text-right shrink-0">{t.columns.length} sloupců</span>
        {t.strategy !== 'simulation'
          ? <label className="flex items-center gap-1.5 text-xs text-slate-500 shrink-0">řádků
              <input type="number" min={1} className="input py-0.5 tabular-nums" style={{ width: 84 }} value={t.row_count} onChange={(e) => onChange({ ...t, row_count: Number(e.target.value) })} />
            </label>
          : <span className="text-xs text-slate-400 text-right shrink-0" style={{ width: 128 }}>ze simulace</span>}
      </div>
      {open && (
        <div className="pl-7 pr-2 pb-3 space-y-2">
          <input className="input text-xs" aria-label={`Popis tabulky ${t.name}`} value={t.description} onChange={(e) => onChange({ ...t, description: e.target.value })} />
          {locked && <p className="text-xs text-violet-800">{t.flags.includes('scm')
            ? 'Schéma je odvozené z kauzálního modelu (SCM). Mění se úpravou modelu, ne tabulky.'
            : 'Schéma tabulky vlastní deterministický simulátor. Sloupce měnit nelze, popis ano.'}</p>}
          <div className="overflow-x-auto"><table className="data">
            <thead><tr><th>Sloupec</th><th>Typ</th><th>PK</th><th>FK</th><th>Popis a omezení</th>{!locked && <th><span className="sr-only">Akce</span></th>}</tr></thead>
            <tbody>
              {t.columns.map((c, i) => (
                <tr key={i}>
                  <td>{locked ? <span className="mono">{c.name}</span> : <input className="input mono py-0.5" value={c.name} onChange={(e) => setCol(i, { name: e.target.value })} />}</td>
                  <td>{locked ? c.type : (
                    <select className="input py-0.5" value={c.type} onChange={(e) => setCol(i, { type: e.target.value as Column['type'] })}>
                      {['int', 'float', 'text', 'date', 'datetime', 'bool'].map((x) => <option key={x}>{x}</option>)}
                    </select>)}</td>
                  <td>{c.pk ? 'ano' : ''}</td>
                  <td className="mono text-xs">{locked ? c.fk ?? '' : <input className="input mono py-0.5" value={c.fk ?? ''} placeholder="tabulka.sloupec" onChange={(e) => setCol(i, { fk: e.target.value || null })} />}</td>
                  <td className="text-xs text-slate-600">
                    {locked ? c.description : <input className="input py-0.5" value={c.description} onChange={(e) => setCol(i, { description: e.target.value })} />}
                    {c.enum_values?.length > 0 && <div className="text-slate-500">∈ {c.enum_values.join(', ')}</div>}
                    {(c.min !== null || c.max !== null) && <div className="text-slate-500">[{c.min ?? '−∞'}, {c.max ?? '∞'}]</div>}
                  </td>
                  {!locked && <td><button className="text-red-700 text-xs cursor-pointer" onClick={() => onChange({ ...t, columns: t.columns.filter((_, j) => j !== i) })}>Odebrat</button></td>}
                </tr>
              ))}
            </tbody>
          </table></div>
          {!locked && (
            <div className="flex gap-2">
              <button className="btn text-xs" onClick={() => onChange({ ...t, columns: [...t.columns, newColumn()] })}>Přidat sloupec</button>
              <button className="btn btn-danger text-xs" onClick={onRemove}>Odebrat tabulku</button>
            </div>
          )}
        </div>
      )}
    </li>
  )
}

export default function SpecReview() {
  const { sid } = useParams()
  const nav = useNavigate()
  const { state } = useLocation()
  const [session, setSession] = useState<Json>(null)
  const [spec, setSpec] = useState<Spec | null>(null)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [decisions, setDecisions] = useState<Record<string, 'accept' | 'reject'>>({})
  const [feedback, setFeedback] = useState('')
  const [job, setJob] = useState<{ id: string; kind: 'replan' | 'run' } | null>(null)
  const [errors, setErrors] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [activeStep, setActiveStep] = useState<string | null>(null)
  const [tab, setTab] = useState<'tables' | 'knobs' | 'settings' | 'checks' | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const load = () => api.session(sid!).then((s) => {
    setSession(s)
    const r: PlannerResponse = s.response
    setSpec(r.spec)
    setAnswers(Object.fromEntries(r.questions.map((q) => [q.id, q.default_answer])))
    setDecisions({})
    setFeedback('')
  })
  useEffect(() => { load().catch((e) => setLoadError(String(e))) }, [sid]) // eslint-disable-line react-hooks/exhaustive-deps

  const resp: PlannerResponse | null = session?.response ?? null
  const edges = useMemo(() => spec?.tables.flatMap((t) => t.columns.filter((c) => c.fk).map((c) => `${t.name}.${c.name} → ${c.fk}`)) ?? [], [spec])
  const stepsByTable = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const s of spec?.story.causal_chain ?? []) for (const t of s.tables) m.set(t, [...(m.get(t) ?? []), s.id])
    return m
  }, [spec])

  if (loadError) return <div role="alert" className="text-red-700">{loadError}</div>
  if (!spec || !resp) return <div className="text-slate-500">Načítám návrh…</div>
  const runSearch = typeof state?.runSearch === 'string' && state.runSearch ? `?${state.runSearch}` : ''
  const resultsUrl = session.parent_run_id ? `/runs/${session.parent_run_id}${runSearch}` : undefined
  if (spec.scm) return <ScmPanel key={`${sid}:${session.round}`} spec={spec} input={session.input}
    sessionId={session.session_id} round={session.round} parentRunId={session.parent_run_id} resultsUrl={resultsUrl}
    notes={resp.notes} reload={load} />

  const meta = spec.meta
  const setMeta = (patch: Partial<Spec['meta']>) => setSpec({ ...spec, meta: { ...meta, ...patch } })
  const updTable = (i: number, t: Table) => setSpec({ ...spec, tables: spec.tables.map((x, j) => (j === i ? t : x)) })
  const setKnob = (i: number, value: string) => setSpec({ ...spec, knobs: spec.knobs.map((k, j) => (j === i ? { ...k, default: value } : k)) })
  const accepted = resp.suggestions.filter((s) => decisions[s.id] === 'accept')
  const roundsLeft = Math.max(0, 4 - session.round)

  const approve = async () => {
    setBusy(true)
    try {
      let final = spec
      for (const s of accepted) final = applySuggestion(final, s)
      const v = await api.validateSpec(final).catch((e) => ({ errors: [String(e)] }))
      if (v.errors.length) { setErrors(v.errors); return }
      setErrors([])
      const knob_overrides = Object.fromEntries(final.knobs.map((k) => [k.name, k.default]))
      const r = await api.startRun({ spec: final, knob_overrides, input: session.input, parent_run_id: session.parent_run_id, label: 'Běh ze schváleného specu' })
      setJob({ id: r.job_id, kind: 'run' })
    } catch (e) { setErrors([String(e)]) } finally { setBusy(false) }
  }
  const replan = async () => {
    setBusy(true)
    try {
      const r = await api.replan(sid!, {
        spec, feedback, answers,
        accepted_suggestions: accepted.map((s) => s.id),
        rejected_suggestions: resp.suggestions.filter((s) => decisions[s.id] === 'reject').map((s) => s.id),
      })
      setJob({ id: r.job_id, kind: 'replan' })
    } catch (e) { setErrors([String(e)]) } finally { setBusy(false) }
  }
  const decide = (id: string, d: 'accept' | 'reject') => {
    const next = { ...decisions }
    if (next[id] === d) delete next[id]; else next[id] = d
    setDecisions(next)
  }

  if (job) {
    return (
      <div className="max-w-4xl mx-auto space-y-4">
        <JobProgress jobId={job.id} title={job.kind === 'run' ? 'Generuji dataset' : 'Přeplánovávám návrh (STRONG)'}
          phases={job.kind === 'run' ? undefined : [['plan', 'Přeplánování'], ['done', 'Hotovo']]}
          onDone={(r) => { if (job.kind === 'run') nav(`/runs/${r.run_id}`); else { setJob(null); load() } }} />
        <button className="btn" onClick={() => setJob(null)}>Zpět k návrhu</button>
      </div>
    )
  }

  const TABS = [
    ['tables', `Tabulky ${spec.tables.length}`],
    ...(spec.knobs.length ? [['knobs', `Parametry ${spec.knobs.length}`]] : []),
    ['settings', 'Nastavení'],
    ['checks', 'Kontroly a důkazy'],
  ] as [NonNullable<typeof tab>, string][]

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_20rem] gap-6 items-start">
      <div className="space-y-6 min-w-0">
        <header className="space-y-1">
          <textarea rows={1} className="input-quiet field-sizing-content resize-none text-2xl leading-tight font-bold tracking-tight text-slate-900 -ml-1.5" aria-label="Název scénáře"
            value={meta.name} onChange={(e) => setMeta({ name: e.target.value })} />
          <textarea rows={2} className="input-quiet field-sizing-content resize-none text-[17px] leading-relaxed text-slate-700 -ml-1.5 max-w-4xl" aria-label="Shrnutí příběhu"
            value={spec.story.logline} onChange={(e) => setSpec({ ...spec, story: { ...spec.story, logline: e.target.value } })} />
        </header>

        <section className="card p-5 sm:p-6" aria-labelledby="story-h">
          <h2 id="story-h" className="text-lg font-semibold text-slate-900 mb-3">Příběh v čase</h2>
          <StoryTimeline chain={spec.story.causal_chain} distractors={spec.story.distractors} knobs={spec.knobs}
            periodStart={meta.period_start} periodEnd={meta.period_end} active={activeStep} onActive={setActiveStep}
            onMechanism={(i, v) => setSpec({ ...spec, story: { ...spec.story, causal_chain: spec.story.causal_chain.map((s, j) => (j === i ? { ...s, mechanism: v } : s)) } })}
            onRemoveDistractor={(i) => setSpec({ ...spec, story: { ...spec.story, distractors: spec.story.distractors.filter((_, j) => j !== i) } })} />
          {spec.story.ground_truth_facts.length > 0 && (
            <div className="mt-5 pt-4 border-t border-slate-200">
              <h3 className="text-sm font-semibold text-slate-800 mb-1.5">Správná odpověď: co má analytik z dat odhalit</h3>
              <ul className="space-y-1 text-sm leading-snug text-slate-700 list-disc ml-4 marker:text-blue-600">
                {spec.story.ground_truth_facts.map((f, i) => <li key={i}>{f}</li>)}
              </ul>
            </div>
          )}
        </section>

        <section className="card" aria-label="Podrobnosti návrhu">
          <div className="flex flex-wrap items-center gap-1 p-2">
            <span className="text-sm text-slate-500 px-2">Podrobnosti</span>
            {TABS.map(([id, label]) => (
              <button key={id} type="button" aria-expanded={tab === id} aria-controls="detail-panel" onClick={() => setTab(tab === id ? null : id)}
                className={`px-3 py-1.5 rounded-lg text-sm cursor-pointer focus-visible:outline-2 focus-visible:outline-blue-500 border ${tab === id ? 'bg-slate-900 border-slate-900 text-white' : 'border-slate-200 text-slate-700 hover:bg-slate-50'}`}>{label}</button>
            ))}
          </div>
          {tab && <div id="detail-panel" className="border-t border-slate-100 p-5 sm:p-6 space-y-5">
            {tab === 'tables' && <>
              <RelationalSchema tables={spec.tables} />
              {GROUPS.map(([strategy, title]) => {
                const rows = spec.tables.map((t, i) => [t, i] as const).filter(([t]) => t.strategy === strategy)
                if (!rows.length) return null
                return (
                  <div key={strategy}>
                    <h3 className="text-sm font-semibold text-slate-800 mb-1">{title}</h3>
                    <ul className="divide-y divide-slate-100 border-y border-slate-100">
                      {rows.map(([t, i]) => <TableRow key={i} t={t} steps={stepsByTable.get(t.name) ?? []}
                        highlight={!!activeStep && (stepsByTable.get(t.name) ?? []).includes(activeStep)}
                        onChange={(nt) => updTable(i, nt)} onRemove={() => setSpec({ ...spec, tables: spec.tables.filter((_, j) => j !== i) })} />)}
                    </ul>
                  </div>
                )
              })}
              <div className="flex flex-wrap items-center gap-4">
                <button className="btn text-xs" onClick={() => setSpec({ ...spec, tables: [...spec.tables, { name: 'new_table', description: '', strategy: 'llm_rows', row_count: 30, flags: [], columns: [{ ...newColumn(), name: 'id', type: 'int', pk: true, nullable: false }] }] })}>Přidat tabulku</button>
                <details className="text-xs"><summary className="cursor-pointer text-slate-700">Cizí klíče: {edges.length}</summary>
                  <div className="mono grid md:grid-cols-2 gap-x-6 mt-2 text-slate-600">{edges.map((e) => <div key={e}>{e}</div>)}</div>
                </details>
              </div>
            </>}

            {tab === 'knobs' && <div className="grid md:grid-cols-2 gap-x-6 gap-y-4">
              {spec.knobs.map((k, i) => (
                <label key={k.name} className="block">
                  <span className="block text-sm font-medium text-slate-800 mb-1">{k.label || k.name}</span>
                  {k.type === 'enum' ? (
                    <select className="input" value={k.default} onChange={(e) => setKnob(i, e.target.value)}>
                      {k.options.map((o) => <option key={o}>{o}</option>)}
                    </select>
                  ) : k.type === 'bool' ? (
                    <select className="input" value={k.default} onChange={(e) => setKnob(i, e.target.value)}>
                      <option value="true">ano</option><option value="false">ne</option>
                    </select>
                  ) : (
                    <input className="input tabular-nums" type={k.type === 'date' ? 'date' : 'number'} min={k.min ?? undefined} max={k.max ?? undefined}
                      step={k.type === 'float' ? 0.1 : 1} value={k.default} onChange={(e) => setKnob(i, e.target.value)} />
                  )}
                  <span className="block text-xs text-slate-500 mt-1">{k.description}</span>
                </label>
              ))}
            </div>}

            {tab === 'settings' && <>
              <div className="flex flex-wrap items-end gap-4">
                {meta.period_start !== null ? <>
                  <label><span className="label">Období od</span><input type="date" className="input" value={meta.period_start ?? ''} onChange={(e) => setMeta({ period_start: e.target.value })} /></label>
                  <label><span className="label">do</span><input type="date" className="input" value={meta.period_end ?? ''} onChange={(e) => setMeta({ period_end: e.target.value })} /></label>
                  {!meta.archetype && <button className="btn text-xs" onClick={() => setMeta({ period_start: null, period_end: null })}>Bez časové osy</button>}
                </> : <p className="text-sm text-slate-600">Data bez časové osy.{' '}
                  <button className="text-blue-700 underline cursor-pointer" onClick={() => setMeta({ period_start: '2025-01-01', period_end: '2025-12-31' })}>Přidat období</button></p>}
                <label className="w-28"><span className="label">Seed</span><input type="number" className="input tabular-nums" value={meta.seed} onChange={(e) => setMeta({ seed: Number(e.target.value) })} /></label>
                <label className="w-28"><span className="label">Jazyk dat</span><input className="input" value={meta.data_language} onChange={(e) => setMeta({ data_language: e.target.value })} /></label>
              </div>
              <div className="grid md:grid-cols-2 gap-4">
                <label><span className="label">Firma</span><textarea className="input h-24 text-[13px] leading-relaxed" value={meta.company} onChange={(e) => setMeta({ company: e.target.value })} /></label>
                <label><span className="label">Prostředí</span><textarea className="input h-24 text-[13px] leading-relaxed" value={meta.environment} onChange={(e) => setMeta({ environment: e.target.value })} /></label>
              </div>
              <p className="text-sm text-slate-600">{meta.archetype ? <>Data vytvoří simulátor <span className="mono text-violet-800">{meta.archetype}</span>.</> : 'Řádky generuje přímo model.'}{resp.notes && <> {resp.notes}</>}</p>
            </>}

            {tab === 'checks' && <div className="divide-y divide-slate-100 text-sm">
              <details className="py-2"><summary className="cursor-pointer font-medium text-slate-800">Kontrolovaná pravidla: {spec.invariants.length}</summary>
                <ul className="mt-2 space-y-1.5">{spec.invariants.map((v: Json) => <li key={v.id}><span className="mono text-xs text-slate-500 mr-2">{v.kind}</span>{v.description}{v.sql && <code className="block text-xs text-slate-500 truncate">{v.sql}</code>}</li>)}</ul>
              </details>
              <details className="py-2"><summary className="cursor-pointer font-medium text-slate-800">Důkazní dotazy: {spec.evidence.length}</summary>
                <ul className="mt-2 space-y-3">{spec.evidence.map((e) => <li key={e.id}><span className="font-medium">{e.title}</span> <span className="text-xs text-slate-500">krok {e.step_id ?? '–'}, graf {e.chart}</span><div className="text-xs text-slate-600">{e.expected}</div><code className="block text-[11px] text-slate-500 whitespace-pre-wrap">{e.sql}</code></li>)}</ul>
              </details>
              <details className="py-2"><summary className="cursor-pointer font-medium text-slate-800">Dashboard: {spec.dashboard.widgets.length} widgetů</summary>
                <ul className="mt-2">{spec.dashboard.widgets.map((w: Json) => <li key={w.id}>{w.title} <span className="text-xs text-slate-500">({w.chart})</span></li>)}</ul>
              </details>
            </div>}
          </div>}
        </section>
      </div>

      <aside className="lg:sticky lg:top-4 card divide-y divide-slate-100" aria-label="Rozhodnutí">
        {resp.suggestions.length > 0 && <div className="p-5 space-y-3">
          <h2 className="text-sm font-semibold text-slate-800">Návrhy na doplnění</h2>
          <ul className="space-y-3">
            {resp.suggestions.map((s) => {
              const d = decisions[s.id]
              const target = suggestionTarget(s)
              return (
                <li key={s.id} title={s.description} className={`border-l-2 pl-3 transition-colors ${d === 'accept' ? 'border-emerald-600' : d === 'reject' ? 'border-slate-200 opacity-60' : 'border-amber-400'}`}>
                  <p className="text-sm font-medium text-slate-900 leading-snug">{s.title}</p>
                  <p className="text-xs text-slate-500 mb-1.5">{SUGGESTION_KIND[s.kind] ?? s.kind}{target && <> <span className="mono">{target}</span></>}</p>
                  <div className="inline-flex rounded-lg border border-slate-300 overflow-hidden text-xs font-medium" role="group" aria-label={`Rozhodnutí: ${s.title}`}>
                    <button type="button" aria-pressed={d === 'accept'} onClick={() => decide(s.id, 'accept')}
                      className={`px-3 py-1 cursor-pointer focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-500 ${d === 'accept' ? 'bg-emerald-700 text-white' : 'bg-white hover:bg-slate-50'}`}>Přijmout</button>
                    <button type="button" aria-pressed={d === 'reject'} onClick={() => decide(s.id, 'reject')}
                      className={`px-3 py-1 border-l border-slate-300 cursor-pointer focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-500 ${d === 'reject' ? 'bg-slate-700 text-white' : 'bg-white hover:bg-slate-50'}`}>Odmítnout</button>
                  </div>
                </li>
              )
            })}
          </ul>
        </div>}

        <div className="p-5 space-y-2">
          <button className="btn btn-primary w-full justify-center py-2.5 text-base" disabled={busy} onClick={() => void approve()}>
            {busy ? 'Pracuji…' : 'Schválit a generovat data'}</button>
          {resultsUrl && <Link className="btn w-full justify-center" to={resultsUrl}>Zpět k výsledkům bez generování</Link>}
          {accepted.length > 0 && <p className="text-xs text-slate-500">Přidá se {accepted.length} přijatých návrhů.</p>}
          {errors.length > 0 && <div role="alert" className="text-sm text-red-800">
            <p className="font-semibold">Návrh nelze spustit</p>
            <ul className="list-disc ml-4 text-xs">{errors.map((e) => <li key={e}>{e}</li>)}</ul>
          </div>}
        </div>

        <details className="group">
          <summary className="px-5 py-3 text-sm text-slate-700 cursor-pointer hover:bg-slate-50 rounded-b-xl group-open:rounded-none">
            Nechat model návrh upravit{resp.questions.length > 0 && <span className="text-slate-500"> (dotazy: {resp.questions.length})</span>}</summary>
          <div className="px-5 pb-5 space-y-4">
            {resp.questions.map((q) => (
              <label key={q.id} className="block">
                <span className="block text-sm text-slate-800 leading-snug mb-1.5" title={q.why}>{q.question}</span>
                <input className="input" value={answers[q.id] ?? ''} onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })} />
              </label>
            ))}
            <label className="block"><span className="block text-sm text-slate-800 mb-1.5">Co má model změnit?</span>
              <textarea className="input h-20" value={feedback} onChange={(e) => setFeedback(e.target.value)} placeholder="Například: přidej tabulku směn personálu." /></label>
            <button className="btn w-full justify-center" disabled={busy || roundsLeft === 0} onClick={() => void replan()}>
              {roundsLeft === 0 ? 'Přeplánování vyčerpáno' : `Přeplánovat (zbývá ${roundsLeft}×)`}</button>
          </div>
        </details>
      </aside>
    </div>
  )
}
