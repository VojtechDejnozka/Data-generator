import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type Json } from '../api'
import JobProgress from '../components/JobProgress'

const TARGETS = [
  ['tables', 'Tabulky'],
  ['variables', 'Kauzální proměnné'],
  ['entity_instances', 'Instance základních entit'],
  ['rows', 'Řádky celkem'],
] as const

async function readContext(file: File | null) {
  if (!file) return ''
  const text = await file.text()
  if (text.length > 50000) throw new Error(`Soubor ${file.name} překračuje limit 50 000 znaků.`)
  return text
}

export default function NewScenario() {
  const [story, setStory] = useState('')
  const [volume, setVolume] = useState('medium')
  const [seed, setSeed] = useState(42)
  const [preset, setPreset] = useState<string | null>(null)
  const [engine, setEngine] = useState<'module' | 'scm'>('scm')
  const [presets, setPresets] = useState<Json[]>([])
  const [sql, setSql] = useState<File | null>(null)
  const [ontology, setOntology] = useState<File | null>(null)
  const [model, setModel] = useState<File | null>(null)
  const [targets, setTargets] = useState<Partial<Record<(typeof TARGETS)[number][0], number>>>({})
  const [mode, setMode] = useState<'automatic' | 'manual'>('automatic')
  const [target, setTarget] = useState(50)
  const [missing, setMissing] = useState('0')
  const [noise, setNoise] = useState('0')
  const [job, setJob] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const nav = useNavigate()
  const scm = engine === 'scm'
  const importing = scm && !!model

  useEffect(() => { api.presets().then(setPresets).catch(() => undefined) }, [])

  const submit = async () => {
    setErr(null)
    setBusy(true)
    const observation = mode === 'automatic' ? { mode, target }
      : { mode, missing_percent: Number(missing), noise_percent: Number(noise) }
    try {
      if (scm && model) {
        let data: Json
        try { data = JSON.parse((await model.text()).replace(/^\uFEFF/, '')) } catch { throw new Error('Soubor SCM nemá platný formát JSON.') }
        if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Soubor SCM musí obsahovat model jako objekt JSON.')
        nav(`/plan/${(await api.scmImport(data, observation)).session_id}`)
        return
      }
      if (story.trim().length < 10) throw new Error('Popište příběh alespoň deseti znaky.')
      const [sql_schema, ontologyText] = scm ? await Promise.all([readContext(sql), readContext(ontology)]) : ['', '']
      const complexity = scm ? targets : { tables: targets.tables, rows: targets.rows }
      const result = await api.plan({ story: story.trim(), volume, seed, preset: scm ? null : preset, engine, complexity,
        ...(scm ? { sql_schema, ontology: ontologyText, observation } : {}) })
      setJob(result.job_id)
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  if (job) return (
    <div className="max-w-3xl space-y-4">
      <JobProgress jobId={job} title={scm ? 'Navrhuji kauzální model' : 'Navrhuji scénář'}
        phases={[["plan", "Návrh"], ["done", "Hotovo"]]} onDone={(r) => nav(`/plan/${r.session_id}`)} />
      <button className="btn" onClick={() => setJob(null)}>Zpět k zadání</button>
    </div>
  )

  return (
    <form className="max-w-3xl space-y-7" onSubmit={(e) => { e.preventDefault(); void submit() }}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-[28px] leading-tight font-semibold tracking-[-0.015em]">Nový scénář</h1>
        {engine === 'module' && presets.map((p) => (
          <button type="button" key={p.id} className="btn" onClick={() => {
            setStory(p.input.story); setVolume(p.input.volume); setSeed(p.input.seed); setPreset(p.id)
          }}>{p.title}</button>
        ))}
      </div>
      <label className="block"><span className="label">Způsob generování</span>
        <select className="input max-w-sm" value={engine} onChange={(e) => setEngine(e.target.value as 'module' | 'scm')}>
          <option value="scm">Kauzální model (SCM)</option><option value="module">Klasický scénář</option>
        </select>
      </label>
      {!importing && <label className="block"><span className="label">Popis scénáře</span>
        <textarea className="input min-h-40" maxLength={8000} value={story}
          onChange={(e) => { setStory(e.target.value); setPreset(null) }}
          placeholder="Popište prostředí, co se stalo a jaké souvislosti mají být v datech vidět." />
      </label>}
      {scm && <fieldset className="border-t border-slate-200 pt-3">
        <legend className="font-semibold text-sm pr-2">Vstupní soubory (volitelné)</legend>
        <div className="grid md:grid-cols-3 gap-3">
          {!importing && <>
            <label><span className="label">SQL schéma (.sql)</span><input className="input text-xs" type="file" accept=".sql" onChange={(e) => setSql(e.target.files?.[0] ?? null)} />{sql && <span className="text-xs text-slate-600">{sql.name}</span>}</label>
            <label><span className="label">Ontologie (.ttl)</span><input className="input text-xs" type="file" accept=".ttl,text/turtle" onChange={(e) => setOntology(e.target.files?.[0] ?? null)} />{ontology && <span className="text-xs text-slate-600">{ontology.name}</span>}</label>
          </>}
          <label><span className="label">Existující SCM (.json)</span><input key={model ? 'selected' : 'empty'} className="input text-xs" type="file" accept=".json,application/json" onChange={(e) => setModel(e.target.files?.[0] ?? null)} /></label>
          {model && <div className="self-end text-sm">{model.name} <button className="btn ml-2" type="button" onClick={() => setModel(null)}>Odebrat</button></div>}
        </div>
        <p className="text-xs text-slate-600 mt-2">{importing ? 'Model se ověří a otevře přímo k úpravám, včetně původního seedu.' : 'SQL a ontologie slouží jako doporučená struktura pro nový model.'}</p>
      </fieldset>}
      {!importing && <fieldset className="border-t border-slate-200 pt-3">
        <legend className="font-semibold text-sm pr-2">Velikost a složitost datasetu</legend>
        <label className="block max-w-xs mb-2"><span className="label">Přibližný rozsah</span>
          <select className="input" value={volume} onChange={(e) => setVolume(e.target.value)}>
            <option value="small">Malý</option><option value="medium">Střední</option><option value="large">{scm ? 'Velký (alespoň 100 000 řádků celkem)' : 'Velký'}</option>
          </select>
        </label>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {TARGETS.filter(([key]) => scm || key === 'tables' || key === 'rows').map(([key, label]) => <label key={key}>
            <span className="label">{label}</span>
            <input className="input" type="number" min={1} step={1} placeholder="Volitelné" value={targets[key] ?? ''}
              onChange={(e) => setTargets({ ...targets, [key]: e.target.value === '' ? undefined : Number(e.target.value) })} />
          </label>)}
        </div>
        <p className="text-xs text-slate-600 mt-2">{scm ? 'Orientační cíle. Kauzální proměnné jsou uzly grafu. Celkový počet řádků zahrnuje i odvozené a časové tabulky.' : 'Orientační cíle podle možností scénáře.'}</p>
      </fieldset>}
      {scm && <fieldset className="border-t border-slate-200 pt-3 space-y-2">
        <legend className="font-semibold text-sm pr-2">Obtížnost rekonstrukce příběhu</legend>
        <div className="flex flex-wrap gap-4 text-sm">
          <label><input type="radio" name="difficulty" checked={mode === 'automatic'} onChange={() => setMode('automatic')} /> Automaticky</label>
          <label><input type="radio" name="difficulty" checked={mode === 'manual'} onChange={() => setMode('manual')} /> Ručně</label>
        </div>
        {mode === 'automatic' ? <label className="block max-w-md"><span className="label">Cílová obtížnost {target} / 100</span>
          <input className="w-full accent-blue-600" type="range" min={0} max={100} value={target} onChange={(e) => setTarget(Number(e.target.value))} />
          <span className="text-xs text-slate-600">Vyšší hodnota ztěžuje odhalení příběhu z dat. Aplikace vybere nejlepší nalezené nastavení.</span>
        </label> : <>
          <div className="grid md:grid-cols-2 gap-3 max-w-xl">
            <label><span className="label">Chybějící hodnoty (NA, %)</span><input className="input" type="number" min={0} max={100} step="any" required value={missing} onChange={(e) => setMissing(e.target.value)} /></label>
            <label><span className="label">Pozorovací šum (%)</span><input className="input" type="number" min={0} step="any" required value={noise} onChange={(e) => setNoise(e.target.value)} /></label>
          </div>
          <p className="text-xs text-slate-600">Například 10 % přidá k číslům náhodnou chybu se směrodatnou odchylkou 10 % původní odchylky sloupce. Týká se pouze pozorované kopie dat.</p>
        </>}
      </fieldset>}
      {!importing && <details className="text-sm"><summary className="cursor-pointer text-slate-700">Pokročilé nastavení</summary>
        <label className="block max-w-40 mt-2"><span className="label">Seed</span><input required className="input" type="number" step={1} value={seed} onChange={(e) => setSeed(Number(e.target.value))} /></label>
      </details>}
      {err && <p role="alert" className="text-sm text-red-700">{err}</p>}
      <button className="btn btn-primary" disabled={busy}>{busy ? 'Zpracovávám…' : importing ? 'Otevřít SCM' : scm ? 'Navrhnout SCM' : 'Navrhnout scénář'}</button>
    </form>
  )
}
