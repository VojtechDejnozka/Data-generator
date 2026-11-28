// Dashboard, SQL console, what-if, detective, ground truth and log tabs.
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, fmt, watchJob, type Json, type JobEvent, type QueryResult } from '../api'
import Chart, { ResultTable } from './Chart'
import JobProgress from './JobProgress'

export function DashboardTab({ runId }: { runId: string }) {
  const [d, setD] = useState<Json>(null)
  useEffect(() => { api.dashboard(runId).then(setD) }, [runId])
  if (!d) return <div className="text-slate-500">Načítám…</div>
  const kpis = d.widgets.filter((w: Json) => w.chart === 'kpi')
  const rest = d.widgets.filter((w: Json) => w.chart !== 'kpi')
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm">
        <h2 className="text-lg font-bold flex-1">{d.meta.title ?? 'Dashboard'}</h2>
        <span className="chip bg-slate-100">zdroj: {d.meta.source === 'spec' ? 'návrh STRONG modelu (validováno)' : 'výchozí z důkazních dotazů'}</span>
      </div>
      {d.meta.errors?.length > 0 && <div className="text-xs text-amber-800">Vyřazené widgety: {d.meta.errors.join('; ')}</div>}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {kpis.map((w: Json) => <div key={w.id} className="card p-4"><div className="text-xs text-slate-500">{w.title}</div><Chart spec={w} /></div>)}
      </div>
      <div className="grid lg:grid-cols-2 gap-4">
        {rest.map((w: Json) => (
          <div key={w.id} className={`card p-4 ${w.width === 2 ? 'lg:col-span-2' : ''}`}>
            <div className="font-medium text-sm mb-1">{w.title}</div>
            {w.description && <div className="text-xs text-slate-500">{w.description}</div>}
            <Chart spec={w} height={w.chart === 'heatmap' ? 320 : 280} />
          </div>
        ))}
      </div>
    </div>
  )
}

export function SqlTab({ runId, initial }: { runId: string; initial: string }) {
  const [sql, setSql] = useState(initial || 'SELECT category, COUNT(*) AS n FROM complaints GROUP BY 1 ORDER BY 2 DESC')
  const [res, setRes] = useState<QueryResult | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [chart, setChart] = useState('')
  useEffect(() => { if (initial) setSql(initial) }, [initial])
  const run = async () => {
    setErr(null)
    try { setRes(await api.query(runId, sql)) } catch (e) { setErr(String(e)); setRes(null) }
  }
  return (
    <div className="card p-4 space-y-3">
      <div className="text-sm text-slate-600">Read-only SQL (SQLite). Povolen je jen jeden SELECT/WITH; běží s časovým limitem a limitem řádků. Placeholdery <code>{'{{knob:supplier_id}}'}</code> se doplní z knobů běhu.</div>
      <textarea className="input mono text-xs h-40" value={sql} onChange={(e) => setSql(e.target.value)} onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === 'Enter' && run()} />
      <div className="flex gap-2 items-center">
        <button className="btn btn-primary" onClick={run}>▶ Spustit (⌘↵)</button>
        <select className="input" style={{ width: 170 }} value={chart} onChange={(e) => setChart(e.target.value)}>
          <option value="">jen tabulka</option><option value="line">čárový graf</option><option value="bar">sloupcový graf</option>
        </select>
        {res && <span className="text-xs text-slate-500">{res.rows.length} řádků{res.truncated && ' (oříznuto)'} · {res.elapsed_ms} ms</span>}
      </div>
      {err && <div className="text-red-700 text-sm mono">{err}</div>}
      {res && chart && res.columns.length >= 2 && <Chart spec={{ ...res, chart, x: res.columns[0], y: res.columns.slice(1).filter((_, i) => typeof res.rows[0]?.[i + 1] === 'number') }} />}
      {res && <ResultTable columns={res.columns} rows={res.rows} max={500} />}
    </div>
  )
}

