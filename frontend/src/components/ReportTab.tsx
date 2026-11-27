import { useState } from 'react'
import { api, fmt, type Json } from '../api'
import Chart from './Chart'
import { VerdictChip } from '../pages/Home'

export function EvidenceCard({ e, onSql }: { e: Json; onSql?: (sql: string) => void }) {
  return (
    <div className="border border-slate-200 rounded-lg p-3 bg-white">
      <div className="flex items-start gap-2">
        <div className="flex-1">
          <div className="font-medium text-sm">{e.title}</div>
          {e.expected && <div className="text-xs text-slate-500">Očekávání: {e.expected}</div>}
        </div>
        {!e.ok && <span className="chip bg-red-50 text-red-700">dotaz selhal</span>}
      </div>
      {e.ok ? <Chart spec={e} height={e.chart === 'bar' ? 260 : 240} /> : <div className="text-xs text-red-700 mono">{e.error}</div>}
      <details className="mt-1">
        <summary className="text-xs text-slate-500 cursor-pointer">SQL ({e.rows?.length ?? 0} řádků{e.elapsed_ms !== undefined ? `, ${e.elapsed_ms} ms` : ''})</summary>
        <pre className="text-[11px] bg-slate-50 rounded p-2 whitespace-pre-wrap mono">{e.sql ?? e.sql_template}</pre>
        {onSql && <button className="btn text-xs" onClick={() => onSql(e.sql ?? e.sql_template)}>Otevřít v SQL konzoli</button>}
      </details>
    </div>
  )
}

