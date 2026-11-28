import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, type Json, type Spec } from '../api'
import CausalGraph from './CausalGraph'
import JobProgress from './JobProgress'
import ScmEditor from './ScmEditor'
import RelationalSchema from './RelationalSchema'

const PHASES: [string, string][] = [
  ['simulation', 'Čistá data'], ['validate', 'Validace'], ['write', 'Uložení'], ['difficulty', 'Pozorovaná data'], ['done', 'Hotovo'],
]
const TITLE: Record<string, string> = {
  exogenous: 'Výchozí rozdělení', linear: 'Lineární', polynomial: 'Polynomiální', interaction: 'Interakce',
  deterministic_expression: 'Výraz', conditional: 'Podmíněný', logistic: 'Logistický', threshold: 'Prahový',
  categorical_cpt: 'Tabulka pravděpodobností', categorical_softmax: 'Softmax', ordinal_threshold: 'Ordinální prahy',
  poisson_regression: 'Poissonova regrese', text_template: 'Textové šablony', lookup: 'Přiřazení hodnot', label: 'Popisek',
}

function distributionLabel(d: Json) {
  return d ? `${d.family}(${Object.entries(d.params ?? {}).map(([key, value]) => `${key}=${value}`).join(', ')})` : ''
}

function noiseLabel(v: Json, catalog: Json) {
  const { noise, distribution } = v.mechanism.params
  if (noise || distribution) return distributionLabel(noise ?? distribution)
  if (v.mechanism.type === 'logistic') return 'Bernoulli podle modelu'
  if (v.mechanism.type === 'poisson_regression') return v.mechanism.params.dispersion ? 'Negativně binomické' : 'Poissonovo'
  if (v.mechanism.type.startsWith('categorical_')) return 'Kategorické podle modelu'
  if (catalog?.deterministic || ['deterministic_expression', 'conditional', 'linear', 'polynomial', 'interaction'].includes(v.mechanism.type)) return 'Bez šumu'
  return 'Dle parametrů'
}

