import { useEffect, useState } from 'react'
import { api, type Json } from '../api'
import CausalGraph from './CausalGraph'
import ScmEditor from './ScmEditor'

// Overview + editor of an executable structural causal model (backend app.causal) for the spec review.
// The SCM is the single source of truth – the tables of the spec are derived from it. Edits go through
// POST /api/scm/edit and are applied only when the model stays executable (validation != FAIL).
const STATUS: Record<string, string> = {
  PASS: 'bg-emerald-50 text-emerald-800 border-emerald-200',
  PASS_WITH_WARNINGS: 'bg-amber-50 text-amber-800 border-amber-200',
  FAIL: 'bg-red-50 text-red-800 border-red-200',
}

function count(e: Json): string {
  if (e.kind === 'base') return `${e.count} řádků`
  if (e.kind === 'cross') return `${e.cross.join(' × ')}${e.temporal ? ' × čas' : ''}`
  return `${e.count_variable ? `počet = ${e.count_variable}` : `${e.count ?? 1}×`} na ${e.parent}`
}

const CHANGE: Record<string, string> = {
  scm: 'změna mechanismu / struktury – čistá realita se při generování vytvoří znovu',
  structural_noise: 'změna strukturálního šumu – nedotčené proměnné si díky společným náhodným číslům zachovají hodnoty',
  presentation: 'změna popisů – data se nemění',
  difficulty: 'změna nastavení obtížnosti', none: 'beze změny',
}

