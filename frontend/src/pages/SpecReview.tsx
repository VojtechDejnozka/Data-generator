import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, type Column, type Json, type PlannerResponse, type Spec, type Suggestion, type Table } from '../api'
import JobProgress from '../components/JobProgress'

const STRATEGY: Record<string, [string, string]> = {
  simulation: ['simulace', 'bg-violet-50 text-violet-800 border-violet-200'],
  llm_dimension: ['LLM master data', 'bg-sky-50 text-sky-800 border-sky-200'],
  llm_rows: ['LLM řádky', 'bg-teal-50 text-teal-800 border-teal-200'],
}
export const StrategyChip = ({ s }: { s: string }) => (
  <span className={`chip border ${STRATEGY[s]?.[1] ?? ''}`}>{STRATEGY[s]?.[0] ?? s}</span>
)

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

function TableCard({ t, onChange, onRemove }: { t: Table; onChange: (t: Table) => void; onRemove: () => void }) {
  const [open, setOpen] = useState(false)
  const locked = t.strategy === 'simulation' || t.flags.includes('module')
  const setCol = (i: number, patch: Partial<Column>) => onChange({ ...t, columns: t.columns.map((c, j) => (j === i ? { ...c, ...patch } : c)) })
  return (
    <div className="border border-slate-200 rounded-lg">
      <div className="flex items-center gap-3 px-3 py-2 cursor-pointer hover:bg-slate-50" onClick={() => setOpen(!open)}>
        <span className="text-slate-400 w-3">{open ? '▾' : '▸'}</span>
        <span className="mono font-semibold">{t.name}</span>
        <StrategyChip s={t.strategy} />
        <span className="text-xs text-slate-500 truncate flex-1">{t.description}</span>
        <span className="text-xs text-slate-500">{t.columns.length} sloupců</span>
        {t.strategy !== 'simulation' && (
          <span onClick={(e) => e.stopPropagation()} className="flex items-center gap-1 text-xs">
            řádků <input type="number" className="input py-0.5" style={{ width: 90 }} value={t.row_count} onChange={(e) => onChange({ ...t, row_count: Number(e.target.value) })} />
          </span>
        )}
      </div>
      {open && (
        <div className="px-3 pb-3 space-y-2">
          <input className="input text-xs" value={t.description} onChange={(e) => onChange({ ...t, description: e.target.value })} />
          {locked && <div className="text-xs text-violet-700">🔒 Schéma tabulky vlastní deterministický simulátor – sloupce nelze měnit (popis ano).</div>}
          <table className="data">
            <thead><tr><th>Sloupec</th><th>Typ</th><th>PK</th><th>FK</th><th>Popis / omezení</th>{!locked && <th></th>}</tr></thead>
            <tbody>
              {t.columns.map((c, i) => (
                <tr key={i}>
                  <td>{locked ? <span className="mono">{c.name}</span> : <input className="input mono py-0.5" value={c.name} onChange={(e) => setCol(i, { name: e.target.value })} />}</td>
                  <td>{locked ? c.type : (
                    <select className="input py-0.5" value={c.type} onChange={(e) => setCol(i, { type: e.target.value as Column['type'] })}>
                      {['int', 'float', 'text', 'date', 'datetime', 'bool'].map((x) => <option key={x}>{x}</option>)}
                    </select>)}</td>
                  <td>{c.pk ? '🔑' : ''}</td>
                  <td className="mono text-xs">{locked ? c.fk ?? '' : <input className="input mono py-0.5" value={c.fk ?? ''} placeholder="tabulka.sloupec" onChange={(e) => setCol(i, { fk: e.target.value || null })} />}</td>
                  <td className="text-xs text-slate-600">
                    {locked ? c.description : <input className="input py-0.5" value={c.description} onChange={(e) => setCol(i, { description: e.target.value })} />}
                    {c.enum_values?.length > 0 && <div className="text-slate-500">∈ {c.enum_values.join(', ')}</div>}
                    {(c.min !== null || c.max !== null) && <div className="text-slate-500">[{c.min ?? '−∞'}, {c.max ?? '∞'}]</div>}
                  </td>
                  {!locked && <td><button className="text-red-600 text-xs cursor-pointer" onClick={() => onChange({ ...t, columns: t.columns.filter((_, j) => j !== i) })}>✕</button></td>}
                </tr>
              ))}
            </tbody>
          </table>
          {!locked && (
            <div className="flex gap-2">
              <button className="btn text-xs" onClick={() => onChange({ ...t, columns: [...t.columns, newColumn()] })}>+ sloupec</button>
              <button className="btn btn-danger text-xs" onClick={onRemove}>Odebrat tabulku</button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function SpecReview() {
  const { sid } = useParams()
  const nav = useNavigate()
  const [session, setSession] = useState<Json>(null)
  const [spec, setSpec] = useState<Spec | null>(null)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [decisions, setDecisions] = useState<Record<string, 'accept' | 'reject'>>({})
  const [feedback, setFeedback] = useState('')
  const [job, setJob] = useState<{ id: string; kind: 'replan' | 'run' } | null>(null)
  const [errors, setErrors] = useState<string[]>([])

  const load = () => api.session(sid!).then((s) => {
    setSession(s)
    const r: PlannerResponse = s.response
    setSpec(r.spec)
    setAnswers(Object.fromEntries(r.questions.map((q) => [q.id, q.default_answer])))
    setDecisions({})
    setFeedback('')
  })
  useEffect(() => { load() }, [sid]) // eslint-disable-line react-hooks/exhaustive-deps

  const resp: PlannerResponse | null = session?.response ?? null
  const edges = useMemo(() => spec?.tables.flatMap((t) => t.columns.filter((c) => c.fk).map((c) => `${t.name}.${c.name} → ${c.fk}`)) ?? [], [spec])

  if (!spec || !resp) return <div className="text-slate-500">Načítám specifikaci…</div>

  const updTable = (i: number, t: Table) => setSpec({ ...spec, tables: spec.tables.map((x, j) => (j === i ? t : x)) })
  const accepted = resp.suggestions.filter((s) => decisions[s.id] === 'accept')

  const approve = async () => {
    let final = spec
    for (const s of accepted) final = applySuggestion(final, s)
    const v = await api.validateSpec(final).catch((e) => ({ errors: [String(e)] }))
    if (v.errors.length) { setErrors(v.errors); return }
    setErrors([])
    const knob_overrides = Object.fromEntries(final.knobs.map((k) => [k.name, k.default]))
    const r = await api.startRun({ spec: final, knob_overrides, input: session.input, label: 'Běh ze schváleného specu' })
    setJob({ id: r.job_id, kind: 'run' })
  }
  const replan = async () => {
    const r = await api.replan(sid!, {
      spec, feedback, answers,
      accepted_suggestions: accepted.map((s) => s.id),
      rejected_suggestions: resp.suggestions.filter((s) => decisions[s.id] === 'reject').map((s) => s.id),
    })
    setJob({ id: r.job_id, kind: 'replan' })
  }

  if (job) {
    return (
      <div className="max-w-4xl mx-auto">
        <JobProgress jobId={job.id} title={job.kind === 'run' ? 'Generuji dataset' : 'Přeplánování (STRONG)'}
          phases={job.kind === 'run' ? undefined : [['plan', 'Přeplánování'], ['done', 'Hotovo']]}
          onDone={(r) => { if (job.kind === 'run') nav(`/runs/${r.run_id}`); else { setJob(null); load() } }} />
      </div>
    )
  }

  return (
    <div className="grid lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2 space-y-5">
        <div className="card p-5">
          <div className="flex items-center gap-3 mb-3">
            <h1 className="text-xl font-bold flex-1">2 · Review specifikace</h1>
            <span className="chip bg-slate-100">kolo {session.round}</span>
            {spec.meta.archetype
              ? <span className="chip border bg-violet-50 border-violet-200 text-violet-800" title="Kauzální simulátor">modul: {spec.meta.archetype}</span>
              : <span className="chip border bg-teal-50 border-teal-200 text-teal-800">obecná cesta llm_rows</span>}
          </div>
          <div className="grid md:grid-cols-4 gap-3">
            <div className="md:col-span-2"><label className="label">Název</label><input className="input" value={spec.meta.name} onChange={(e) => setSpec({ ...spec, meta: { ...spec.meta, name: e.target.value } })} /></div>
            <div><label className="label">Od</label><input type="date" className="input" value={spec.meta.period_start} onChange={(e) => setSpec({ ...spec, meta: { ...spec.meta, period_start: e.target.value } })} /></div>
            <div><label className="label">Do</label><input type="date" className="input" value={spec.meta.period_end} onChange={(e) => setSpec({ ...spec, meta: { ...spec.meta, period_end: e.target.value } })} /></div>
            <div><label className="label">Seed</label><input type="number" className="input" value={spec.meta.seed} onChange={(e) => setSpec({ ...spec, meta: { ...spec.meta, seed: Number(e.target.value) } })} /></div>
            <div><label className="label">Jazyk dat</label><input className="input" value={spec.meta.data_language} onChange={(e) => setSpec({ ...spec, meta: { ...spec.meta, data_language: e.target.value } })} /></div>
          </div>
          {resp.notes && <pre className="mt-3 text-xs bg-slate-50 border border-slate-200 rounded p-2 whitespace-pre-wrap">{resp.notes}</pre>}
        </div>

        <div className="card p-5 space-y-3">
          <h2 className="font-semibold">Příběh a kauzální řetězec</h2>
          <textarea className="input h-16" value={spec.story.logline} onChange={(e) => setSpec({ ...spec, story: { ...spec.story, logline: e.target.value } })} />
          <ol className="relative border-l-2 border-blue-200 ml-2 space-y-3">
            {spec.story.causal_chain.map((s, i) => (
              <li key={s.id} className="ml-4">
                <span className="absolute -left-[9px] w-4 h-4 rounded-full bg-blue-600 border-2 border-white" />
                <div className="flex items-center gap-2 text-sm">
                  <span className="mono text-xs text-slate-500">{s.id}</span>
                  <input className="input py-0.5 font-medium" value={s.mechanism} onChange={(e) => {
                    const cc = [...spec.story.causal_chain]; cc[i] = { ...s, mechanism: e.target.value }
                    setSpec({ ...spec, story: { ...spec.story, causal_chain: cc } })
                  }} />
                </div>
                <div className="text-xs text-slate-600 mt-1 flex flex-wrap gap-x-4">
                  <span>start: <b>{s.start ?? '–'}</b></span><span>profil: {s.ramp_profile}</span>
                  {s.lag_days > 0 && <span>zpoždění: {s.lag_days} d</span>}
                  <span>tabulky: {s.tables.join(', ') || '–'}</span>
                </div>
                <div className="text-xs text-slate-500">Očekávaný dopad: {s.expected_impact}</div>
              </li>
            ))}
          </ol>
          <h3 className="font-semibold text-sm pt-2">Šum a distraktory (červené stopy)</h3>
          <div className="grid md:grid-cols-2 gap-2">
            {spec.story.distractors.map((d, i) => (
              <div key={d.id} className="border border-slate-200 rounded-lg p-2 text-sm">
                <div className="flex items-center gap-2">
                  <span className="font-medium flex-1">{d.name}</span><span className="chip bg-slate-100 text-xs">{d.kind}</span>
                  <button className="text-xs text-red-600 cursor-pointer" onClick={() => setSpec({ ...spec, story: { ...spec.story, distractors: spec.story.distractors.filter((_, j) => j !== i) } })}>✕</button>
                </div>
                <div className="text-xs text-slate-600">{d.description}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="card p-5 space-y-2">
          <div className="flex items-center"><h2 className="font-semibold flex-1">Tabulky ({spec.tables.length})</h2>
            <button className="btn text-xs" onClick={() => setSpec({ ...spec, tables: [...spec.tables, { name: 'new_table', description: '', strategy: 'llm_rows', row_count: 30, flags: [], columns: [{ ...newColumn(), name: 'id', type: 'int', pk: true, nullable: false }] }] })}>+ tabulka (llm_rows)</button>
          </div>
          {spec.tables.map((t, i) => <TableCard key={i} t={t} onChange={(nt) => updTable(i, nt)} onRemove={() => setSpec({ ...spec, tables: spec.tables.filter((_, j) => j !== i) })} />)}
          <details className="text-xs pt-2"><summary className="cursor-pointer font-medium text-slate-700">Vazby (FK) – {edges.length}</summary>
            <div className="mono grid md:grid-cols-2 gap-x-6 mt-2">{edges.map((e) => <div key={e}>{e}</div>)}</div>
          </details>
        </div>

        <div className="card p-5">
          <h2 className="font-semibold mb-2">Knoby (parametry pro what-if)</h2>
          <div className="grid md:grid-cols-2 gap-3">
            {spec.knobs.map((k, i) => (
              <div key={k.name}>
                <label className="label">{k.label || k.name} <span className="mono text-slate-400 font-normal">{k.name}</span></label>
                {k.type === 'enum' ? (
                  <select className="input" value={k.default} onChange={(e) => { const ks = [...spec.knobs]; ks[i] = { ...k, default: e.target.value }; setSpec({ ...spec, knobs: ks }) }}>
                    {k.options.map((o) => <option key={o}>{o}</option>)}
                  </select>
                ) : k.type === 'bool' ? (
                  <select className="input" value={k.default} onChange={(e) => { const ks = [...spec.knobs]; ks[i] = { ...k, default: e.target.value }; setSpec({ ...spec, knobs: ks }) }}>
                    <option value="true">ano</option><option value="false">ne</option>
                  </select>
                ) : (
                  <input className="input" type={k.type === 'date' ? 'date' : 'number'} min={k.min ?? undefined} max={k.max ?? undefined}
                    step={k.type === 'float' ? 0.1 : 1} value={k.default}
                    onChange={(e) => { const ks = [...spec.knobs]; ks[i] = { ...k, default: e.target.value }; setSpec({ ...spec, knobs: ks }) }} />
                )}
                <div className="text-xs text-slate-500 mt-0.5">{k.description}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="card p-5 space-y-2">
          <details><summary className="font-semibold cursor-pointer">Invarianty ({spec.invariants.length}) – deterministicky kontrolovaná pravidla</summary>
            <ul className="text-sm mt-2 space-y-1">{spec.invariants.map((v: Json) => <li key={v.id}><span className="chip bg-slate-100 mono mr-2">{v.kind}</span>{v.description}{v.sql && <code className="block text-xs text-slate-500 truncate">{v.sql}</code>}</li>)}</ul>
          </details>
          <details><summary className="font-semibold cursor-pointer">Důkazní dotazy ({spec.evidence.length})</summary>
            <ul className="text-sm mt-2 space-y-2">{spec.evidence.map((e) => <li key={e.id}><b>{e.title}</b> <span className="text-xs text-slate-500">[{e.step_id} · {e.chart}]</span><div className="text-xs text-slate-600">{e.expected}</div><code className="block text-[11px] text-slate-500 whitespace-pre-wrap">{e.sql}</code></li>)}</ul>
          </details>
          <details><summary className="font-semibold cursor-pointer">Dashboard ({spec.dashboard.widgets.length} widgetů)</summary>
            <ul className="text-sm mt-2">{spec.dashboard.widgets.map((w: Json) => <li key={w.id}>{w.title} <span className="text-xs text-slate-500">[{w.chart}]</span></li>)}</ul>
          </details>
        </div>
      </div>

      <aside className="space-y-5 lg:sticky lg:top-4 self-start">
        {resp.questions.length > 0 && (
          <div className="card p-5 space-y-3">
            <h2 className="font-semibold">Dotazy na upřesnění</h2>
            {resp.questions.map((q) => (
              <div key={q.id}>
                <div className="text-sm font-medium">{q.question}</div>
                <div className="text-xs text-slate-500 mb-1">{q.why}</div>
                <input className="input" value={answers[q.id] ?? ''} onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })} />
              </div>
            ))}
          </div>
        )}
        {resp.suggestions.length > 0 && (
          <div className="card p-5 space-y-3">
            <h2 className="font-semibold">Návrhy na doplnění</h2>
            {resp.suggestions.map((s) => (
              <div key={s.id} className={`border rounded-lg p-2.5 ${decisions[s.id] === 'accept' ? 'border-emerald-300 bg-emerald-50' : decisions[s.id] === 'reject' ? 'border-slate-200 opacity-50' : 'border-slate-200'}`}>
                <div className="text-sm font-medium">{s.title}</div>
                <div className="text-xs text-slate-600 mb-2">{s.description}</div>
                <div className="flex gap-2">
                  <button className="btn text-xs" onClick={() => setDecisions({ ...decisions, [s.id]: 'accept' })}>✓ Přijmout</button>
                  <button className="btn text-xs" onClick={() => setDecisions({ ...decisions, [s.id]: 'reject' })}>✕ Odmítnout</button>
                  <span className="chip bg-slate-100 text-[10px] ml-auto">{s.kind}</span>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="card p-5 space-y-3">
          <label className="label">Zpětná vazba pro přeplánování</label>
          <textarea className="input h-20" value={feedback} onChange={(e) => setFeedback(e.target.value)} placeholder="Např. přidej tabulku směn personálu…" />
          <button className="btn w-full justify-center" disabled={session.round >= 4} onClick={replan}>↻ Přeplánovat se zpětnou vazbou (kolo {session.round + 1}/4)</button>
          <button className="btn btn-primary w-full justify-center py-2.5 text-base" onClick={approve}>✓ Schválit a generovat</button>
          {accepted.length > 0 && <div className="text-xs text-slate-500">Přijaté návrhy ({accepted.length}) se aplikují při schválení.</div>}
          {errors.length > 0 && <ul className="text-xs text-red-700 list-disc ml-4">{errors.map((e) => <li key={e}>{e}</li>)}</ul>}
        </div>
      </aside>
    </div>
  )
}