export function WhatIfTab({ run }: { run: Json }) {
  const knobs = run.spec.knobs as Json[]
  const [vals, setVals] = useState<Record<string, string>>(Object.fromEntries(knobs.map((k) => [k.name, String(run.knob_values[k.name])])))
  const [job, setJob] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const nav = useNavigate()
  const changed = Object.fromEntries(Object.entries(vals).filter(([k, v]) => String(run.knob_values[k]) !== v))
  const start = async (overrides: Record<string, string>, label = '') => {
    setErr(null)
    try { setJob((await api.whatif(run.run_id, overrides, label)).job_id) } catch (e) { setErr(String(e)) }
  }
  const sev = Number(run.knob_values.delay_severity ?? 6)
  const presets: [string, Record<string, string>][] = [
    ['Baseline bez příběhu (intenzita 0)', { story_intensity: '0' }],
    [`Silnější zpoždění (${sev} → ${Math.min(15, sev * 2)} dní)`, { delay_severity: String(Math.min(15, sev * 2)) }],
    ['Skokový náběh (step)', { ramp_profile: 'step' }],
    ['Jiný dodavatel (ID 5)', { supplier_id: '5' }],
  ]
  if (job) return <JobProgress jobId={job} title="What-if regenerace (stejný seed)" onDone={(r) => nav(`/compare?a=${run.run_id}&b=${r.run_id}`)} />
  return (
    <div className="grid lg:grid-cols-3 gap-4">
      <div className="card p-4 lg:col-span-2">
        <h3 className="font-semibold mb-1">4 · Změna parametrů scénáře</h3>
        <p className="text-sm text-slate-600 mb-3">Regenerace proběhne se stejným seedem a stejnými master daty. Poptávka (návštěvy a nákupní záměry) se vzorkuje nezávisle na zásobách, takže rozdíl mezi běhy vzniká jen změněným mechanismem.</p>
        <div className="grid md:grid-cols-2 gap-3">
          {knobs.map((k) => (
            <div key={k.name}>
              <label className="label">{k.label || k.name} {String(run.knob_values[k.name]) !== vals[k.name] && <span className="text-blue-700">• změněno</span>}</label>
              {k.type === 'enum' ? (
                <select className="input" value={vals[k.name]} onChange={(e) => setVals({ ...vals, [k.name]: e.target.value })}>{k.options.map((o: string) => <option key={o}>{o}</option>)}</select>
              ) : k.type === 'bool' ? (
                <select className="input" value={vals[k.name]} onChange={(e) => setVals({ ...vals, [k.name]: e.target.value })}><option value="true">ano</option><option value="false">ne</option></select>
              ) : (
                <input className="input" type={k.type === 'date' ? 'date' : 'number'} step={k.type === 'float' ? 0.1 : 1} min={k.min ?? undefined} max={k.max ?? undefined}
                  value={vals[k.name]} onChange={(e) => setVals({ ...vals, [k.name]: e.target.value })} />
              )}
            </div>
          ))}
        </div>
        {err && <div className="text-red-700 text-sm mt-2">{err}</div>}
        <button className="btn btn-primary mt-4" disabled={!Object.keys(changed).length} onClick={() => start(changed)}>Regenerovat a porovnat ({Object.keys(changed).length} změn)</button>
      </div>
      <div className="card p-4 space-y-2">
        <h3 className="font-semibold">Rychlé scénáře</h3>
        {presets.map(([label, o]) => <button key={label} className="btn w-full justify-start" onClick={() => start(o, label)}>{label}</button>)}
      </div>
    </div>
  )
}

