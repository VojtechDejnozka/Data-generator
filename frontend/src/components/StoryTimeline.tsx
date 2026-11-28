import type { CausalStep, Distractor, Knob } from '../api'

const DAY = 86_400_000

const RAMP: Record<string, string> = {
  step: 'skokový náběh', linear: 'lineární náběh', exponential: 'exponenciální náběh',
  sigmoid: 'esovitý náběh', lagged: 'náběh se zpožděním',
}
const DISTRACTOR_KIND: Record<string, string> = {
  seasonality: 'sezónnost', event: 'jednorázová událost', noise: 'šum', red_herring: 'falešná stopa',
}

const parseDate = (s: string | null | undefined) => {
  const m = s?.match(/^(\d{4})-(\d{2})-(\d{2})/)
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) : null
}
const fmtShort = (t: number) => new Intl.DateTimeFormat('cs', { day: 'numeric', month: 'numeric', timeZone: 'UTC' }).format(t)
const startText = (s: CausalStep, r: { t: number | null; how: string }) => r.t === null ? `začátek: ${s.start ?? 'neurčen'}`
  : r.how === 'derived' ? `od ${fmt(r.t)} (${s.start}${s.lag_days ? ` + ${s.lag_days} d` : ''})` : `od ${fmt(r.t)}`
