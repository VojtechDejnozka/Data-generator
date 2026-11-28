// "Soubory z dat": turn rows of a generated table into documents (orders -> invoices, deliveries -> delivery notes…).
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, fmt, type Json } from '../api'
import JobProgress from './JobProgress'
import { VerdictChip } from '../pages/Home'

const FORMATS: [string, string][] = [['pdf', 'PDF'], ['html', 'HTML'], ['md', 'Markdown'], ['txt', 'Text'], ['json', 'JSON'], ['eml', 'E-mail (.eml)']]

export default function FilesFromData({ runId, initialTable }: { runId: string; initialTable?: string }) {
  const [sources, setSources] = useState<Json[] | null>(null)
  const [table, setTable] = useState(initialTable ?? '')
  const [description, setDescription] = useState('')
  const [count, setCount] = useState(10)
  const [format, setFormat] = useState('pdf')
  const [filterCol, setFilterCol] = useState('')
  const [filterVal, setFilterVal] = useState('')
  const [existing, setExisting] = useState<Json[]>([])
  const [job, setJob] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const nav = useNavigate()

  useEffect(() => {
    api.docSources(runId).then((s) => {
      setSources(s)
      const pick = s.find((x: Json) => x.table === initialTable) ?? s[0]
      if (pick) { setTable(pick.table); setDescription(pick.suggestion) }
    })
    api.runs().then((r) => setExisting(r.filter((x) => x.kind === 'documents' && x.source_run_id === runId)))
  }, [runId, initialTable])

  const src = sources?.find((s) => s.table === table)
  const choose = (s: Json) => { setTable(s.table); setDescription(s.suggestion); setFilterCol(''); setFilterVal('') }

  const submit = async () => {
    setErr(null)
    try {
      const r = await api.docsPlan({ description, count, format, seed: 42, source_run_id: runId, source_table: table,
        source_filter_col: filterCol || null, source_filter_val: filterCol ? filterVal : null })
      setJob(r.job_id)
    } catch (e) { setErr(String(e)) }
  }

  if (job) {
    return <JobProgress jobId={job} title="STRONG model navrhuje dokumenty z dat" phases={[['plan', 'Návrh'], ['done', 'Hotovo']]}
      onDone={(r) => nav(`/docs/plan/${r.session_id}`)} />
  }
  if (!sources) return <div className="text-slate-500">Načítám tabulky…</div>

  return (
    <div className="grid lg:grid-cols-5 gap-4">
      <div className="lg:col-span-2 card p-3 self-start">
        <div className="font-semibold text-sm mb-2">Z jaké tabulky (1 řádek = 1 soubor)</div>
        {sources.map((s) => (
          <button key={s.table} onClick={() => choose(s)}
            className={`w-full text-left px-3 py-2 rounded-lg mb-1 cursor-pointer border ${s.table === table ? 'border-blue-400 bg-blue-50' : 'border-transparent hover:bg-slate-50'}`}>
            <div className="flex items-center gap-2"><span className="mono text-sm font-medium">{s.table}</span>
              <span className="text-xs text-slate-500">{fmt(s.rows)} řádků</span>
              {s.items_from && <span className="chip bg-teal-50 text-teal-800 text-[10px]">+ položky z {s.items_from}</span>}</div>
            <div className="text-xs text-slate-600">{s.suggestion}</div>
          </button>
        ))}
      </div>
      <div className="lg:col-span-3 space-y-4">
        <div className="card p-5 space-y-3">
          <h3 className="font-semibold">Vygenerovat soubory z tabulky <span className="mono">{table}</span></h3>
          {src && (
            <div className="text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded-lg p-2 space-y-0.5">
              <div>Každý soubor dostane data svého řádku{src.joins.length > 0 && <> a připojené údaje přes vazby: <b>{src.joins.join(', ')}</b></>}.</div>
              {src.items_from && <div>Položky dokladu se vezmou z tabulky <b>{src.items_from}</b>; částky, DPH a součty se <b>počítají deterministicky z dat</b> (LLM je nepočítá) a tabulka položek se vykreslí přímo z databáze.</div>}
            </div>
          )}
          <div><label className="label">Jaké soubory</label>
            <textarea className="input h-20" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
          <div className="grid md:grid-cols-4 gap-3">
            <div><label className="label">Počet souborů</label><input type="number" min={1} max={500} className="input" value={count} onChange={(e) => setCount(Number(e.target.value))} /></div>
            <div><label className="label">Formát</label><select className="input" value={format} onChange={(e) => setFormat(e.target.value)}>{FORMATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></div>
            <div><label className="label">Jen řádky kde (volitelné)</label>
              <select className="input" value={filterCol} onChange={(e) => setFilterCol(e.target.value)}>
                <option value="">— všechny řádky —</option>{src?.columns.map((c: string) => <option key={c}>{c}</option>)}</select></div>
            <div><label className="label">= hodnota</label><input className="input" disabled={!filterCol} value={filterVal} onChange={(e) => setFilterVal(e.target.value)} placeholder="např. 2" /></div>
          </div>
          <p className="text-xs text-slate-500">Řádky se vyberou náhodně podle seedu (reprodukovatelně). Příběh v datech se do souborů přenese sám – např. faktury problémového dodavatele budou mít pozdější data dodání.</p>
          {err && <div className="text-sm text-red-700">{err}</div>}
          <button className="btn btn-primary" disabled={!table || description.trim().length < 10} onClick={submit}>Navrhnout soubory (AI) →</button>
        </div>
        {existing.length > 0 && (
          <div className="card p-4">
            <div className="font-semibold text-sm mb-2">Soubory už vygenerované z tohoto běhu</div>
            <table className="data"><tbody>{existing.map((r) => (
              <tr key={r.run_id}><td className="mono text-xs">{r.run_id}</td><td>{r.name}</td><td className="text-xs">{r.source_table}</td>
                <td className="uppercase text-xs">{r.format}</td><td>{r.row_counts?.documents}</td><td><VerdictChip v={r.verdict?.verdict} /></td>
                <td><Link className="btn text-xs" to={`/docs/runs/${r.run_id}`}>Otevřít</Link></td></tr>))}</tbody></table>
          </div>
        )}
      </div>
    </div>
  )
}