export default function ScmPanel({ spec, input, sessionId, round, parentRunId, resultsUrl, notes, reload }: {
  spec: Spec; input: Json; sessionId: string; round: number; parentRunId?: string; resultsUrl?: string
  notes?: string; reload: () => Promise<void>
}) {
  const nav = useNavigate()
  const [scm, setScm] = useState(spec.scm)
  const [selected, setSelected] = useState<string | null>(null)
  const [validation, setValidation] = useState<{ scm: Json; data?: Json; error?: string } | null>(null)
  const [catalog, setCatalog] = useState<Record<string, Json>>({})
  const [job, setJob] = useState<{ id: string; kind: 'run' | 'replan' } | null>(null)
  const [feedback, setFeedback] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const current = validation?.scm === scm ? validation : null
  const pending = current === null
  const errors: Json[] = current?.data?.report?.checks.filter((c: Json) => !c.passed && c.severity === 'error') ?? []
  const warnings: Json[] = current?.data?.report?.checks.filter((c: Json) => !c.passed && c.severity === 'warning') ?? []
  const isValid = !!current?.data && current.data.report.status !== 'FAIL'
  const edges = useMemo(() => scm.variables.flatMap((v: Json) => v.parents.map((p: Json) => ({
    source: p.variable, target: v.variable_id, lag: p.lag, aggregation: p.aggregation,
  }))), [scm])
  const variableTypes = useMemo(() => new Map<string, string>(scm.variables.map((v: Json) => [v.variable_id, v.data_type])), [scm])

  useEffect(() => {
    let active = true
    const timer = window.setTimeout(() => {
      api.scmValidate(scm).then((data) => { if (active) setValidation({ scm, data }) })
        .catch((err) => { if (active) setValidation({ scm, error: String(err) }) })
    }, 250)
    return () => { active = false; window.clearTimeout(timer) }
  }, [scm])

  useEffect(() => {
    api.scmMechanisms().then((items) => setCatalog(Object.fromEntries(items.map((m) => [m.id, m])))).catch(() => undefined)
  }, [])

  const update = (v: Json) => setScm((model: Json) => ({ ...model,
    variables: model.variables.map((item: Json) => item.variable_id === v.variable_id ? v : item) }))

  const generate = async () => {
    if (!isValid) return
    setBusy(true); setError(null)
    try {
      const { job_id } = await api.startRun({ spec: { ...spec, scm }, input, parent_run_id: parentRunId, label: 'Běh ze schváleného SCM' })
      setJob({ id: job_id, kind: 'run' })
    } catch (err) { setError(String(err)) } finally { setBusy(false) }
  }

  const replan = async () => {
    setBusy(true); setError(null)
    try {
      const { job_id } = await api.replan(sessionId, { spec: { ...spec, scm }, feedback: feedback.trim() })
      setJob({ id: job_id, kind: 'replan' })
    } catch (err) { setError(String(err)) } finally { setBusy(false) }
  }

  const exportModel = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(scm, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `${scm.meta.scenario_id || 'scm'}.json`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  if (job) return <div className="max-w-4xl mx-auto space-y-4">
    <JobProgress jobId={job.id} title={job.kind === 'replan' ? 'Přegenerovávám SCM' : 'Generuji dataset'}
      phases={job.kind === 'replan' ? [] : input?.observation ? PHASES : PHASES.filter(([phase]) => phase !== 'difficulty')}
      onError={(message) => { setError(message); setJob(null) }}
      onDone={(r) => {
        if (job.kind === 'run') nav(`/runs/${r.run_id}`)
        else void reload().catch((err) => { setError(String(err)); setJob(null) })
      }} />
  </div>

  return <div className="space-y-5">
    <header className="flex items-center justify-between gap-3 flex-wrap">
      <div><h1 className="text-xl font-bold">{scm.meta.name || 'Kauzální model'}</h1>
        <p className="text-sm text-slate-600">{scm.entities.filter((e: Json) => e.export).length} tabulek · {scm.variables.length} kauzálních proměnných</p></div>
      <div className="flex flex-wrap gap-2">
        {resultsUrl && <Link className="btn" to={resultsUrl}>Zpět k výsledkům bez generování</Link>}
        <button className="btn" disabled={!isValid} onClick={exportModel}>Exportovat SCM</button>
        <button className="btn btn-primary" disabled={!isValid || busy} onClick={() => void generate()}>{busy ? 'Spouštím…' : 'Schválit a generovat data'}</button>
      </div>
    </header>
    <section className="card p-4 space-y-2">
      <label className="block font-semibold" htmlFor="scm-feedback">Přegenerovat SCM podle změn</label>
      <textarea id="scm-feedback" className="input w-full" rows={2} maxLength={4000}
        placeholder="Např. zvyš počet prodejen na 30 a zachovej vztahy."
        value={feedback} onChange={(e) => setFeedback(e.target.value)} />
      <button className="btn" disabled={busy || !feedback.trim() || round >= 4} onClick={() => void replan()}>
        Přegenerovat SCM
      </button>
      {round >= 4 && <p className="text-xs text-slate-600">Limit přeplánování tohoto návrhu byl vyčerpán.</p>}
      {round > 0 && <p className="text-sm whitespace-pre-wrap" role="status">{notes || 'Upravený SCM je načtený.'}</p>}
    </section>
    {scm.meta.warnings?.length > 0 && <details className="text-sm text-slate-700"><summary className="cursor-pointer">Upozornění k modelu</summary>
      <ul className="list-disc ml-5 mt-1">{scm.meta.warnings.map((warning: string, i: number) => <li key={i}>{warning}</li>)}</ul>
    </details>}
    {(pending || current?.error || errors.length > 0 || warnings.length > 0 || error) && <div className="card p-3 text-sm" role="status">
      {pending && <p className="text-slate-600">Ověřuji model…</p>}
      {(error || current?.error) && <p className="text-red-700">{error || current?.error}</p>}
      {errors.length > 0 && <div className="text-red-700"><p className="font-semibold">Model nelze spustit</p>
        <ul className="list-disc ml-5">{errors.map((c, i) => <li key={i}>{c.target} · {c.explanation}{c.suggested_fix && ` (${c.suggested_fix})`}</li>)}</ul></div>}
      {warnings.length > 0 && <div className="text-amber-800"><p className="font-semibold">Upozornění před generováním</p>
        <ul className="list-disc ml-5">{warnings.map((c, i) => <li key={i}>{c.target} · {c.explanation} {c.suggested_fix}</li>)}</ul></div>}
    </div>}
    <RelationalSchema tables={current?.data?.tables ?? spec.tables} />
    <section className="card p-4 space-y-2">
      <h2 className="font-semibold">Kauzální graf</h2>
      <p className="text-xs text-slate-600">Plný uzel = pozorovaná proměnná, přerušovaný = latentní. Kliknutím na uzel otevřete jeho mechanismus.</p>
      <CausalGraph scm={scm} edges={current?.data?.edges ?? edges} mechanisms={current?.data?.mechanisms ?? {}} showLabels
        selected={selected} onSelect={(id) => {
          setSelected(id)
          requestAnimationFrame(() => document.getElementById(`mechanism-${id}`)?.scrollIntoView({ block: 'nearest' }))
        }} />
    </section>
    <section className="card p-4 space-y-2">
      <h2 className="font-semibold">Kauzální mechanismy</h2>
      <div className="overflow-x-auto"><table className="data">
        <thead><tr><th>Proměnná</th><th>Rodiče</th><th>Mechanismus</th><th>Strukturální šum nebo rozdělení</th></tr></thead>
        {scm.variables.map((v: Json) => {
          const id: string = v.variable_id
          const open = selected === id
          const m = catalog[v.mechanism.type]
          return <tbody key={id}>
            <tr id={`mechanism-${id}`}>
              <th scope="row"><button type="button" className="text-left text-blue-800 font-semibold cursor-pointer" aria-expanded={open}
                onClick={() => setSelected(open ? null : id)}>{v.name || id} <span aria-hidden="true">{open ? '▾' : '▸'}</span></button>
                {v.name && v.name !== id && <div className="text-xs text-slate-500 mono">{id}</div>}
              </th>
              <td>{v.parents.length > 0 ? v.parents.map((p: Json) => (
                <div className="mono text-xs" key={p.alias ?? p.variable}>{p.variable}{p.lag > 0 && ` (t−${p.lag})`}{p.aggregation && ` [${p.aggregation}]`}</div>
              )) : '—'}</td>
              <td><div>{TITLE[v.mechanism.type] ?? v.mechanism.type}</div>
                <div className="text-xs text-slate-600 break-words max-w-lg mono">{current?.data?.mechanisms?.[id] ?? v.mechanism.params.expression ?? v.mechanism.params.predictor}</div>
              </td>
              <td className="text-xs">{noiseLabel(v, m)}</td>
            </tr>
            {open && <tr><td colSpan={4}><div className="bg-slate-50 rounded p-3">
              <ScmEditor variable={v} parentTypes={variableTypes} schema={m?.params_schema} onChange={update} />
              {(v.constraints?.min != null || v.constraints?.max != null) && <label className="flex items-center gap-2 mt-3 text-sm">
                <input type="checkbox" checked={v.constraints.clip}
                  onChange={(e) => update({ ...v, constraints: { ...v.constraints, clip: e.target.checked } })} />
                Omezit hodnoty na zadané meze ({v.constraints.min ?? '−∞'} až {v.constraints.max ?? '∞'})
              </label>}
            </div></td></tr>}
          </tbody>
        })}
      </table></div>
    </section>
  </div>
}
