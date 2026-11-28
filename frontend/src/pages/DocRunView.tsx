import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, fmt, type Json } from '../api'
import { ResultTable } from '../components/Chart'
import { SqlTab } from '../components/Tabs'
import { VerdictChip } from './Home'

export default function DocRunView() {
  const { rid } = useParams()
  const [run, setRun] = useState<Json>(null)
  const [sel, setSel] = useState<number | null>(null)
  const [doc, setDoc] = useState<Json>(null)
  const [tab, setTab] = useState('docs')
  const [facts, setFacts] = useState<Json>(null)
  const [gt, setGt] = useState<Json>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api.docsRun(rid!).then((r) => { setRun(r); setSel(r.documents?.[0]?.doc_id ?? null) }).catch((e) => setErr(String(e)))
  }, [rid])
  useEffect(() => { if (sel !== null) api.docsDocument(rid!, sel).then(setDoc) }, [rid, sel])
  useEffect(() => { if (tab === 'facts' && !facts) api.query(rid!, 'SELECT * FROM facts', 5000).then(setFacts) }, [tab, facts, rid])

  if (err) return <div className="text-red-700">{err}</div>
  if (!run) return <div className="text-slate-500">Načítám…</div>
  const spec = run.doc_spec
  const docs = run.documents as Json[]
  const fileUrl = (f: string, dl = false) => `/api/docs/runs/${rid}/files/${f}${dl ? '?download=true' : ''}`
  const selDoc = docs.find((d) => d.doc_id === sel)
  const inlineFile = ['pdf', 'html', 'txt', 'md', 'json'].includes(spec.format)
  const affected: number[] = gt?.affected_doc_ids ?? []
  const v = run.verdict ?? {}

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-xs text-slate-500 mono">{run.run_id}{spec.source_run_id && <> · fakta z tabulky {spec.source_table}{spec.source_filter_col && ` (${spec.source_filter_col} = ${spec.source_filter_val})`} běhu <Link className="text-blue-700" to={`/runs/${spec.source_run_id}?tab=files`}>{spec.source_run_id}</Link></>}</div>
          <h1 className="text-[28px] leading-tight font-semibold tracking-[-0.015em] truncate">{spec.name} <span className="text-slate-500 font-normal text-base">– {docs.length} × {spec.document_type} ({spec.format.toUpperCase()})</span></h1>
        </div>
        <VerdictChip v={v.verdict} />
        <span className="chip bg-slate-100">{run.checks_summary.passed}/{run.checks_summary.total} prošlo kontrolami</span>
        {run.mock && <span className="chip bg-amber-50 text-amber-800 border border-amber-200">mock LLM</span>}
        <a className="btn" href={`/api/docs/runs/${rid}/export.zip`}>⬇ Vše (ZIP)</a>
        <a className="btn" href={`/api/runs/${rid}/db`}>⬇ Fakta (SQLite)</a>
      </div>
      <div className="flex border-b border-slate-200">
        {[['docs', 'Dokumenty'], ['facts', 'Fakta'], ['sql', 'SQL nad fakty'], ['validation', 'Validace & kontroly'], ['truth', 'Ground truth'], ['log', 'Log & tokeny']].map(([id, l]) => (
          <button key={id} className={`tab ${tab === id ? 'tab-active' : ''}`} onClick={() => setTab(id)}>{l}</button>))}
      </div>

      {tab === 'docs' && (
        <div className="grid lg:grid-cols-12 gap-4">
          <div className="lg:col-span-3 card overflow-auto max-h-[78vh]">
            {docs.map((d) => (
              <button key={d.doc_id} onClick={() => setSel(d.doc_id)}
                className={`w-full text-left px-3 py-2 border-b border-slate-100 cursor-pointer ${sel === d.doc_id ? 'bg-blue-50' : 'hover:bg-slate-50'}`}>
                <div className="flex items-center gap-2 text-sm">
                  <span className={d.checks_ok ? 'text-emerald-700' : 'text-red-700'}>{d.checks_ok ? '✓' : '✖'}</span>
                  <span className="truncate flex-1">{d.title}</span>
                  {affected.includes(d.doc_id) && <span className="chip bg-violet-50 text-violet-800 text-[10px]">vzor</span>}
                </div>
                <div className="text-[11px] text-slate-500 ml-5">{d.file} · {d.words} slov</div>
              </button>))}
          </div>
          <div className="lg:col-span-6 card p-2">
            {selDoc && (inlineFile
              ? <iframe key={selDoc.file} title={selDoc.title} src={fileUrl(selDoc.file)} className="w-full h-[76vh] rounded border-0 bg-white" />
              : doc && <iframe title="preview" srcDoc={doc.html} className="w-full h-[76vh] rounded border-0" />)}
          </div>
          <div className="lg:col-span-3 space-y-3">
            {selDoc && (
              <div className="card p-3 text-sm space-y-2">
                <a className="btn w-full justify-center" href={fileUrl(selDoc.file, true)}>⬇ Stáhnout {selDoc.file}</a>
                <div className={selDoc.checks_ok ? 'text-emerald-700' : 'text-red-700'}>
                  {selDoc.checks_ok ? '✓ Všechna povinná fakta v textu, sekce kompletní' : selDoc.issues.map((x: string) => <div key={x}>✖ {x}</div>)}
                </div>
              </div>)}
            {doc && (
              <div className="card p-3">
                <div className="font-semibold text-sm mb-1">Fakta dokumentu</div>
                <table className="data text-xs"><tbody>
                  {Object.entries(doc.facts).map(([k, val]) => {
                    const f = spec.facts.find((x: Json) => x.name === k)
                    return <tr key={k}><td className="text-slate-500">{k}{f?.must_mention && <span title="musí zaznít v textu"> ●</span>}</td><td className="font-medium">{val === null ? '–' : String(val)}</td></tr>
                  })}
                </tbody></table>
              </div>)}
          </div>
        </div>
      )}

      {tab === 'facts' && (
        <div className="card p-4">
          <p className="text-sm text-slate-600 mb-2">Strukturovaná fakta, ze kterých dokumenty vznikly (1 řádek = 1 dokument). Příznak skrytého vzoru zde záměrně není – je jen v ground truth.</p>
          {facts ? <ResultTable columns={facts.columns} rows={facts.rows} max={1000} /> : 'Načítám…'}
        </div>
      )}

      {tab === 'sql' && <SqlTab runId={rid!} initial={`SELECT * FROM facts LIMIT 20`} />}

      {tab === 'validation' && (
        <div className="space-y-4">
          <div className="card p-4">
            <div className="flex items-center gap-2 mb-1"><VerdictChip v={v.verdict} /><span className="text-xs text-slate-500">validátor: {run.validator === 'strong' ? 'STRONG model' : 'deterministický'}</span></div>
            <p className="text-sm">{v.summary}</p>
            {v.story_detectability?.map((d: Json) => <p key={d.step_id} className="text-sm mt-2">{d.detectable ? '✓' : '✖'} Vzor dohledatelný ({Math.round(d.confidence * 100)} %): {d.comment}</p>)}
            {v.issues?.length > 0 && <ul className="text-sm list-disc ml-5 mt-2">{v.issues.map((i: Json) => <li key={i.id}><b>{i.severity}</b>: {i.description}</li>)}</ul>}
          </div>
          <div className="card p-4">
            <h3 className="font-semibold mb-2">Deterministické kontroly dokumentů</h3>
            <p className="text-xs text-slate-500 mb-2">U každého dokumentu: přítomnost všech sekcí, doslovné zmínění povinných faktů (včetně variant zápisu čísel a dat), délka v rozsahu, žádné nevyplněné placeholdery. Repair: {run.repair_history.map((h: Json) => `iterace ${h.iteration}: ${h.failed} chyb`).join(' → ')}</p>
            <ResultTable columns={['doc_id', 'soubor', 'slov', 'OK', 'problémy']} rows={docs.map((d) => [d.doc_id, d.file, d.words, d.checks_ok, d.issues.join('; ')])} max={500} />
          </div>
        </div>
      )}

      {tab === 'truth' && (!gt ? (
        <div className="card p-8 text-center">
          <p className="text-slate-600 mb-3">Ground truth říká, do kterých dokumentů byl vložen skrytý vzor a jak byla změněna jejich fakta.</p>
          <button className="btn btn-primary" onClick={() => api.groundTruth(rid!).then(setGt)}>👁 Odhalit ground truth</button>
        </div>
      ) : (
        <div className="card p-4 space-y-2 text-sm">
          {gt.story ? (<>
            <div><b>Vzor:</b> {gt.story.logline}</div>
            <div><b>Zasažené dokumenty ({gt.affected_count}/{gt.total}):</b> {gt.affected_doc_ids.join(', ')}</div>
            <div><b>Změny faktů:</b> {gt.story.overrides.map((o: Json) => `${o.field} ${o.op === 'set' ? '=' : o.op === 'multiply' ? '×' : '+'} ${o.value}`).join(' · ')}</div>
            <div><b>Projev v textu:</b> {gt.story.writer_hint}</div>
            <p className="text-xs text-slate-500">V záložce Dokumenty jsou nyní zasažené dokumenty označené štítkem „vzor“.</p>
          </>) : <div>Tato sada nemá skrytý vzor.</div>}
        </div>
      ))}

      {tab === 'log' && (
        <div className="card p-4 space-y-2 text-sm">
          <div>LLM volání: <b>{run.usage.total_calls}</b> · tokeny: <b>{fmt(run.usage.total_tokens)}</b> · odhad: <b>${run.usage.total_cost_usd.toFixed(4)}</b> · doba: <b>{run.duration_s} s</b></div>
          <ResultTable columns={['role', 'model', 'calls', 'cached', 'failed', 'prompt_tokens', 'output_tokens', 'cost_usd']}
            rows={run.usage.by_model.map((b: Json) => [b.role, b.model, b.calls, b.cached, b.failed, b.prompt_tokens, b.output_tokens, b.cost_usd])} />
        </div>
      )}
    </div>
  )
}