export default function ScmPanel({ scm, onChange }: { scm: Json; onChange?: (scm: Json, derived: Json) => void }) {
  const [v, setV] = useState<Json>(null)
  const [err, setErr] = useState<string | null>(null)
  const [showLabels, setShowLabels] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [initial] = useState<Json>(scm)
  const [banner, setBanner] = useState<{ ok: boolean; messages: string[] } | null>(null)
  const edited = JSON.stringify(initial) !== JSON.stringify(scm)

  const apply = async (ops: Json[], base: Json = scm): Promise<{ ok: boolean; messages: string[] }> => {
    try {
      const r = await api.scmEdit(base, ops)
      const errors: string[] = [...r.errors, ...r.report.checks.filter((c: Json) => !c.passed && c.severity === 'error')
        .map((c: Json) => `${c.target}: ${c.explanation}`)]
      if (r.report.status === 'FAIL' || r.errors.length) {
        const res = { ok: false, messages: ['Změna nebyla použita – model by nebyl spustitelný:', ...errors] }
        setBanner(res)
        return res
      }
      onChange?.(r.scm, r.spec)
      const res = { ok: true, messages: [`Změna použita: ${CHANGE[r.change.kind] ?? r.change.kind}.`,
        ...(r.change.affected_variables?.length ? [`Ovlivněné proměnné: ${r.change.affected_variables.join(', ')}`] : []),
        ...r.warnings.map((w: string) => `⚠ ${w}`)] }
      setBanner(res)
      return res
    } catch (e) {
      const res = { ok: false, messages: [String(e)] }
      setBanner(res)
      return res
    }
  }
  useEffect(() => {
    api.scmValidate(scm).then((r) => { setV(r); setErr(null) }).catch((e) => setErr(String(e)))
  }, [scm])

  const rep = v?.report
  const time = scm.time
  return (
    <div className="card p-5 space-y-4">
      <div className="flex items-center gap-3">
        <h2 className="font-semibold flex-1">Kauzální model (SCM)</h2>
        {rep && <span className={`chip border ${STATUS[rep.status] ?? ''}`}>validace: {rep.status}</span>}
        <span className="chip bg-slate-100 text-xs">schema v{scm.meta?.schema_version}</span>
      </div>
      {err && <div className="text-sm text-red-700">{err}</div>}
      {scm.meta?.refined_story && <p className="text-sm text-slate-700">{scm.meta.refined_story}</p>}
      <div className="text-xs text-slate-600 flex flex-wrap gap-x-4">
        <span>seed: <b>{scm.meta?.seed}</b></span>
        {time ? <span>čas: <b>{time.start}</b>, {time.periods} kroků ({time.frequency})</span> : <span>bez časové osy</span>}
        <span>{scm.entities.length} entit · {scm.variables.length} proměnných · {scm.events.length} událostí</span>
      </div>
      {rep && rep.status !== 'PASS' && (
        <ul className="text-xs list-disc ml-4">
          {rep.checks.filter((c: Json) => !c.passed && c.severity !== 'info').map((c: Json) => (
            <li key={c.check_id + c.target} className={c.severity === 'error' ? 'text-red-700' : 'text-amber-700'}>
              <span className="mono">{c.target}</span>: {c.explanation}{c.suggested_fix && <> – {c.suggested_fix}</>}
            </li>
          ))}
        </ul>
      )}
      {(scm.meta?.assumptions?.length > 0 || scm.meta?.warnings?.length > 0) && (
        <details className="text-xs"><summary className="cursor-pointer font-medium text-slate-700">Předpoklady a upozornění plánovače</summary>
          <ul className="list-disc ml-4 mt-1">
            {scm.meta.assumptions.map((a: string) => <li key={a}>{a}</li>)}
            {scm.meta.warnings.map((w: string) => <li key={w} className="text-amber-700">{w}</li>)}
          </ul>
        </details>
      )}

      <div>
        <h3 className="font-semibold text-sm mb-1">Entity a granularita</h3>
        <table className="data">
          <thead><tr><th>Entita</th><th>Tabulka</th><th>Řádek =</th><th>Počet / struktura</th></tr></thead>
          <tbody>{scm.entities.map((e: Json) => (
            <tr key={e.entity_id}>
              <td className="mono">{e.entity_id}</td>
              <td className="mono">{e.export ? e.name : <span className="text-slate-400">interní</span>}</td>
              <td className="text-xs">{e.grain}</td>
              <td className="text-xs">{count(e)}{e.assignments?.length > 0 && <> · FK → {e.assignments.map((a: Json) => a.entity).join(', ')}</>}</td>
            </tr>))}</tbody>
        </table>
      </div>

      <div>
        <div className="flex items-center gap-3 mb-1">
          <h3 className="font-semibold text-sm flex-1">Kauzální diagram</h3>
          <label className="text-xs text-slate-600 flex items-center gap-1 cursor-pointer">
            <input type="checkbox" checked={showLabels} onChange={(e) => setShowLabels(e.target.checked)} /> i názvy a texty
          </label>
        </div>
        {v?.edges
          ? <div className="border border-slate-200 rounded-lg">
              <CausalGraph scm={scm} edges={v.edges} mechanisms={v.mechanisms ?? {}} showLabels={showLabels}
                selected={onChange ? selected : null} onSelect={onChange ? setSelected : undefined} />
            </div>
          : <div className="text-xs text-slate-500">Načítám diagram…</div>}
        <div className="text-[11px] text-slate-500 mt-1 flex flex-wrap gap-x-4">
          <span>vlevo příčiny → vpravo následky (pořadí výpočtu v rámci kroku)</span>
          <span>● pozorovaná · ◌ přerušovaně = latentní</span><span>◆ ⚡ = mění ji událost</span>
          <span>- - - = vliv s časovým zpožděním (t−k)</span><span>↻ = závisí na vlastní minulosti</span>
          <span>+ / − = směr efektu · [sum] = agregace</span><span>barva = entita (klik v legendě ji skryje)</span>
          <span>najetí na uzel = rovnice a zvýraznění sousedů · široký model se posouvá vodorovně</span>
          {onChange && <span>klik na uzel = úprava parametrů</span>}
        </div>
      </div>

      {onChange && (
        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <h3 className="font-semibold text-sm flex-1">Úprava parametrů modelu</h3>
            {edited && <button className="btn text-xs" onClick={() => { apply([], initial); setSelected(null) }}>↺ Vrátit původní model</button>}
          </div>
          {banner && (
            <div className={`text-xs rounded border p-2 ${banner.ok ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-red-200 bg-red-50 text-red-800'}`}>
              {banner.messages.map((m, i) => <div key={i}>{m}</div>)}
            </div>
          )}
          <ScmEditor scm={scm} selected={selected} onSelect={setSelected} apply={(ops) => apply(ops)} />
        </div>
      )}

      <details>
        <summary className="font-semibold text-sm cursor-pointer">Proměnné a mechanismy X = f(rodiče, šum) – tabulka</summary>
        <table className="data mt-1">
          <thead><tr><th>Proměnná</th><th>Grain</th><th>Sloupec</th><th>Mechanismus</th></tr></thead>
          <tbody>{scm.variables.map((x: Json) => (
            <tr key={x.variable_id}>
              <td className="mono">{x.variable_id}</td>
              <td className="mono text-xs">{x.entity}</td>
              <td className="text-xs">
                {x.column ? <span className="mono">{x.column}</span> : <span className="text-slate-400">—</span>}
                {x.observability === 'latent' && <span className="chip bg-slate-100 text-[10px] ml-1">latentní</span>}
              </td>
              <td className="mono text-[11px] text-slate-700">
                {v?.mechanisms?.[x.variable_id] ?? x.mechanism.type}
                {x.parents.some((p: Json) => p.lag > 0) && <span className="text-slate-500"> · lag {x.parents.filter((p: Json) => p.lag > 0).map((p: Json) => `${p.alias ?? p.variable}(t−${p.lag})`).join(', ')}</span>}
              </td>
            </tr>))}</tbody>
        </table>
      </details>

      {scm.events.length > 0 && (
        <div>
          <h3 className="font-semibold text-sm mb-1">Události a intervence</h3>
          <ul className="text-sm space-y-1">{scm.events.map((e: Json) => (
            <li key={e.event_id}>
              <span className={`chip text-[10px] mr-2 ${e.kind === 'story' ? 'bg-blue-50 text-blue-800' : 'bg-slate-100'}`}>{e.kind}</span>
              {e.description || e.event_id}
              <div className="text-xs text-slate-500 mono">
                {e.intervention_type} {e.affected_variables.join(', ')}{e.affected_entities && ` kde ${e.affected_entities}`}
                {' '}od {e.start_time ?? 'začátku'}{e.ramp_profile !== 'step' && ` (náběh ${e.ramp_profile}, ${e.ramp_periods} kroků)`}
              </div>
            </li>))}</ul>
        </div>
      )}

      {scm.ground_truth?.story_facts?.length > 0 && (
        <details className="text-sm"><summary className="cursor-pointer font-semibold">Příběhové skutečnosti pro evaluaci ({scm.ground_truth.story_facts.length})</summary>
          <ul className="mt-1 space-y-1">{scm.ground_truth.story_facts.map((f: Json) => (
            <li key={f.fact_id}><span className="chip bg-slate-100 text-[10px] mr-2">{f.kind} · váha {f.importance}</span>{f.description}</li>))}</ul>
        </details>
      )}
      {v?.edges && (
        <details className="text-xs"><summary className="cursor-pointer font-medium text-slate-700">Kauzální hrany ({v.edges.length}) – odvozené z rodičů mechanismů</summary>
          <div className="mono grid md:grid-cols-2 gap-x-6 mt-2">{v.edges.map((e: Json) => (
            <div key={`${e.source}>${e.target}`}>{e.source} → {e.target}{e.lag ? ` (lag ${e.lag})` : ''}{e.aggregation ? ` [${e.aggregation}]` : ''}{e.optional_coefficient != null ? ` · ${e.optional_coefficient}` : ''}</div>))}</div>
        </details>
      )}
      <p className="text-xs text-slate-500">
        Parametry modelu lze měnit přes <span className="mono">POST /api/scm/edit</span> nebo zpětnou vazbou pro přeplánování;
        tabulky níže jsou z modelu odvozené.
      </p>
    </div>
  )
}
