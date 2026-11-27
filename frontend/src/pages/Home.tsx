import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, fmt, type Json } from '../api'

export function VerdictChip({ v }: { v?: string }) {
  if (!v) return <span className="chip bg-slate-100 text-slate-600">–</span>
  const cls = v === 'pass' ? 'bg-emerald-50 text-emerald-800 border-emerald-200'
    : v === 'warn' ? 'bg-amber-50 text-amber-800 border-amber-200' : 'bg-red-50 text-red-800 border-red-200'
  const icon = v === 'pass' ? '✓' : v === 'warn' ? '!' : '✖'
  const text = v === 'pass' ? 'prošlo' : v === 'warn' ? 'varování' : 'selhalo'
  return <span className={`chip border ${cls}`}>{icon} {text}</span>
}

export default function Home() {
  const [runs, setRuns] = useState<Json[] | null>(null)
  const [sel, setSel] = useState<string[]>([])
  const nav = useNavigate()
  useEffect(() => { api.runs().then(setRuns).catch(() => setRuns([])) }, [])
  const toggle = (id: string) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id].slice(-2)))

  return (
    <div className="space-y-6">
      <section className="card p-6 flex flex-col md:flex-row gap-6 items-start md:items-center">
        <div className="flex-1">
          <h1 className="text-2xl font-bold mb-1">Generátor propojených datasetů z příběhu</h1>
          <p className="text-slate-600 text-sm max-w-3xl">
            Popište firmu, prostředí a příběh. STRONG model navrhne datový model, kauzální řetězec, distraktory a
            důkazní dotazy; po vašem schválení vznikne propojený dataset, ve kterém je příběh dohledatelný
            v časových a věcných souvislostech – ověřený deterministickými kontrolami, validací a „detektivem“.
          </p>
        </div>
        <Link to="/new" className="btn btn-primary text-base px-5 py-2.5">+ Nový scénář</Link>
      </section>

      <section className="card">
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200">
          <h2 className="font-semibold">Vygenerované běhy</h2>
          <button className="btn" disabled={sel.length !== 2} onClick={() => nav(`/compare?a=${sel[0]}&b=${sel[1]}`)}>
            Porovnat vybrané ({sel.length}/2)
          </button>
        </div>
        {runs === null ? <div className="p-5 text-sm text-slate-500">Načítám…</div> : runs.length === 0 ? (
          <div className="p-5 text-sm text-slate-500">Zatím žádné běhy. Začněte novým scénářem (např. předpřipraveným presetem).</div>
        ) : (
          <table className="data">
            <thead><tr><th></th><th>Běh</th><th>Název</th><th>Vytvořeno</th><th>Klíčové knoby</th><th>Řádků</th><th>Verdikt</th><th></th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run_id} className="hover:bg-slate-50">
                  <td><input type="checkbox" checked={sel.includes(r.run_id)} onChange={() => toggle(r.run_id)} /></td>
                  <td className="mono text-xs">{r.run_id}<div className="text-slate-500 font-sans">{r.label}{r.parent_run_id && ' ↳ ' + r.parent_run_id.slice(-6)}</div></td>
                  <td>{r.name}{r.mock && <span className="chip bg-amber-50 text-amber-800 ml-2">mock</span>}</td>
                  <td className="text-xs whitespace-nowrap">{r.created_at?.replace('T', ' ')}</td>
                  <td className="text-xs">
                    {r.knob_values && ['supplier_id', 'delay_severity', 'story_intensity', 'delay_start_date']
                      .filter((k) => k in r.knob_values).map((k) => <div key={k}><span className="text-slate-500">{k}</span> = {fmt(r.knob_values[k])}</div>)}
                  </td>
                  <td className="tabular-nums">{r.row_counts ? fmt(Object.values(r.row_counts as Record<string, number>).reduce((a, b) => a + b, 0)) : '–'}</td>
                  <td>{r.status === 'running' ? <span className="chip bg-blue-50 text-blue-700">běží</span> : <VerdictChip v={r.verdict?.verdict} />}</td>
                  <td><Link className="btn" to={`/runs/${r.run_id}`}>Otevřít</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
