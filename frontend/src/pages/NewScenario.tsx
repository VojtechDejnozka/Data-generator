import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type Json } from '../api'
import JobProgress from '../components/JobProgress'

// Only the story, data size and seed come from the user. Everything else (company, environment,
// entity counts, whether there is a time axis and which period, story intensity) is decided by
// the STRONG model and can be reviewed/edited on the next screen.
const SIZES: [string, string, string][] = [
  ['small', 'Malý', 'desítky až stovky řádků v hlavních tabulkách – rychlé generování'],
  ['medium', 'Střední', 'stovky až tisíce řádků; u simulací stovky tisíc transakcí'],
  ['large', 'Velký', 'tisíce řádků; u simulací až ~1 mil. transakcí'],
]

export default function NewScenario() {
  const [story, setStory] = useState('')
  const [volume, setVolume] = useState('medium')
  const [seed, setSeed] = useState(42)
  const [preset, setPreset] = useState<string | null>(null)
  const [presets, setPresets] = useState<Json[]>([])
  const [job, setJob] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const nav = useNavigate()
  useEffect(() => { api.presets().then(setPresets).catch(() => undefined) }, [])

  const submit = async () => {
    setErr(null)
    if (story.trim().length < 10) { setErr('Popište příběh (alespoň pár vět).'); return }
    try { setJob((await api.plan({ story, volume, seed, preset })).job_id) } catch (e) { setErr(String(e)) }
  }

  if (job) {
    return (
      <div className="max-w-3xl mx-auto space-y-4">
        <JobProgress jobId={job} title="STRONG model navrhuje scénář" phases={[['plan', 'Návrh ScenarioSpec'], ['done', 'Hotovo']]}
          onDone={(r) => nav(`/plan/${r.session_id}`)} />
        <button className="btn" onClick={() => setJob(null)}>Zpět k zadání</button>
      </div>
    )
  }

  return (
    <div className="max-w-4xl mx-auto card p-6 space-y-5">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-xl font-bold">1 · Zadání příběhu</h1>
        <div className="flex gap-2">
          {presets.map((p) => (
            <button key={p.id} className="btn" title={p.description}
              onClick={() => { setStory(p.input.story); setVolume(p.input.volume); setSeed(p.input.seed); setPreset(p.id) }}>
              ⚡ Preset: {p.title}
            </button>
          ))}
        </div>
      </div>

      <div>
        <label className="label">Popis příběhu</label>
        <textarea className="input h-56" value={story} onChange={(e) => { setStory(e.target.value); setPreset(null) }}
          placeholder="Co je to za firmu či prostředí, co se stalo, komu, kdy a s jakými důsledky – a co se naopak NEzměnilo…" />
        <p className="text-xs text-slate-500 mt-1">
          Firmu, prostředí, počty entit, intenzitu příběhu i to, zda data potřebují časovou osu (a jaké období),
          navrhne Gemini. Všechno pak uvidíte a můžete upravit v review.
        </p>
      </div>

      <div className="grid md:grid-cols-4 gap-4 items-start">
        <div className="md:col-span-3">
          <label className="label">Velikost dat</label>
          <div className="grid grid-cols-3 gap-2">
            {SIZES.map(([id, label, desc]) => (
              <button key={id} onClick={() => setVolume(id)}
                className={`text-left border rounded-lg p-3 cursor-pointer ${volume === id ? 'border-blue-500 bg-blue-50' : 'border-slate-200 hover:bg-slate-50'}`}>
                <div className="font-medium text-sm">{label}</div>
                <div className="text-xs text-slate-500">{desc}</div>
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="label">Seed</label>
          <input type="number" className="input" value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
          <p className="text-xs text-slate-500 mt-1">Stejný seed = stejná data.</p>
        </div>
      </div>

      {err && <div className="text-sm text-red-700">{err}</div>}
      <button className="btn btn-primary text-base px-5 py-2" onClick={submit}>Navrhnout scénář (Gemini) →</button>
    </div>
  )
}
