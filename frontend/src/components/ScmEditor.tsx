// Editing of an SCM through the backend edit contract (POST /api/scm/edit): every change is sent as a
// list of operations, validated on the server and applied only if the model stays executable.
import { useMemo, useState } from 'react'
import { type Json } from '../api'
import { diffParams, isObj } from './scmEditOps'

export type ApplyFn = (ops: Json[]) => Promise<{ ok: boolean; messages: string[] }>

const FAMILIES = ['normal', 'uniform', 'lognormal', 'exponential', 'gamma', 'bernoulli', 'binomial', 'poisson',
  'negative_binomial', 'categorical', 'beta', 'student_t', 'constant']
const AGGS = ['', 'sum', 'mean', 'count', 'min', 'max', 'any']
const HINT: Record<string, string> = {
  intercept: 'absolutní člen', coefficients: 'koeficienty (term → β)', noise: 'strukturální šum', sd: 'směrodatná odchylka',
  mean: 'střední hodnota', predictor: 'lineární prediktor (výraz)', expression: 'výraz', strength: 'síla vztahu α (0–1)',
  threshold: 'práh', cutpoints: 'prahy kategorií', phi: 'autokorelace φ', distribution: 'rozdělení', family: 'typ rozdělení',
  probs: 'pravděpodobnosti', categories: 'kategorie', table: 'tabulka podmíněných pravděpodobností', default: 'výchozí',
  cases: 'případy', when: 'podmínka', value: 'hodnota', dispersion: 'disperze (NegBin)', link: 'link funkce',
  explicitness: 'explicitnost textu (0–1)', rate: 'intenzita λ', p: 'pravděpodobnost', delta: 'posun', factor: 'násobek',
  param: 'parametr mechanismu', log_mean: 'průměr logaritmu', log_sd: 'sd logaritmu',
}
const LONG = new Set(['predictor', 'expression', 'when', 'value', 'default', 'param'])

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x))

function changed(a: Json, b: Json): Json {
  return Object.fromEntries(Object.keys(b).filter((k) => JSON.stringify(a[k]) !== JSON.stringify(b[k])).map((k) => [k, b[k]]))
}

function NumberList({ value, onChange }: { value: number[]; onChange: (v: number[]) => void }) {
  const [text, setText] = useState(value.join(', '))
  return <input className="input mono py-0.5" value={text} onChange={(e) => setText(e.target.value)}
    onBlur={() => onChange(text.split(',').map((x) => Number(x.trim())).filter((x) => Number.isFinite(x)))} />
}

function Field({ k, value, onChange }: { k: string; value: Json; onChange: (v: Json) => void }) {
  if (typeof value === 'number') {
    return <input type="number" step="any" className="input py-0.5 mono" value={value}
      onChange={(e) => onChange(e.target.value === '' ? 0 : Number(e.target.value))} />
  }
  if (typeof value === 'boolean') return <input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />
  if (typeof value === 'string') {
    if (k === 'family') {
      return <select className="input py-0.5" value={value} onChange={(e) => onChange(e.target.value)}>
        {FAMILIES.map((f) => <option key={f}>{f}</option>)}</select>
    }
    return LONG.has(k) || value.length > 40
      ? <textarea className="input mono text-xs h-14" value={value} onChange={(e) => onChange(e.target.value)} />
      : <input className="input py-0.5 mono" value={value} onChange={(e) => onChange(e.target.value)} />
  }
  if (Array.isArray(value)) {
    if (value.every((x) => typeof x === 'number')) return <NumberList value={value} onChange={onChange} />
    if (value.every((x) => typeof x === 'string')) {
      return <textarea className="input text-xs h-16" value={value.join('\n')} title="jedna položka na řádek"
        onChange={(e) => onChange(e.target.value.split('\n'))} />
    }
    return <div className="space-y-1">{value.map((item, i) => (
      <div key={i} className="border-l-2 border-slate-200 pl-2">
        <div className="text-[10px] text-slate-400">#{i + 1}</div>
        <Field k={k} value={item} onChange={(v) => onChange(value.map((x, j) => (j === i ? v : x)))} />
      </div>))}</div>
  }
  if (isObj(value)) return <ParamFields value={value} onChange={onChange} />
  return <span className="text-xs text-slate-400">—</span>
}