const fmt = (t: number) => new Intl.DateTimeFormat('cs', { day: 'numeric', month: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(t)

/** Resolve each step's start: ISO date as-is, "after sX" = start of sX + lag_days. */
function resolveStarts(chain: CausalStep[], fallback: number | null) {
  const out = new Map<string, { t: number | null; how: 'date' | 'derived' | 'unknown' }>()
  for (const s of chain) {
    const iso = parseDate(s.start)
    const after = s.start?.match(/after\s+(\w+)/i)?.[1]
    const base = after ? out.get(after)?.t ?? null : null
    if (iso !== null) out.set(s.id, { t: iso, how: 'date' })
    else if (base !== null) out.set(s.id, { t: base + s.lag_days * DAY, how: 'derived' })
    else out.set(s.id, { t: fallback, how: 'unknown' })
  }
  return out
}

const shape = (profile: string, x: number) => {
  if (profile === 'step') return 1
  if (profile === 'exponential') return (Math.exp(3 * x) - 1) / (Math.exp(3) - 1)
  if (profile === 'sigmoid') {
    const s = (v: number) => 1 / (1 + Math.exp(-10 * (v - 0.5)))
    return (s(x) - s(0)) / (s(1) - s(0))
  }
  return x
}

const W = 1000
const H = 40

function rampPath(profile: string, from: number, ramp: number) {
  const y = (f: number) => (H - 3 - f * (H - 10)).toFixed(1)
  const pts: string[] = [`M0,${y(0)}`, `L${(from * W).toFixed(1)},${y(0)}`]
  if (profile === 'step') pts.push(`L${(from * W).toFixed(1)},${y(1)}`)
  else for (let i = 1; i <= 32; i++) {
    const x = Math.min(1, from + (ramp * i) / 32)
    pts.push(`L${(x * W).toFixed(1)},${y(shape(profile, i / 32))}`)
  }
  pts.push(`L${W},${y(1)}`)
  return pts.join(' ')
}

function axisTicks({ start, end }: { start: number; end: number }) {
  const ticks: { t: number; label: string; year: boolean }[] = []
  const d = new Date(start)
  const months = (new Date(end).getUTCFullYear() - d.getUTCFullYear()) * 12 + new Date(end).getUTCMonth() - d.getUTCMonth()
  for (let y = d.getUTCFullYear(), m = d.getUTCMonth() + (d.getUTCDate() > 1 ? 1 : 0); ; m++) {
    const t = Date.UTC(y, m, 1)
    if (t > end) break
    const month = new Date(t).getUTCMonth()
    const show = months <= 12 || month % 3 === 0
    ticks.push({ t, year: month === 0, label: month === 0 ? String(new Date(t).getUTCFullYear()) : show ? new Intl.DateTimeFormat('cs', { month: 'short', timeZone: 'UTC' }).format(t) : '' })
  }
  return { ticks: ticks.map((k) => ({ ...k, x: (k.t - start) / (end - start) })) }
}

function Gridlines({ ticks }: { ticks: { x: number; year: boolean }[] }) {
  return <>{ticks.map((k) => <span key={k.x} aria-hidden="true"
    className={`absolute inset-y-0 w-px ${k.year ? 'bg-slate-300' : 'bg-slate-100'}`} style={{ left: `${k.x * 100}%` }} />)}</>
}

export default function StoryTimeline({ chain, distractors, knobs, periodStart, periodEnd, active, onActive, onMechanism, onRemoveDistractor }: {
  chain: CausalStep[]
  distractors: Distractor[]
  knobs: Knob[]
  periodStart: string | null
  periodEnd: string | null
  active: string | null
  onActive: (id: string | null) => void
  onMechanism: (index: number, value: string) => void
  onRemoveDistractor: (index: number) => void
}) {
  const ps = parseDate(periodStart)
  const pe = parseDate(periodEnd)
  const timed = ps !== null && pe !== null && pe > ps
  const span = timed ? pe - ps : 1
  const starts = resolveStarts(chain, ps)
  const rampKnob = knobs.find((k) => k.name === 'ramp_days')
  const rampDays = Number(rampKnob?.default) || 45
  const { ticks } = timed ? axisTicks({ start: ps, end: pe }) : { ticks: [] }
  const pos = (t: number) => Math.min(1, Math.max(0, (t - ps!) / span))
  const lane = 'grid grid-cols-1 gap-x-6 gap-y-2 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)] items-center py-3'

  return (
    <div className="relative">
      {timed && <div className={`${lane} hidden lg:grid py-0 pb-1`} aria-hidden="true">
        <span />
        <div className="relative h-5 text-[11px] text-slate-500 tabular-nums">
          {ticks.filter((k) => k.label).map((k) => (
            <span key={k.t} className={`absolute -translate-x-1/2 ${k.year ? 'font-semibold text-slate-700' : ''}`} style={{ left: `${k.x * 100}%` }}>{k.label}</span>
          ))}
        </div>
      </div>}

      <ol className="divide-y divide-slate-100">
        {chain.map((s, i) => {
          const r = starts.get(s.id)!
          const from = timed && r.t !== null ? pos(r.t) : 0
          const isActive = active === s.id
          return (
            <li key={s.id} className={`${lane} -mx-3 px-3 rounded-lg transition-colors ${isActive ? 'bg-slate-50' : ''}`}
              onMouseEnter={() => onActive(s.id)} onMouseLeave={() => onActive(null)}
              onFocus={() => onActive(s.id)} onBlur={() => onActive(null)}>
              <div className="flex gap-3 min-w-0">
                <span className="text-xs font-semibold text-ink pt-1.5 tabular-nums w-5 shrink-0">{s.id}</span>
                <div className="min-w-0 flex-1">
                  <textarea rows={1} className="input-quiet field-sizing-content resize-none font-medium text-[15px] leading-snug text-slate-900" aria-label={`Mechanismus kroku ${s.id}`}
                    value={s.mechanism} onChange={(e) => onMechanism(i, e.target.value)} />
                  <p className="px-1.5 text-[13px] text-slate-500 leading-snug">{s.expected_impact}</p>
                  <p className="sr-only">{startText(s, r)}, {RAMP[s.ramp_profile] ?? s.ramp_profile}</p>
                </div>
              </div>
              {timed && <div className="relative h-10 ml-8 lg:ml-0 rounded bg-slate-50 overflow-hidden" title={`${startText(s, r)}, ${RAMP[s.ramp_profile] ?? s.ramp_profile}`}>
                <Gridlines ticks={ticks} />
                <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="absolute inset-0 w-full h-full reveal" style={{ animationDelay: `${120 + i * 110}ms` }} aria-hidden="true">
                  <path d={`${rampPath(s.ramp_profile, from, (rampDays * DAY) / span)} L${W},${H} L0,${H} Z`} fill="#ffe14d" fillOpacity="0.55" />
                  <path d={rampPath(s.ramp_profile, from, (rampDays * DAY) / span)} fill="none" stroke="#23262b" strokeWidth="1.5" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
                </svg>
                <span aria-hidden="true" className={`absolute top-1 bottom-1 w-0 border-l ${r.how === 'date' ? 'border-ink' : 'border-ink border-dashed'}`} style={{ left: `${from * 100}%` }} />
                {r.t !== null && <span aria-hidden="true" className={`absolute top-0.5 text-[10px] text-ink tabular-nums ${from > 0.8 ? '-translate-x-full -ml-1' : 'ml-1'}`} style={{ left: `${from * 100}%` }}>{fmtShort(r.t)}</span>}
              </div>}
            </li>
          )
        })}
      </ol>

      {distractors.length > 0 && <div className="mt-5 pt-4 border-t border-slate-200">
        <h3 className="text-sm font-semibold text-slate-800 mb-2">Falešné stopy a šum <span className="font-normal text-slate-500">{distractors.length}</span></h3>
        <ul className="flex flex-wrap gap-2">
          {distractors.map((d, i) => {
            const a = parseDate(d.start)
            return (
              <li key={d.id} title={d.description} className="inline-flex items-center gap-1.5 rounded-md border border-dashed border-slate-300 pl-2.5 pr-1 py-0.5 text-sm">
                <span className="text-slate-800">{d.name}</span>
                <span className="text-xs text-slate-500">{DISTRACTOR_KIND[d.kind] ?? d.kind}{a !== null && `, od ${fmtShort(a)}`}</span>
                <button type="button" className="px-1 text-slate-400 hover:text-red-700 cursor-pointer rounded focus-visible:outline-2 focus-visible:outline-ink"
                  onClick={() => onRemoveDistractor(i)} aria-label={`Odebrat ${d.name}`}>×</button>
              </li>
            )
          })}
        </ul>
      </div>}
    </div>
  )
}
