import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, type Json } from '../api'
import JobProgress from '../components/JobProgress'
import { VerdictChip } from './Home'

const FORMATS: [string, string][] = [
  ['pdf', 'PDF'], ['html', 'HTML'], ['md', 'Markdown'], ['txt', 'Text'], ['json', 'JSON'], ['eml', 'E-mail (.eml)'],
]
const EXAMPLES = [
  'Propouštěcí zprávy pacientů interního oddělení krajské nemocnice. U části pacientů léčených novým lékem se objevují zvýšené jaterní testy.',
  'Pojistné události z havarijního pojištění – hlášení škody od klientů a posudek likvidátora; část hlášení vykazuje znaky podvodu.',
  'Měsíční reporty vedoucích prodejen pro centrálu – tržby, personál, incidenty a návrhy zlepšení.',
]

export default function DocsHome() {
  const [description, setDescription] = useState('')
  const [count, setCount] = useState(20)
  const [format, setFormat] = useState('pdf')
  const [seed, setSeed] = useState(42)
  const [useSource, setUseSource] = useState(false)
  const [sourceRun, setSourceRun] = useState('')
  const [sourceTable, setSourceTable] = useState('')
  const [schema, setSchema] = useState<Json>(null)
  const [runs, setRuns] = useState<Json[]>([])
  const [job, setJob] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const nav = useNavigate()

  useEffect(() => { api.runs().then(setRuns).catch(() => setRuns([])) }, [])
  useEffect(() => {
    setSchema(null); setSourceTable('')
    if (sourceRun) api.schema(sourceRun).then(setSchema).catch(() => undefined)
  }, [sourceRun])
  const dataRuns = runs.filter((r) => r.kind !== 'documents' && r.status === 'done')
  const docRuns = runs.filter((r) => r.kind === 'documents')

  const submit = async () => {
    setErr(null)
    if (description.trim().length < 10) { setErr('Popište, jaké dokumenty chcete vygenerovat.'); return }
    if (useSource && (!sourceRun || !sourceTable)) { setErr('Vyberte běh a tabulku, ze které se vezmou fakta.'); return }
    try {
      const r = await api.docsPlan({ description, count, format, seed,
        source_run_id: useSource ? sourceRun : null, source_table: useSource ? sourceTable : null })
      setJob(r.job_id)
    } catch (e) { setErr(String(e)) }
  }

  if (job) {
    return (
      <div className="max-w-3xl space-y-4">
        <JobProgress jobId={job} title="STRONG model navrhuje specifikaci dokumentů" phases={[['plan', 'Návrh'], ['done', 'Hotovo']]}
          onDone={(r) => nav(`/docs/plan/${r.session_id}`)} />
        <button className="btn" onClick={() => setJob(null)}>Zpět</button>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="card p-6 space-y-4">
        <div>
          <h1 className="text-[28px] leading-tight font-semibold tracking-[-0.015em]">Generátor dokumentů</h1>
          <p className="text-sm text-slate-600 max-w-4xl">
            Libovolné nestrukturované výstupy – lékařské zprávy, dopisy, e-maily, posudky, reporty… Stejná pipeline jako u dat:
            Gemini navrhne <b>fakta</b> (strukturovaný záznam ke každému dokumentu), <b>sekce</b> a volitelně skrytý <b>vzor</b>,
            CHEAP model napíše texty z faktů, deterministické kontroly ověří, že text fakta skutečně obsahuje, a STRONG model výsledek validuje.
          </p>
        </div>
        <div>
          <label className="label">Co chcete vygenerovat</label>
          <textarea className="input h-32" value={description} onChange={(e) => setDescription(e.target.value)}
            placeholder="Např. propouštěcí zprávy pacientů… / reklamační dopisy zákazníků… / hlášení pojistných událostí…" />
          <div className="flex flex-wrap gap-2 mt-2">
            {EXAMPLES.map((x) => <button key={x} className="chip bg-slate-100 hover:bg-slate-200 cursor-pointer text-left" onClick={() => setDescription(x)}>{x.slice(0, 60)}…</button>)}
          </div>
        </div>
        <div className="grid md:grid-cols-4 gap-4">
          <div><label className="label">Počet dokumentů</label><input type="number" min={1} max={500} className="input" value={count} onChange={(e) => setCount(Number(e.target.value))} /></div>
          <div><label className="label">Formát</label>
            <select className="input" value={format} onChange={(e) => setFormat(e.target.value)}>{FORMATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></div>
          <div><label className="label">Seed</label><input type="number" className="input" value={seed} onChange={(e) => setSeed(Number(e.target.value))} /></div>
          <div className="flex items-end"><label className="text-sm flex items-center gap-2 pb-2"><input type="checkbox" checked={useSource} onChange={(e) => setUseSource(e.target.checked)} /> Navázat na data z běhu</label></div>
        </div>
        {useSource && (
          <div className="grid md:grid-cols-2 gap-4 bg-slate-50 border border-slate-200 rounded-lg p-3">
            <div><label className="label">Datový běh</label>
              <select className="input" value={sourceRun} onChange={(e) => setSourceRun(e.target.value)}>
                <option value="">— vyberte —</option>
                {dataRuns.map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id} · {r.name} · {r.label}</option>)}
              </select></div>
            <div><label className="label">Tabulka (1 řádek = 1 dokument)</label>
              <select className="input" value={sourceTable} onChange={(e) => setSourceTable(e.target.value)} disabled={!schema}>
                <option value="">— vyberte —</option>
                {schema?.tables.map((t: Json) => <option key={t.name} value={t.name}>{t.name} ({t.rows} řádků)</option>)}
              </select></div>
            <p className="text-xs text-slate-500 md:col-span-2">Fakta se vezmou z řádků tabulky (např. stížnosti → formální odpovědi zákazníkům), takže dokumenty jsou konzistentní s vygenerovanými daty.</p>
          </div>
        )}
        {err && <div className="text-sm text-red-700">{err}</div>}
        <button className="btn btn-primary text-base px-5 py-2" onClick={submit}>Navrhnout dokumenty (Gemini) →</button>
      </div>

      <div className="card">
        <div className="px-5 py-3 border-b border-slate-200 font-semibold">Vygenerované sady dokumentů</div>
        {docRuns.length === 0 ? <div className="p-5 text-sm text-slate-500">Zatím žádné.</div> : (
          <table className="data">
            <thead><tr><th>Běh</th><th>Název</th><th>Typ</th><th>Formát</th><th>Počet</th><th>Vytvořeno</th><th>Verdikt</th><th></th></tr></thead>
            <tbody>{docRuns.map((r) => (
              <tr key={r.run_id}>
                <td className="mono text-xs">{r.run_id}</td><td>{r.name}{r.mock && <span className="chip bg-amber-50 text-amber-800 ml-2">mock</span>}</td>
                <td>{r.document_type}</td><td className="uppercase text-xs">{r.format}</td><td>{r.row_counts?.documents ?? r.count}</td>
                <td className="text-xs">{r.created_at?.replace('T', ' ')}</td>
                <td>{r.status === 'running' ? <span className="chip bg-blue-50 text-blue-700">běží</span> : <VerdictChip v={r.verdict?.verdict} />}</td>
                <td><Link className="btn" to={`/docs/runs/${r.run_id}`}>Otevřít</Link></td>
              </tr>))}</tbody>
          </table>
        )}
      </div>
    </div>
  )
}
