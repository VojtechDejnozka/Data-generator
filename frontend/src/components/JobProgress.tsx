import { useEffect, useRef, useState } from 'react'
import { watchJob, type Json, type JobEvent } from '../api'

const RUN_PHASES: [string, string][] = [
  ['plan', 'Plán generování (STRONG)'],
  ['dimensions', 'Master data (CHEAP)'],
  ['simulation', 'Kauzální simulace'],
  ['llm_rows', 'Řádky přes LLM (CHEAP)'],
  ['write', 'Zápis SQLite'],
  ['checks', 'Deterministické kontroly'],
  ['evidence', 'Důkazní dotazy'],
  ['validate', 'Validace (STRONG)'],
  ['repair', 'Repair smyčka'],
  ['dashboard', 'Dashboard'],
  ['done', 'Hotovo'],
]

export default function JobProgress({ jobId, onDone, phases = RUN_PHASES, title = 'Probíhá zpracování' }: {
  jobId: string
  onDone: (result: Json) => void
  phases?: [string, string][]
  title?: string
}) {
  const [events, setEvents] = useState<JobEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const done = useRef(onDone)
  done.current = onDone

  useEffect(() => {
    setEvents([])
    setError(null)
    return watchJob(jobId, (e) => {
      if (e.final) {
        if (e.status === 'error') setError(e.error ?? 'Neznámá chyba')
        else done.current(e.result)
        return
      }
      setEvents((prev) => [...prev, e])
    })
  }, [jobId])

  const seen = new Set(events.map((e) => e.phase))
  const current = events.at(-1)?.phase
  const progress = events.at(-1)?.progress ?? 0
  const logRef = useRef<HTMLDivElement>(null)
  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight) }, [events.length])

  return (
    <div className="card p-5">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold">{title}</h3>
        <span className="text-sm text-slate-500 tabular-nums">{Math.round(progress * 100)} %</span>
      </div>
      <div className="h-2 bg-slate-100 rounded-full overflow-hidden mb-4">
        <div className="h-full bg-blue-600 transition-all" style={{ width: `${progress * 100}%` }} />
      </div>
      {phases.length > 1 && (
        <ol className="flex flex-wrap gap-2 mb-4">
          {phases.map(([id, label]) => {
            const active = id === current && !error
            const ok = seen.has(id) && !active
            return (
              <li key={id} className={`chip border ${active ? 'bg-blue-50 border-blue-300 text-blue-800' : ok ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-white border-slate-200 text-slate-400'}`}>
                {ok ? '✓ ' : active ? '● ' : ''}{label}
              </li>
            )
          })}
        </ol>
      )}
      <div ref={logRef} className="mono text-xs bg-slate-900 text-slate-100 rounded-lg p-3 h-48 overflow-auto">
        {events.map((e, i) => (
          <div key={i}><span className="text-slate-500">{e.t?.toFixed(1)}s</span> <span className="text-sky-300">[{e.phase}]</span> {e.message}</div>
        ))}
        {error && <div className="text-red-300 mt-2">✖ {error}</div>}
      </div>
    </div>
  )
}