export function DetectiveTab({ runId }: { runId: string }) {
  const [result, setResult] = useState<Json>(null)
  const [live, setLive] = useState<JobEvent[]>([])
  const [running, setRunning] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { api.detective(runId).then(setResult).catch(() => undefined) }, [runId])
  const start = async () => {
    setErr(null); setLive([]); setRunning(true); setResult(null)
    const { job_id } = await api.startDetective(runId)
    watchJob(job_id, (e) => {
      if (e.final) { setRunning(false); if (e.status === 'error') setErr(e.error ?? 'chyba'); else setResult(e.result); return }
      if (e.phase === 'detective') setLive((l) => [...l, e])
    })
  }
  const steps = result?.transcript ?? []
  return (
    <div className="space-y-4">
      <div className="card p-4 flex items-start gap-4">
        <div className="flex-1">
          <h3 className="font-semibold">Detektiv – slepý AI analytik</h3>
          <p className="text-sm text-slate-600">Agent dostane jen schéma databáze (bez popisů, specifikace, příběhu i knobů) a otázku „proč klesají tržby?“. Přes read-only SQL nástroj sám hledá příčinu. Výsledek se porovná se skrytým ground truth – silný důkaz, že příběh je v datech skutečně dohledatelný.</p>
        </div>
        <button className="btn btn-primary" disabled={running} onClick={start}>{running ? 'Vyšetřuje…' : result ? '↻ Spustit znovu' : '🔎 Spustit detektiva'}</button>
      </div>
      {err && <div className="text-red-700">{err}</div>}
      {running && live.map((e, i) => (
        <div key={i} className="card p-3 text-sm"><b>Krok {i + 1}:</b> {e.message?.replace(/^Krok \d+: /, '')}{e.sql && <pre className="mono text-[11px] bg-slate-50 p-2 mt-1 whitespace-pre-wrap">{e.sql}</pre>}</div>
      ))}
      {result && (
        <>
          <div className="card p-4 border-blue-200">
            <div className="flex items-center gap-3 mb-2">
              <h3 className="font-semibold flex-1">Závěr detektiva</h3>
              <span className={`chip border text-sm ${result.score.score >= 80 ? 'bg-emerald-50 text-emerald-800 border-emerald-200' : 'bg-amber-50 text-amber-800 border-amber-200'}`}>shoda s ground truth: {result.score.score} %</span>
            </div>
            <p className="text-sm">{result.final?.root_cause}</p>
            {result.final?.causal_chain?.length > 0 && <ol className="text-sm list-decimal ml-5 mt-2">{result.final.causal_chain.map((c: string) => <li key={c}>{c}</li>)}</ol>}
            {result.final?.ruled_out?.length > 0 && <div className="text-xs text-slate-600 mt-2">Vyloučeno: {result.final.ruled_out.join(' · ')}</div>}
            <table className="data mt-3"><thead><tr><th></th><th>Kritérium</th><th>Očekáváno</th><th>Nalezeno</th></tr></thead>
              <tbody>{result.score.checks.map((c: Json) => <tr key={c.name}><td>{c.ok ? '✓' : '✖'}</td><td>{c.name}</td><td className="text-xs">{c.expected ?? ''}</td><td className="text-xs">{c.got ?? ''}</td></tr>)}</tbody></table>
          </div>
          <div className="space-y-2">
            {steps.map((s: Json) => (
              <details key={s.step} className="card p-3" open={!!s.sql && s.step < 2}>
                <summary className="text-sm cursor-pointer"><b>Krok {s.step + 1}</b> – {s.thought}</summary>
                {s.sql && <pre className="mono text-[11px] bg-slate-50 p-2 mt-2 whitespace-pre-wrap">{s.sql}</pre>}
                {s.observation?.ok && <ResultTable columns={s.observation.columns} rows={s.observation.rows} max={40} />}
                {s.observation && !s.observation.ok && <div className="text-xs text-red-700">{s.observation.error}</div>}
              </details>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

export function GroundTruthTab({ runId }: { runId: string }) {
  const [gt, setGt] = useState<Json>(null)
  const [shown, setShown] = useState(false)
  const [error, setError] = useState(false)
  const reveal = () => {
    setShown(true)
    setError(false)
    api.groundTruth(runId).then(setGt).catch(() => setError(true))
  }
  if (!shown) {
    return (
      <div className="card p-8 text-center">
        <p className="text-slate-600 mb-3">Ground truth manifest obsahuje to, co generátor do dat skutečně „vložil“ (viník, data, závažnost, realizované dopady, distraktory). Je skrytý, aby šlo příběh nejdřív hledat v datech.</p>
        <button className="btn btn-primary" onClick={reveal}>👁 Odhalit ground truth</button>
      </div>
    )
  }
  if (error) return <div className="card p-4 text-sm" role="alert">
    Ground truth se nepodařilo načíst. <button className="btn ml-2" onClick={reveal}>Zkusit znovu</button>
  </div>
  if (!gt) return <div className="text-slate-500">Načítám…</div>
  if (gt.kind === 'scm_ground_truth') return <ScmGroundTruth gt={gt} />
  if (Object.keys(gt).length === 0) return <div className="card p-4 text-sm text-slate-600">Pro tento běh není ground truth k dispozici.</div>
  if (!gt.story || !Array.isArray(gt.causal_chain) || !Array.isArray(gt.monthly)) {
    return <pre className="card p-4 text-xs overflow-auto">{JSON.stringify(gt, null, 1)}</pre>
  }
  const monthly: Json[] = gt.monthly
  const cols = ['month', 'injected_delay_days', 'actual_delay_affected', 'actual_delay_others']
  return (
    <div className="space-y-4">
      <div className="card p-4">
        <h3 className="font-semibold mb-2">Vložený příběh</h3>
        <div className="grid md:grid-cols-3 gap-2 text-sm">
          {Object.entries(gt.story).filter(([k]) => k !== 'affected_product_ids').map(([k, v]) => <div key={k}><span className="text-slate-500 text-xs">{k}</span><div className="font-medium">{fmt(v)}</div></div>)}
        </div>
        <ol className="list-decimal ml-5 mt-3 text-sm">{gt.causal_chain.map((c: Json) => <li key={c.step}>{c.fact} <span className="text-xs text-slate-500">({(c.tables ?? []).join(', ')})</span></li>)}</ol>
      </div>
      <div className="grid lg:grid-cols-2 gap-4">
        <div className="card p-4"><div className="font-medium text-sm">Vložené vs. realizované zpoždění (měsíčně)</div>
          <Chart spec={{ columns: cols, rows: monthly.map((m) => cols.map((c) => m[c] ?? null)), chart: 'line', x: 'month', y: cols.slice(1) }} /></div>
        <div className="card p-4"><div className="font-medium text-sm">Skryté ztracené tržby (Kč) – nejsou v žádné tabulce</div>
          <Chart spec={{ columns: ['month', 'lost_revenue_czk', 'walkout_revenue_czk'], rows: monthly.map((m) => [m.month, m.lost_revenue_czk, m.walkout_revenue_czk]), chart: 'bar', x: 'month', y: ['lost_revenue_czk', 'walkout_revenue_czk'] }} /></div>
      </div>
      <div className="card p-4"><h3 className="font-semibold mb-2">Realizované dopady</h3>
        <div className="grid md:grid-cols-4 gap-2 text-sm">{Object.entries(gt.realized ?? {}).map(([k, v]) => <div key={k}><span className="text-xs text-slate-500">{k}</span><div className="font-medium tabular-nums">{fmt(v)}</div></div>)}</div></div>
      <div className="card p-4"><h3 className="font-semibold mb-2">Distraktory (konkrétní instance)</h3>
        <table className="data"><tbody>{(gt.distractors ?? []).map((d: Json) => <tr key={d.id}><td className="font-medium">{d.name}</td><td className="text-xs">{Object.entries(d).filter(([k]) => !['id', 'name'].includes(k)).map(([k, v]) => `${k}: ${v}`).join(' · ')}</td></tr>)}</tbody></table></div>
      <div className="card p-4"><h3 className="font-semibold mb-2">Měsíční ground truth</h3><ResultTable columns={Object.keys(monthly[0] ?? {})} rows={monthly.map((m) => Object.values(m))} /></div>
    </div>
  )
}

function ScmGroundTruth({ gt }: { gt: Json }) {
  const story = gt.story?.refined || gt.story?.original
  const facts: Json[] = gt.realized_story_facts ?? []
  const events: Json[] = gt.realized_events ?? []
  return <div className="space-y-4">
    <section className="card p-4 space-y-2">
      <h3 className="font-semibold">Fakta kauzálního modelu</h3>
      {story && <p className="text-sm whitespace-pre-wrap">{story}</p>}
      {facts.length > 0 ? <ResultTable columns={['Fakt', 'Projevil se v datech', 'Viditelné v proměnných']}
        rows={facts.map((f) => [f.description || f.fact_id, f.realized, f.visible_in?.join(', ') || null])} />
        : <p className="text-sm text-slate-600">V tomto modelu nejsou uvedena žádná příběhová fakta.</p>}
    </section>
    {events.length > 0 && <section className="card p-4 space-y-2">
      <h3 className="font-semibold">Události v datech</h3>
      <ResultTable columns={['Událost', 'Od', 'Do', 'Zasažené řádky podle proměnné', 'Projevila se']}
        rows={events.map((e) => [
          e.description || e.event_id, e.first_date ?? e.first_step, e.last_date ?? e.last_step,
          Object.entries(e.affected_rows ?? {}).map(([variable, count]) => `${variable}: ${fmt(count)}`).join(', ') || null,
          e.realized,
        ])} />
    </section>}
  </div>
}

export function LogTab({ run }: { run: Json }) {
  const [calls, setCalls] = useState<Json[] | null>(null)
  const u = run.usage ?? {}
  return (
    <div className="space-y-4">
      <div className="grid md:grid-cols-4 gap-4">
        <div className="card p-4"><div className="text-xs text-slate-500">LLM volání</div><div className="text-2xl font-semibold">{u.total_calls}</div></div>
        <div className="card p-4"><div className="text-xs text-slate-500">Tokeny</div><div className="text-2xl font-semibold tabular-nums">{fmt(u.total_tokens)}</div></div>
        <div className="card p-4"><div className="text-xs text-slate-500">Odhad nákladů</div><div className="text-2xl font-semibold">${u.total_cost_usd?.toFixed(4)}</div></div>
        <div className="card p-4"><div className="text-xs text-slate-500">Doba běhu</div><div className="text-2xl font-semibold">{run.duration_s} s</div></div>
      </div>
      <div className="card p-4"><h3 className="font-semibold mb-2">Spotřeba podle modelu</h3>
        <ResultTable columns={['role', 'model', 'calls', 'cached', 'failed', 'prompt_tokens', 'output_tokens', 'thoughts_tokens', 'cost_usd']} rows={(u.by_model ?? []).map((b: Json) => [b.role, b.model, b.calls, b.cached, b.failed, b.prompt_tokens, b.output_tokens, b.thoughts_tokens, b.cost_usd])} /></div>
      <div className="card p-4"><h3 className="font-semibold mb-2">GenerationPlan (STRONG)</h3>
        <table className="data"><thead><tr><th>Balíček</th><th>Strategie</th><th>Tabulky</th><th>Závislosti</th><th>Instrukce</th></tr></thead>
          <tbody>{run.plan?.work_packages.map((p: Json) => <tr key={p.id}><td className="mono">{p.id}</td><td>{p.strategy}{p.module && ` (${p.module})`}</td><td className="text-xs">{p.tables.join(', ')}</td><td className="text-xs">{p.depends_on.join(', ')}</td><td className="text-xs">{p.instructions}</td></tr>)}</tbody></table>
        {run.plan?.notes && <div className="text-xs text-slate-500 mt-1">{run.plan.notes}</div>}</div>
      {(run.warnings?.length ?? 0) > 0 && <div className="card p-4"><h3 className="font-semibold mb-2">Varování</h3><ul className="text-sm list-disc ml-5">{run.warnings.map((w: string) => <li key={w}>{w}</li>)}</ul></div>}
      <div className="card p-4"><h3 className="font-semibold mb-2">Interní parametry simulátoru</h3><pre className="text-xs mono">{JSON.stringify(run.params, null, 1)}</pre></div>
      <div className="card p-4">
        <div className="flex items-center"><h3 className="font-semibold flex-1">Jednotlivá LLM volání</h3>{!calls && <button className="btn text-xs" onClick={() => api.llmCalls(run.run_id).then(setCalls)}>Načíst</button>}</div>
        {calls && <ResultTable columns={['role', 'task', 'model', 'cached', 'ok', 'attempts', 'prompt_tokens', 'output_tokens', 'cost_usd', 'latency_s', 'error']} rows={calls.map((c) => [c.role, c.task, c.model, c.cached, c.ok, c.attempts, c.prompt_tokens, c.output_tokens, c.cost_usd, c.latency_s, c.error])} max={500} />}
      </div>
      <div className="text-xs text-slate-500">Odpovědi LLM jsou uloženy v <code>runs/{run.run_id}/llm_calls.jsonl</code> a v cache podle hashe vstupu – běh lze přehrát bez nových volání. <Link className="text-blue-700" to="/">← běhy</Link></div>
    </div>
  )
}