export default function ReportTab({ report, onSql }: { report: Json; onSql: (sql: string) => void }) {
  const [comment, setComment] = useState<Json>(null)
  const [busy, setBusy] = useState(false)
  const r = report.run
  const q = report.quality.summary ?? {}
  const v = report.verdict ?? {}
  const totalRows = Object.values((r.row_counts ?? {}) as Record<string, number>).reduce((a, b) => a + b, 0)
  const failed = (report.quality.checks as Json[]).filter((c) => !c.passed)

  const askAI = async () => {
    setBusy(true)
    try { setComment(await api.commentary(r.run_id)) } catch (e) { setComment({ headline: 'Chyba', paragraphs: [String(e)] }) }
    setBusy(false)
  }

  return (
    <div className="space-y-6">
      <div className="grid md:grid-cols-4 gap-4">
        <div className="card p-4 md:col-span-2">
          <div className="text-xs text-slate-500 mb-1">Scénář</div>
          <div className="font-semibold">{report.meta.name}</div>
          <div className="text-sm text-slate-600 mt-1">{report.story.logline}</div>
          <div className="text-xs text-slate-500 mt-2">{report.meta.period_start} – {report.meta.period_end} · seed {report.meta.seed} · {report.meta.archetype ?? 'llm_rows'}</div>
        </div>
        <div className="card p-4">
          <div className="text-xs text-slate-500 mb-1">Kvalita dat</div>
          <div className="text-2xl font-semibold tabular-nums">{q.passed}/{q.total}</div>
          <div className="text-xs text-slate-600">kontrol prošlo · FK integrita <b>{q.fk_integrity_pct} %</b></div>
          <div className="text-xs text-slate-600">{fmt(totalRows)} řádků v {Object.keys(r.row_counts ?? {}).length} tabulkách</div>
        </div>
        <div className="card p-4">
          <div className="text-xs text-slate-500 mb-1">Verdikt validace ({r.validator === 'strong' ? 'STRONG model' : 'deterministický'})</div>
          <VerdictChip v={v.verdict} />
          <div className="text-xs text-slate-600 mt-1">{v.summary}</div>
        </div>
      </div>

      <div className="card p-4">
        <div className="text-xs text-slate-500 mb-2">Hodnoty knobů tohoto běhu</div>
        <div className="flex flex-wrap gap-2">
          {report.knobs.map((k: Json) => (
            <span key={k.name} className="chip bg-slate-100 border border-slate-200" title={k.description}>{k.label || k.name}: <b className="ml-1">{fmt(k.value)}</b></span>
          ))}
        </div>
      </div>

      <section className="space-y-4">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-bold flex-1">3 · Rekonstrukce příběhu z dat – krok za krokem</h2>
          <button className="btn" disabled={busy} onClick={askAI}>{busy ? 'Generuji…' : '✦ AI komentář'}</button>
        </div>
        {comment && (
          <div className="card p-4 bg-blue-50/40 border-blue-200">
            <div className="font-semibold mb-1">{comment.headline}</div>
            {comment.paragraphs.map((p: string, i: number) => <p key={i} className="text-sm text-slate-700 mb-1">{p}</p>)}
          </div>
        )}
        {report.steps.map((s: Json, i: number) => {
          const det = s.detectability
          const sig = s.signal
          const ok = det?.detectable ?? sig?.detectable
          return (
            <div key={s.step.id} className="card p-4">
              <div className="flex items-start gap-3 mb-3">
                <div className="w-8 h-8 rounded-full bg-blue-600 text-white grid place-items-center font-bold shrink-0">{i + 1}</div>
                <div className="flex-1">
                  <div className="font-semibold">{s.step.mechanism}</div>
                  <div className="text-xs text-slate-500">Tabulky: {s.step.tables.join(', ') || '–'} · start: {s.step.start} · očekávaný dopad: {s.step.expected_impact}</div>
                </div>
                {ok !== undefined && (
                  <span className={`chip border ${ok ? 'bg-emerald-50 text-emerald-800 border-emerald-200' : 'bg-slate-50 text-slate-600 border-slate-200'}`}>
                    {ok ? '✓ dohledatelné' : '✖ nedohledatelné'}{det?.confidence !== undefined && ` · ${Math.round(det.confidence * 100)} %`}
                  </span>
                )}
              </div>
              {(sig?.comment || det?.comment) && (
                <div className="text-sm bg-slate-50 border border-slate-200 rounded p-2 mb-3">
                  {sig?.comment}{det?.comment && det.comment !== sig?.comment && <div className="text-xs text-slate-600 mt-1">Validátor: {det.comment}</div>}
                </div>
              )}
              <div className="grid lg:grid-cols-2 gap-3">
                {s.evidence.map((e: Json) => <EvidenceCard key={e.id} e={e} onSql={onSql} />)}
              </div>
            </div>
          )
        })}
        {report.other_evidence.length > 0 && (
          <div className="card p-4"><h3 className="font-semibold mb-2">Další důkazy</h3>
            <div className="grid lg:grid-cols-2 gap-3">{report.other_evidence.map((e: Json) => <EvidenceCard key={e.id} e={e} onSql={onSql} />)}</div>
          </div>
        )}
      </section>

      <section className="card p-4">
        <h3 className="font-semibold mb-2">Šum a distraktory zahrnuté v datech</h3>
        <div className="grid md:grid-cols-3 gap-2">
          {report.story.distractors.map((d: Json) => (
            <div key={d.id} className="border border-slate-200 rounded p-2 text-sm"><b>{d.name}</b> <span className="text-xs text-slate-500">({d.kind})</span><div className="text-xs text-slate-600">{d.description}</div></div>
          ))}
        </div>
      </section>

      <section className="card p-4">
        <h3 className="font-semibold mb-2">Deterministické kontroly ({q.passed}/{q.total})</h3>
        {failed.length > 0 && <div className="mb-2 text-sm text-red-700">Selhalo: {failed.map((c) => c.id).join(', ')}</div>}
        <details>
          <summary className="text-sm cursor-pointer text-slate-600">Zobrazit všechny kontroly</summary>
          <table className="data mt-2">
            <thead><tr><th></th><th>Kontrola</th><th>Typ</th><th>Porušení</th></tr></thead>
            <tbody>{(report.quality.checks as Json[]).map((c) => (
              <tr key={c.id}><td>{c.passed ? '✓' : '✖'}</td><td>{c.description}<div className="mono text-[10px] text-slate-400">{c.id}</div></td><td className="text-xs">{c.kind}</td><td className="tabular-nums">{c.violations}{c.error && <span className="text-red-700 text-xs"> {c.error}</span>}</td></tr>
            ))}</tbody>
          </table>
        </details>
        {(v.issues?.length ?? 0) > 0 && (
          <div className="mt-3"><h4 className="text-sm font-semibold">Problémy z validace</h4>
            <ul className="text-sm list-disc ml-5">{v.issues.map((i: Json) => <li key={i.id}><b>{i.severity}</b>: {i.description} {i.suggested_fix && <span className="text-slate-500">→ {i.suggested_fix}</span>}</li>)}</ul>
          </div>
        )}
        {r.repair_history?.length > 1 && (
          <div className="mt-3 text-sm"><h4 className="font-semibold">Repair smyčka</h4>
            {r.repair_history.map((h: Json) => <div key={h.iteration}>Iterace {h.iteration}: verdikt {h.verdict}, chyb {h.errors} {h.actions.map((a: Json) => `· ${a.action} ${a.table ?? a.param}${a.value !== undefined ? '=' + a.value : ''}`).join(' ')}</div>)}
          </div>
        )}
      </section>
    </div>
  )
}