export function ParamFields({ value, onChange }: { value: Json; onChange: (v: Json) => void }) {
  return (
    <div className="grid grid-cols-[minmax(110px,max-content)_1fr] gap-x-3 gap-y-1.5 items-start">
      {Object.entries(value).map(([k, v]) => (
        <div key={k} className="contents">
          <div className="text-xs pt-1"><span className="mono">{k}</span>{HINT[k] && <div className="text-[10px] text-slate-500">{HINT[k]}</div>}</div>
          <Field k={k} value={v} onChange={(nv) => onChange({ ...value, [k]: nv })} />
        </div>
      ))}
    </div>
  )
}

function ApplyBar({ dirty, busy, onApply, onReset }: { dirty: boolean; busy: boolean; onApply: () => void; onReset: () => void }) {
  return (
    <div className="flex gap-2 pt-2">
      <button className="btn btn-primary text-xs" disabled={!dirty || busy} onClick={onApply}>{busy ? 'Ověřuji…' : 'Použít změny'}</button>
      <button className="btn text-xs" disabled={!dirty || busy} onClick={onReset}>Zahodit</button>
    </div>
  )
}

// ---------------------------------------------------------------------------- variable
function VariableEditor({ scm, v, apply }: { scm: Json; v: Json; apply: ApplyFn }) {
  const [params, setParams] = useState<Json>(() => clone(v.mechanism.params))
  const [meta, setMeta] = useState<Json>(() => ({ observability: v.observability, column: v.column ?? '', constraints: clone(v.constraints) }))
  const [json, setJson] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; messages: string[] } | null>(null)
  const [add, setAdd] = useState({ source: '', lag: 0, aggregation: '', coefficient: '' })

  const ops = useMemo(() => {
    let target = params
    if (json !== null) { try { target = JSON.parse(json) } catch { return null } }
    const out: Json[] = diffParams(v.mechanism.params, target).map((d) => ({ op: 'set_parameter', target: v.variable_id, ...d }))
    const m = changed({ observability: v.observability, column: v.column ?? '', constraints: v.constraints }, meta)
    if (Object.keys(m).length) out.push({ op: 'update_variable', target: v.variable_id, value: { ...m, ...(m.column !== undefined ? { column: m.column || null } : {}) } })
    return out
  }, [params, meta, json, v])

  const run = async (o: Json[]) => {
    setBusy(true)
    const r = await apply(o)
    setBusy(false)
    setMsg(r)
  }
  const reset = () => { setParams(clone(v.mechanism.params)); setJson(null); setMeta({ observability: v.observability, column: v.column ?? '', constraints: clone(v.constraints) }); setMsg(null) }
  const others: Json[] = scm.variables  // incl. itself: a lagged self-reference = autoregression

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="mono font-semibold">{v.variable_id}</span>
        <span className="chip bg-slate-100 text-xs">{v.mechanism.type}</span>
        <span className="text-xs text-slate-500">grain {v.entity} · {v.data_type}</span>
        <button className="ml-auto text-xs text-slate-600 underline cursor-pointer"
          onClick={() => setJson(json === null ? JSON.stringify(params, null, 2) : null)}>{json === null ? 'upravit jako JSON' : 'formulář'}</button>
      </div>
      <div>
        <div className="text-xs font-semibold text-slate-700 mb-1">Parametry mechanismu</div>
        {json === null
          ? <ParamFields value={params} onChange={setParams} />
          : <textarea className="input mono text-xs h-48" value={json} onChange={(e) => setJson(e.target.value)} />}
        {ops === null && <div className="text-xs text-red-700">Neplatný JSON.</div>}
      </div>
      <div className="grid md:grid-cols-4 gap-2 text-xs">
        <label>pozorovatelnost<select className="input py-0.5" value={meta.observability} onChange={(e) => setMeta({ ...meta, observability: e.target.value })}>
          <option value="observed">pozorovaná</option><option value="latent">latentní</option></select></label>
        <label>sloupec<input className="input py-0.5 mono" value={meta.column} placeholder="(nematerializovat)" onChange={(e) => setMeta({ ...meta, column: e.target.value })} /></label>
        <label>min / max<div className="flex gap-1">
          <input type="number" step="any" className="input py-0.5" value={meta.constraints.min ?? ''} onChange={(e) => setMeta({ ...meta, constraints: { ...meta.constraints, min: e.target.value === '' ? null : Number(e.target.value) } })} />
          <input type="number" step="any" className="input py-0.5" value={meta.constraints.max ?? ''} onChange={(e) => setMeta({ ...meta, constraints: { ...meta.constraints, max: e.target.value === '' ? null : Number(e.target.value) } })} />
        </div></label>
        <label className="flex items-end gap-1 pb-1"><input type="checkbox" checked={!!meta.constraints.clip} onChange={(e) => setMeta({ ...meta, constraints: { ...meta.constraints, clip: e.target.checked } })} /> ořezat do [min, max]</label>
      </div>
      <ApplyBar dirty={!!ops && ops.length > 0} busy={busy} onApply={() => ops && run(ops)} onReset={reset} />

      <div className="border-t border-slate-200 pt-2">
        <div className="text-xs font-semibold text-slate-700 mb-1">Rodiče (hrany do této proměnné)</div>
        <ul className="text-xs space-y-0.5">
          {v.parents.length === 0 && <li className="text-slate-400">žádní – exogenní proměnná</li>}
          {v.parents.map((p: Json) => (
            <li key={p.alias ?? p.variable} className="flex items-center gap-2">
              <span className="mono">{p.alias ?? p.variable}</span><span className="text-slate-500">← {p.variable}{p.lag ? ` (t−${p.lag})` : ''}{p.aggregation ? ` [${p.aggregation}]` : ''}</span>
              <button className="text-red-600 cursor-pointer" disabled={busy} title="Odebrat hranu"
                onClick={() => run([{ op: 'remove_edge', target: v.variable_id, source: p.variable, alias: p.alias ?? null }])}>✕</button>
            </li>))}
        </ul>
        <div className="flex flex-wrap items-end gap-2 mt-2 text-xs">
          <label>nový rodič<select className="input py-0.5" value={add.source} onChange={(e) => setAdd({ ...add, source: e.target.value })}>
            <option value="">—</option>{others.map((x: Json) => <option key={x.variable_id} value={x.variable_id}>{x.variable_id} ({x.entity})</option>)}</select></label>
          <label className="flex flex-col">lag<input type="number" min={0} className="input py-0.5" style={{ width: 60 }} value={add.lag} onChange={(e) => setAdd({ ...add, lag: Number(e.target.value) })} /></label>
          <label>agregace<select className="input py-0.5" value={add.aggregation} onChange={(e) => setAdd({ ...add, aggregation: e.target.value })}>
            {AGGS.map((a) => <option key={a} value={a}>{a || '—'}</option>)}</select></label>
          <label className="flex flex-col">koeficient<input type="number" step="any" className="input py-0.5" style={{ width: 80 }} value={add.coefficient} onChange={(e) => setAdd({ ...add, coefficient: e.target.value })} /></label>
          <button className="btn text-xs" disabled={!add.source || busy} onClick={() => run([{ op: 'add_edge', target: v.variable_id, source: add.source, lag: add.lag,
            aggregation: add.aggregation || null, coefficient: add.coefficient === '' ? null : Number(add.coefficient) }])}>+ přidat hranu</button>
        </div>
      </div>
      {msg && <Messages {...msg} />}
    </div>
  )
}

