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

export default function JobProgress({ jobId, onDone, onError, phases = RUN_PHASES, title = 'Probíhá zpracování' }: {
  jobId: string
  onDone: (result: Json) => void
  onError?: (message: string) => void
  phases?: [string, string][]
  title?: string
}) {
  const [events, setEvents] = useState<JobEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const done = useRef(onDone)
  const failed = useRef(onError)
  done.current = onDone
  failed.current = onError

  useEffect(() => {
    setEvents([])
    setError(null)
    return watchJob(jobId, (e) => {
      if (e.final) {
        if (e.status === 'error') {
          const message = e.error ?? 'Neznámá chyba'
          setError(message)
          failed.current?.(message)
        }
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
    <div aria-live="polite">
      <div className="flex items-baseline justify-between mb-3">
        <h3 className="text-[21px] font-semibold tracking-[-0.01em]">{title}</h3>
        <span className="text-sm text-slate-500 tabular-nums">{Math.round(progress * 100)} %</span>
      </div>
      <div className="h-1 bg-slate-100 mb-5" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)}>
        <div className={`h-full transition-all duration-500 ${error ? 'bg-red-700' : 'bg-ink'}`} style={{ width: `${progress * 100}%` }} />
      </div>
      {phases.length > 1 && (
        <ol className="flex flex-wrap gap-x-5 gap-y-1.5 mb-5 text-sm">
          {phases.map(([id, label]) => {
            const active = id === current && !error
            const ok = seen.has(id) && !active
            return (
              <li key={id} className={active ? 'mark font-medium' : ok ? 'text-ink' : 'text-slate-400'}>
                {ok && <span aria-hidden="true" className="mr-1">✓</span>}{label}
              </li>
            )
          })}
        </ol>
      )}
      <div ref={logRef} className="mono text-xs leading-relaxed bg-slate-50 border border-line rounded-md p-3 h-48 overflow-auto">
        {events.map((e, i) => (
          <div key={i}><span className="text-slate-400 tabular-nums">{e.t?.toFixed(1)}s</span> <span className="text-slate-500">{e.phase}</span> {e.message}</div>
        ))}
        {error && <div className="text-red-700 mt-2">✕ {error}</div>}
      </div>
    </div>
  )
}
