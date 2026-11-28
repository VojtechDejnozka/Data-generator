import { Fragment, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, fmt, type Json } from '../api'

export function VerdictChip({ v }: { v?: string }) {
  if (!v) return <span className="text-xs text-slate-400">bez verdiktu</span>
  const cls = v === 'pass' ? 'text-emerald-800' : v === 'warn' ? 'text-amber-800' : 'text-red-700'
  const icon = v === 'pass' ? '✓' : v === 'warn' ? '!' : '✕'
  const text = v === 'pass' ? 'prošlo' : v === 'warn' ? 'varování' : 'selhalo'
  return <span className={`inline-flex items-center gap-1 text-xs font-medium whitespace-nowrap ${cls}`}><span aria-hidden="true">{icon}</span>{text}</span>
}

// The demo story. Plain text is context; marked phrases are the causal chain hidden in the data.
const STORY: [string, boolean][] = [
  ['Tržby prodejen klesají, i když poptávka zůstává stejná. ', false],
  ['Jeden dodavatel dodává čím dál později', true], [', ', false],
  ['v regálech chybí zboží', true], [', ', false],
  ['zákazníci odcházejí bez nákupu', true], [' a ', false],
  ['přibývá stížností na nedostupnost', true], ['.', false],
]

function Hero() {
  let k = 0
  return (
    <section className="pt-10 pb-16 md:pt-16 md:pb-24">
      <p className="max-w-[30ch] text-[28px] md:text-[42px] leading-[1.2] tracking-[-0.018em] font-medium">
        {STORY.map(([t, marked], i) => marked
          ? <span key={i} className="mark mark-sweep" style={{ animationDelay: `${300 + k++ * 420}ms` }}>{t}</span>
          : <Fragment key={i}>{t}</Fragment>)}
      </p>
      <div className="mt-10 flex flex-wrap items-center gap-x-6 gap-y-3 text-[15px] text-slate-600">
        <p className="max-w-[46ch]">
          Napište příběh a Story2Data z něj vytvoří propojený dataset, ve kterém se dá dohledat krok za krokem.
        </p>
        <Link to="/new" className="btn btn-primary px-4 py-2 text-[15px]">Nový scénář</Link>
      </div>
    </section>
  )
}

const KNOBS = ['supplier_id', 'delay_severity', 'story_intensity', 'delay_start_date']

export default function Home() {
  const [runs, setRuns] = useState<Json[] | null>(null)
  const [sel, setSel] = useState<string[]>([])
  const nav = useNavigate()
  useEffect(() => { api.runs().then((r) => setRuns(r.filter((x) => x.kind !== 'documents'))).catch(() => setRuns([])) }, [])
  const toggle = (id: string) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id].slice(-2)))

  return (
    <div>
      <Hero />

      <section aria-labelledby="runs">
        <div className="flex flex-wrap items-baseline justify-between gap-3 mb-3">
          <h2 id="runs" className="text-[21px] font-semibold tracking-[-0.01em]">
            Vygenerované běhy {runs && runs.length > 0 && <span className="text-slate-400 font-normal tabular-nums">{runs.length}</span>}
          </h2>
          <button className="btn" disabled={sel.length !== 2} onClick={() => nav(`/compare?a=${sel[0]}&b=${sel[1]}`)}>
            {sel.length === 2 ? 'Porovnat vybrané' : `Vyberte dva běhy k porovnání (${sel.length}/2)`}
          </button>
        </div>
        {runs === null ? <p className="py-6 text-sm text-slate-500">Načítám běhy…</p> : runs.length === 0 ? (
          <p className="py-6 text-sm text-slate-500 border-t border-line">
            Zatím tu nic není. <Link to="/new" className="text-ink underline underline-offset-2">Založte první scénář</Link>, třeba z připraveného presetu.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="data">
              <thead><tr>
                <th className="w-8"><span className="sr-only">Vybrat</span></th>
                <th>Scénář</th><th>Vytvořeno</th><th>Nastavení</th><th className="text-right">Řádků</th><th>Verdikt</th>
              </tr></thead>
              <tbody>
                {runs.map((r) => {
                  const picked = sel.includes(r.run_id)
                  return (
                    <tr key={r.run_id} className={picked ? 'bg-mark-soft' : 'hover:bg-slate-50'}>
                      <td className="pt-2.5"><input type="checkbox" aria-label={`Vybrat ${r.name}`} checked={picked} onChange={() => toggle(r.run_id)} /></td>
                      <td className="py-2">
                        <Link to={`/runs/${r.run_id}`} className="font-medium hover:underline underline-offset-2">{r.name}</Link>
                        {r.mock && <span className="ml-2 text-xs text-amber-800">mock</span>}
                        {r.is_preset && <span className="ml-2 text-xs text-slate-500" title="Sdílený ukázkový běh, jen pro čtení">ukázka</span>}
                        {r.shared && <span className="ml-2 text-xs text-slate-500" title="Má veřejný odkaz pro čtení">sdíleno</span>}
                        <div className="text-xs text-slate-500">
                          {r.label}{r.parent_run_id && <span title={r.parent_run_id}>, what-if z …{r.parent_run_id.slice(-6)}</span>}
                          <span className="mono text-slate-400 ml-2">{r.run_id}</span>
                        </div>
                      </td>
                      <td className="py-2 text-xs text-slate-600 whitespace-nowrap tabular-nums">{r.created_at ? new Date(r.created_at).toLocaleString('cs-CZ', { dateStyle: 'short', timeStyle: 'short' }) : '–'}</td>
                      <td className="py-2 text-xs text-slate-600">
                        {r.knob_values && KNOBS.filter((k) => k in r.knob_values).map((k) =>
                          <div key={k}><span className="text-slate-400">{k}</span> {fmt(r.knob_values[k])}</div>)}
                      </td>
                      <td className="py-2 text-right tabular-nums">{r.row_counts ? fmt(Object.values(r.row_counts as Record<string, number>).reduce((a, b) => a + b, 0)) : '–'}</td>
                      <td className="py-2">{r.status === 'running' ? <span className="text-xs font-medium mark">běží</span> : <VerdictChip v={r.verdict?.verdict} />}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