// ---------------------------------------------------------------------------- events
function EventEditor({ ev, apply }: { ev: Json; apply: ApplyFn }) {
  const pick = (e: Json) => ({ description: e.description, affected_entities: e.affected_entities ?? '', start_time: e.start_time ?? '',
    end_time: e.end_time ?? '', ramp_profile: e.ramp_profile, ramp_periods: e.ramp_periods, intervention_parameters: clone(e.intervention_parameters) })
  const [d, setD] = useState<Json>(() => pick(ev))
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; messages: string[] } | null>(null)
  const diff = changed(pick(ev), d)
  const norm = (x: Json) => Object.fromEntries(Object.entries(x).map(([k, v]) => [k, v === '' ? null : v]))
  const run = async (ops: Json[]) => { setBusy(true); setMsg(await apply(ops)); setBusy(false) }
  return (
    <div className="border border-slate-200 rounded-lg p-3 space-y-2 text-xs">
      <div className="flex items-center gap-2">
        <span className="mono font-semibold">{ev.event_id}</span><span className="chip bg-slate-100">{ev.kind}</span>
        <span className="text-slate-500">{ev.intervention_type} → {ev.affected_variables.join(', ')}</span>
        <button className="ml-auto text-red-600 cursor-pointer" disabled={busy} onClick={() => run([{ op: 'remove_event', target: ev.event_id }])}>odebrat</button>
      </div>
      <div className="grid md:grid-cols-3 gap-2">
        <label className="md:col-span-3">popis<input className="input py-0.5" value={d.description} onChange={(e) => setD({ ...d, description: e.target.value })} /></label>
        <label>zasažené entity (filtr)<input className="input py-0.5 mono" value={d.affected_entities} placeholder="např. supplier_id == 1" onChange={(e) => setD({ ...d, affected_entities: e.target.value })} /></label>
        <label>začátek (datum / krok)<input className="input py-0.5 mono" value={d.start_time} onChange={(e) => setD({ ...d, start_time: e.target.value })} /></label>
        <label>konec (prázdné = do konce)<input className="input py-0.5 mono" value={d.end_time} onChange={(e) => setD({ ...d, end_time: e.target.value })} /></label>
        <label>náběh<select className="input py-0.5" value={d.ramp_profile} onChange={(e) => setD({ ...d, ramp_profile: e.target.value })}>
          {['step', 'linear', 'exponential', 'sigmoid'].map((r) => <option key={r}>{r}</option>)}</select></label>
        <label>délka náběhu (kroků)<input type="number" min={0} className="input py-0.5" value={d.ramp_periods} onChange={(e) => setD({ ...d, ramp_periods: Number(e.target.value) })} /></label>
      </div>
      <div><div className="font-semibold text-slate-700 mb-1">Parametry intervence</div>
        <ParamFields value={d.intervention_parameters} onChange={(v) => setD({ ...d, intervention_parameters: v })} /></div>
      <ApplyBar dirty={Object.keys(diff).length > 0} busy={busy} onApply={() => run([{ op: 'update_event', target: ev.event_id, value: norm(diff) }])}
        onReset={() => { setD(pick(ev)); setMsg(null) }} />
      {msg && <Messages {...msg} />}
    </div>
  )
}

