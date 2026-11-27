import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, type Json } from '../api'
import JobProgress from '../components/JobProgress'

const DOC_PHASES: [string, string][] = [
  ['plan', 'Start'], ['facts', 'Fakta'], ['story', 'Skrytý vzor'], ['write', 'Psaní (CHEAP)'], ['repair', 'Repair'],
  ['render', 'Vykreslení'], ['validate', 'Validace (STRONG)'], ['done', 'Hotovo'],
]
export { DOC_PHASES }

const newFact = () => ({ name: 'new_field', type: 'text', description: '', enum_values: [], min: null, max: null, must_mention: false })

export default function DocSpecReview() {
  const { sid } = useParams()
  const nav = useNavigate()
  const [session, setSession] = useState<Json>(null)
  const [spec, setSpec] = useState<Json>(null)
  const [feedback, setFeedback] = useState('')
  const [job, setJob] = useState<{ id: string; kind: 'run' | 'replan' } | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const load = () => api.docsSession(sid!).then((s) => { setSession(s); setSpec(s.response.spec); setFeedback('') })
  useEffect(() => { load() }, [sid]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!spec) return <div className="text-slate-500">Načítám…</div>

  const set = (k: string, v: Json) => setSpec({ ...spec, [k]: v })
  const setFact = (i: number, patch: Json) => set('facts', spec.facts.map((f: Json, j: number) => (j === i ? { ...f, ...patch } : f)))
  const setSec = (i: number, patch: Json) => set('sections', spec.sections.map((s: Json, j: number) => (j === i ? { ...s, ...patch } : s)))
  const story = spec.story
  const fromSource = !!spec.source_run_id
  const resp = session.response

  const approve = async () => {
    setErr(null)
    try { setJob({ id: (await api.docsStartRun({ spec, label: spec.name })).job_id, kind: 'run' }) } catch (e) { setErr(String(e)) }
  }
  const replan = async () => {
    setErr(null)
    try { setJob({ id: (await api.docsReplan(sid!, { spec, feedback })).job_id, kind: 'replan' }) } catch (e) { setErr(String(e)) }
  }

  if (job) {
    return (
      <div className="max-w-4xl mx-auto">
        <JobProgress jobId={job.id} title={job.kind === 'run' ? 'Generuji dokumenty' : 'Přeplánování'}
          phases={job.kind === 'run' ? DOC_PHASES : [['plan', 'Přeplánování'], ['done', 'Hotovo']]}
          onDone={(r) => { if (job.kind === 'run') nav(`/docs/runs/${r.run_id}`); else { setJob(null); load() } }} />
      </div>
    )
  }

  return (
    <div className="grid lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2 space-y-5">
        <div className="card p-5 space-y-3">
          <div className="flex items-center gap-3"><h1 className="text-xl font-bold flex-1">Review specifikace dokumentů</h1>
            <span className="chip bg-slate-100">kolo {session.round}</span>
            {fromSource && <span className="chip bg-violet-50 text-violet-800 border border-violet-200">fakta z {spec.source_table} ({spec.source_run_id})</span>}</div>
          <div className="grid md:grid-cols-4 gap-3">
            <div className="md:col-span-2"><label className="label">Název sady</label><input className="input" value={spec.name} onChange={(e) => set('name', e.target.value)} /></div>
            <div className="md:col-span-2"><label className="label">Typ dokumentu</label><input className="input" value={spec.document_type} onChange={(e) => set('document_type', e.target.value)} /></div>
            <div className="md:col-span-4"><label className="label">Popis</label><input className="input" value={spec.description} onChange={(e) => set('description', e.target.value)} /></div>
            <div className="md:col-span-4"><label className="label">Styl a konvence</label><input className="input" value={spec.style} onChange={(e) => set('style', e.target.value)} /></div>
            <div><label className="label">Počet</label><input type="number" className="input" value={spec.count} onChange={(e) => set('count', Number(e.target.value))} /></div>
            <div><label className="label">Formát</label><select className="input" value={spec.format} onChange={(e) => set('format', e.target.value)}>{['pdf', 'html', 'md', 'txt', 'json', 'eml'].map((f) => <option key={f}>{f}</option>)}</select></div>
            <div><label className="label">Min. slov</label><input type="number" className="input" value={spec.min_words} onChange={(e) => set('min_words', Number(e.target.value))} /></div>
            <div><label className="label">Max. slov</label><input type="number" className="input" value={spec.max_words} onChange={(e) => set('max_words', Number(e.target.value))} /></div>
          </div>
          {resp.notes && <div className="text-xs text-slate-600 bg-slate-50 border rounded p-2">{resp.notes}</div>}
        </div>

        <div className="card p-5">
          <div className="flex items-center mb-2"><h2 className="font-semibold flex-1">Fakta – strukturovaný záznam ke každému dokumentu</h2>
            {!fromSource && <button className="btn text-xs" onClick={() => set('facts', [...spec.facts, newFact()])}>+ pole</button>}</div>
          <p className="text-xs text-slate-500 mb-2">„Musí zaznít“ = hodnota se musí v textu objevit doslova (kontroluje se deterministicky u každého dokumentu).</p>
          <table className="data">
            <thead><tr><th>Pole</th><th>Typ</th><th>Popis</th><th>Hodnoty / rozsah</th><th>Musí zaznít</th><th></th></tr></thead>
            <tbody>{spec.facts.map((f: Json, i: number) => (
              <tr key={i}>
                <td>{fromSource ? <span className="mono">{f.name}</span> : <input className="input mono py-0.5" value={f.name} onChange={(e) => setFact(i, { name: e.target.value })} />}</td>
                <td>{fromSource ? f.type : <select className="input py-0.5" value={f.type} onChange={(e) => setFact(i, { type: e.target.value })}>{['text', 'int', 'float', 'date', 'datetime', 'bool'].map((t) => <option key={t}>{t}</option>)}</select>}</td>
                <td><input className="input py-0.5 text-xs" value={f.description} onChange={(e) => setFact(i, { description: e.target.value })} /></td>
                <td className="text-xs">{fromSource ? (f.enum_values?.join(', ') || (f.min !== null || f.max !== null ? `[${f.min ?? ''}, ${f.max ?? ''}]` : '')) : (
                  <input className="input py-0.5 text-xs" placeholder="a, b, c  nebo  min–max" value={f.enum_values?.length ? f.enum_values.join(', ') : (f.min !== null || f.max !== null ? `${f.min ?? ''}–${f.max ?? ''}` : '')}
                    onChange={(e) => {
                      const v = e.target.value
                      const m = v.match(/^\s*(-?[\d.]+)\s*–\s*(-?[\d.]+)\s*$/)
                      setFact(i, m ? { min: Number(m[1]), max: Number(m[2]), enum_values: [] } : { enum_values: v ? v.split(',').map((x) => x.trim()).filter(Boolean) : [], min: null, max: null })
                    }} />)}</td>
                <td className="text-center"><input type="checkbox" checked={f.must_mention} onChange={(e) => setFact(i, { must_mention: e.target.checked })} /></td>
                <td>{!fromSource && <button className="text-red-600 text-xs cursor-pointer" onClick={() => set('facts', spec.facts.filter((_: Json, j: number) => j !== i))}>✕</button>}</td>
              </tr>))}</tbody>
          </table>
        </div>

        <div className="card p-5">
          <div className="flex items-center mb-2"><h2 className="font-semibold flex-1">Sekce dokumentu</h2>
            <button className="btn text-xs" onClick={() => set('sections', [...spec.sections, { title: 'Nová sekce', kind: 'text', instructions: '' }])}>+ sekce</button></div>
          <div className="space-y-2">{spec.sections.map((s: Json, i: number) => (
            <div key={i} className="grid grid-cols-12 gap-2 items-center">
              <span className="col-span-1 text-slate-400 text-sm">{i + 1}.</span>
              <input className="input col-span-3" value={s.title} onChange={(e) => setSec(i, { title: e.target.value })} />
              <select className="input col-span-2" value={s.kind} onChange={(e) => setSec(i, { kind: e.target.value })}>
                <option value="text">text</option><option value="list">seznam</option><option value="key_values">klíč–hodnota</option><option value="table">tabulka</option></select>
              <input className="input col-span-5 text-xs" value={s.instructions} placeholder="instrukce pro autora" onChange={(e) => setSec(i, { instructions: e.target.value })} />
              <button className="col-span-1 text-red-600 text-xs cursor-pointer" onClick={() => set('sections', spec.sections.filter((_: Json, j: number) => j !== i))}>✕</button>
            </div>))}</div>
        </div>

        <div className="card p-5 space-y-3">
          <div className="flex items-center"><h2 className="font-semibold flex-1">Skrytý vzor (příběh napříč dokumenty)</h2>
            <label className="text-sm flex items-center gap-2"><input type="checkbox" checked={!!story} onChange={(e) => set('story', e.target.checked ? { logline: '', affected_share: 0.25, overrides: [], writer_hint: '' } : null)} /> zapnout</label></div>
          {story && (<>
            <div><label className="label">Vzor (ground truth)</label><input className="input" value={story.logline} onChange={(e) => set('story', { ...story, logline: e.target.value })} /></div>
            <div><label className="label">Podíl zasažených dokumentů: {Math.round(story.affected_share * 100)} %</label>
              <input type="range" min={0} max={1} step={0.05} className="w-full" value={story.affected_share} onChange={(e) => set('story', { ...story, affected_share: Number(e.target.value) })} /></div>
            <div><label className="label">Změny faktů u zasažených dokumentů (deterministicky, podle seedu)</label>
              {story.overrides.map((o: Json, i: number) => (
                <div key={i} className="flex gap-2 mb-1">
                  <select className="input" style={{ width: 200 }} value={o.field} onChange={(e) => set('story', { ...story, overrides: story.overrides.map((x: Json, j: number) => (j === i ? { ...x, field: e.target.value } : x)) })}>
                    {spec.facts.map((f: Json) => <option key={f.name}>{f.name}</option>)}</select>
                  <select className="input" style={{ width: 120 }} value={o.op} onChange={(e) => set('story', { ...story, overrides: story.overrides.map((x: Json, j: number) => (j === i ? { ...x, op: e.target.value } : x)) })}>
                    <option value="set">nastavit</option><option value="multiply">násobit</option><option value="add">přičíst</option></select>
                  <input className="input" style={{ width: 160 }} value={o.value} onChange={(e) => set('story', { ...story, overrides: story.overrides.map((x: Json, j: number) => (j === i ? { ...x, value: e.target.value } : x)) })} />
                  <button className="text-red-600 text-xs cursor-pointer" onClick={() => set('story', { ...story, overrides: story.overrides.filter((_: Json, j: number) => j !== i) })}>✕</button>
                </div>))}
              <button className="btn text-xs" onClick={() => set('story', { ...story, overrides: [...story.overrides, { field: spec.facts[0]?.name, op: 'set', value: '' }] })}>+ změna</button></div>
            <div><label className="label">Jak se má vzor projevit v textu (pro autora)</label><input className="input" value={story.writer_hint} onChange={(e) => set('story', { ...story, writer_hint: e.target.value })} /></div>
          </>)}
        </div>
      </div>

      <aside className="space-y-5 lg:sticky lg:top-4 self-start">
        {(resp.questions?.length > 0 || resp.suggestions?.length > 0) && (
          <div className="card p-5 space-y-2 text-sm">
            {resp.questions?.length > 0 && <><h3 className="font-semibold">Dotazy modelu</h3><ul className="list-disc ml-5">{resp.questions.map((q: string) => <li key={q}>{q}</li>)}</ul></>}
            {resp.suggestions?.length > 0 && <><h3 className="font-semibold pt-2">Návrhy</h3><ul className="list-disc ml-5">{resp.suggestions.map((q: string) => <li key={q}>{q}</li>)}</ul>
              <p className="text-xs text-slate-500">Návrh přijmete tak, že ho napíšete do zpětné vazby a přeplánujete, nebo ho rovnou upravíte ve formuláři.</p></>}
          </div>
        )}
        <div className="card p-5 space-y-3">
          <label className="label">Zpětná vazba pro přeplánování</label>
          <textarea className="input h-24" value={feedback} onChange={(e) => setFeedback(e.target.value)} placeholder="Např. přidej sekci s medikací při propuštění…" />
          <button className="btn w-full justify-center" disabled={!feedback.trim() || session.round >= 4} onClick={replan}>↻ Přeplánovat (kolo {session.round + 1}/4)</button>
          <button className="btn btn-primary w-full justify-center py-2.5 text-base" onClick={approve}>✓ Schválit a generovat {spec.count} dokumentů</button>
          {err && <div className="text-xs text-red-700">{err}</div>}
        </div>
      </aside>
    </div>
  )
}
