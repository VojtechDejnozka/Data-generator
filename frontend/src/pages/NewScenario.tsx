import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type Json } from '../api'
import JobProgress from '../components/JobProgress'

const EMPTY = {
  company: '', environment: '', story: '',
  entity_counts: { stores: 10, products: 240, suppliers: 7, customers: 1500 } as Record<string, number>,
  period_start: '2025-01-01', period_end: '2026-03-31', volume: 'medium', story_intensity: 1, seed: 42,
  custom_model: '', data_language: 'cs', preset: null as string | null,
}

const COUNT_LABELS: Record<string, string> = { stores: 'Prodejny', products: 'Produkty', suppliers: 'Dodavatelé', customers: 'Zákazníci' }

export default function NewScenario() {
  const [form, setForm] = useState<Json>(EMPTY)
  const [presets, setPresets] = useState<Json[]>([])
  const [job, setJob] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const nav = useNavigate()
  useEffect(() => { api.presets().then(setPresets).catch(() => undefined) }, [])
  const set = (k: string, v: Json) => setForm((f: Json) => ({ ...f, [k]: v }))

  const submit = async () => {
    setErr(null)
    if (!form.company.trim() || !form.story.trim()) { setErr('Vyplňte popis firmy a příběh.'); return }
    try { setJob((await api.plan(form)).job_id) } catch (e) { setErr(String(e)) }
  }

  if (job) {
    return (
      <div className="max-w-3xl mx-auto space-y-4">
        <JobProgress jobId={job} title="STRONG model plánuje scénář" phases={[['plan', 'Návrh ScenarioSpec'], ['done', 'Hotovo']]}
          onDone={(r) => nav(`/plan/${r.session_id}`)} />
        <button className="btn" onClick={() => setJob(null)}>Zpět k zadání</button>
      </div>
    )
  }

  return (
    <div className="grid lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2 card p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-bold">1 · Zadání příběhu</h1>
          <div className="flex gap-2">
            {presets.map((p) => (
              <button key={p.id} className="btn" title={p.description} onClick={() => setForm({ ...EMPTY, ...p.input })}>
                ⚡ Preset: {p.title}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="label">Popis firmy</label>
          <textarea className="input h-20" value={form.company} onChange={(e) => set('company', e.target.value)}
            placeholder="Např. regionální síť supermarketů, 10 prodejen, věrnostní program…" />
        </div>
        <div>
          <label className="label">Prostředí (trh, sezónnost, provozní kontext)</label>
          <textarea className="input h-16" value={form.environment} onChange={(e) => set('environment', e.target.value)} />
        </div>
        <div>
          <label className="label">Příběh / situace, která se má v datech odehrát</label>
          <textarea className="input h-32" value={form.story} onChange={(e) => set('story', e.target.value)}
            placeholder="Co se stalo, komu, kdy, jaké to mělo důsledky – a co se naopak NEzměnilo…" />
        </div>
        <details>
          <summary className="text-sm font-medium cursor-pointer text-slate-700">Vlastní datový model / ontologie (volitelné)</summary>
          <textarea className="input h-28 mt-2 mono text-xs" value={form.custom_model} onChange={(e) => set('custom_model', e.target.value)}
            placeholder="např. tables: suppliers(id, name), products(id, supplier_id → suppliers) …" />
        </details>
        {err && <div className="text-sm text-red-700">{err}</div>}
        <button className="btn btn-primary text-base px-5 py-2" onClick={submit}>Navrhnout scénář (STRONG) →</button>
      </div>

      <div className="card p-6 space-y-4">
        <h2 className="font-semibold">Parametry složitosti</h2>
        <div className="grid grid-cols-2 gap-3">
          {Object.keys(form.entity_counts).map((k) => (
            <div key={k}>
              <label className="label">{COUNT_LABELS[k] ?? k}</label>
              <input type="number" min={1} className="input" value={form.entity_counts[k]}
                onChange={(e) => set('entity_counts', { ...form.entity_counts, [k]: Number(e.target.value) })} />
            </div>
          ))}
          <div><label className="label">Období od</label><input type="date" className="input" value={form.period_start} onChange={(e) => set('period_start', e.target.value)} /></div>
          <div><label className="label">Období do</label><input type="date" className="input" value={form.period_end} onChange={(e) => set('period_end', e.target.value)} /></div>
          <div>
            <label className="label">Objem dat</label>
            <select className="input" value={form.volume} onChange={(e) => set('volume', e.target.value)}>
              <option value="small">malý (~20 návštěv/den)</option><option value="medium">střední (~45)</option><option value="large">velký (~90)</option>
            </select>
          </div>
          <div><label className="label">Seed</label><input type="number" className="input" value={form.seed} onChange={(e) => set('seed', Number(e.target.value))} /></div>
        </div>
        <div>
          <label className="label">Intenzita příběhu: {form.story_intensity} {form.story_intensity === 0 && '(baseline bez příběhu)'}</label>
          <input type="range" min={0} max={2} step={0.1} className="w-full" value={form.story_intensity}
            onChange={(e) => set('story_intensity', Number(e.target.value))} />
        </div>
        <p className="text-xs text-slate-500">
          Parametry se promítnou do specifikace jako počty řádků a knoby. Knoby jde později měnit ve what-if analýze
          se stejným seedem.
        </p>
      </div>
    </div>
  )
}