// ---------------------------------------------------------------------------- entities & time
function StructureEditor({ scm, apply }: { scm: Json; apply: ApplyFn }) {
  const [counts, setCounts] = useState<Record<string, number>>(() => Object.fromEntries(
    scm.entities.filter((e: Json) => e.count != null && !e.count_variable).map((e: Json) => [e.entity_id, e.count])))
  const [time, setTime] = useState<Json>(() => (scm.time ? { start: scm.time.start, periods: scm.time.periods } : null))
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; messages: string[] } | null>(null)
  const ops: Json[] = scm.entities.filter((e: Json) => e.entity_id in counts && counts[e.entity_id] !== e.count)
    .map((e: Json) => ({ op: 'update_entity', target: e.entity_id, value: { count: counts[e.entity_id] } }))
  if (time) {
    const t = changed({ start: scm.time.start, periods: scm.time.periods }, time)
    if (Object.keys(t).length) ops.push({ op: 'update_time', value: t })
  }
  return (
    <div className="space-y-3 text-xs">
      {time && <div className="grid grid-cols-2 gap-2 max-w-md">
        <label>začátek období<input type="date" className="input py-0.5" value={time.start} onChange={(e) => setTime({ ...time, start: e.target.value })} /></label>
        <label>počet kroků ({scm.time.frequency})<input type="number" min={1} className="input py-0.5" value={time.periods} onChange={(e) => setTime({ ...time, periods: Number(e.target.value) })} /></label>
      </div>}
      <table className="data">
        <thead><tr><th>Entita</th><th>Řádek =</th><th>Počet</th></tr></thead>
        <tbody>{scm.entities.map((e: Json) => (
          <tr key={e.entity_id}><td className="mono">{e.entity_id}</td><td>{e.grain}</td><td>
            {e.entity_id in counts
              ? <input type="number" min={0} className="input py-0.5" style={{ width: 90 }} value={counts[e.entity_id]}
                  onChange={(ev) => setCounts({ ...counts, [e.entity_id]: Number(ev.target.value) })} />
              : <span className="text-slate-500">{e.kind === 'cross' ? 'součin entit' : `= ${e.count_variable}`}</span>}
          </td></tr>))}</tbody>
      </table>
      <ApplyBar dirty={ops.length > 0} busy={busy} onApply={async () => { setBusy(true); setMsg(await apply(ops)); setBusy(false) }}
        onReset={() => { setCounts(Object.fromEntries(scm.entities.filter((e: Json) => e.count != null && !e.count_variable).map((e: Json) => [e.entity_id, e.count]))); setTime(scm.time ? { start: scm.time.start, periods: scm.time.periods } : null); setMsg(null) }} />
      {msg && <Messages {...msg} />}
    </div>
  )
}

function Messages({ ok, messages }: { ok: boolean; messages: string[] }) {
  return (
    <div className={`text-xs rounded border p-2 ${ok ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-red-200 bg-red-50 text-red-800'}`}>
      {messages.map((m, i) => <div key={i}>{m}</div>)}
    </div>
  )
}

export default function ScmEditor({ scm, selected, onSelect, apply }:
  { scm: Json; selected: string | null; onSelect: (id: string) => void; apply: ApplyFn }) {
  const [tab, setTab] = useState<'var' | 'events' | 'structure'>('var')
  const v = scm.variables.find((x: Json) => x.variable_id === selected)
  return (
    <div className="border border-slate-200 rounded-lg p-3 space-y-3">
      <div className="flex items-center gap-1 border-b border-slate-200 -mt-1">
        {([['var', 'Proměnná'], ['events', `Události (${scm.events.length})`], ['structure', 'Entity a čas']] as const).map(([id, label]) => (
          <button key={id} className={`tab text-xs ${tab === id ? 'tab-active' : ''}`} onClick={() => setTab(id)}>{label}</button>))}
        {tab === 'var' && (
          <select className="input py-0.5 text-xs ml-auto" style={{ width: 220 }} value={selected ?? ''} onChange={(e) => onSelect(e.target.value)}>
            <option value="">— vyber proměnnou (nebo klikni na uzel) —</option>
            {scm.variables.map((x: Json) => <option key={x.variable_id} value={x.variable_id}>{x.variable_id}</option>)}
          </select>)}
      </div>
      {tab === 'var' && (v ? <VariableEditor key={JSON.stringify(v)} scm={scm} v={v} apply={apply} />
        : <div className="text-xs text-slate-500">Klikni na uzel v diagramu nebo vyber proměnnou.</div>)}
      {tab === 'events' && <div className="space-y-2">{scm.events.map((e: Json) =>
        <EventEditor key={`${e.event_id}:${JSON.stringify(e)}`} ev={e} apply={apply} />)}
        {scm.events.length === 0 && <div className="text-xs text-slate-500">Model nemá žádné události.</div>}</div>}
      {tab === 'structure' && <StructureEditor key={JSON.stringify([scm.entities, scm.time])} scm={scm} apply={apply} />}
    </div>
  )
}
