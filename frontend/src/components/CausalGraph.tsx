// SVG diagram of an executable SCM. Colour = entity (fixed categorical order, legend + text labels, so
// identity is never colour-alone). Secondary encodings: hollow dashed node = latent, diamond + ⚡ = changed
// by an event, dashed edge = time lag (t−k), edge label = sign / aggregation, ↻ = depends on its own past.
import { useMemo, useRef, useState } from 'react'
import { type Json } from '../api'
import { R, layoutGraph } from './causalGraphLayout'

const TEXT = '#191919'
const TEXT2 = '#666666'
const EDGE = '#a8a8a8'
const SURFACE = '#ffffff'

export interface CausalGraphProps {
  scm: Json
  edges: Json[]
  mechanisms: Record<string, string>
  showLabels: boolean
  selected?: string | null
  onSelect?: (id: string) => void
}

export default function CausalGraph({ scm, edges, mechanisms, showLabels, selected, onSelect }: CausalGraphProps) {
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [hover, setHover] = useState<string | null>(null)
  const [tip, setTip] = useState<{ x: number; y: number; lines: string[] } | null>(null)
  const [zoom, setZoom] = useState(1)
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null)
  const g = useMemo(() => layoutGraph(scm, edges, mechanisms, showLabels, hidden),
    [scm, edges, mechanisms, showLabels, hidden])

  const near = useMemo(() => {
    if (!hover) return null
    const s = new Set([hover])
    for (const e of g.edges) if (e.source === hover || e.target === hover) { s.add(e.source); s.add(e.target) }
    return s
  }, [hover, g])
  const toggle = (id: string) => setHidden((h) => { const n = new Set(h); if (n.has(id)) n.delete(id); else n.add(id); return n })
  const showTip = (ev: React.MouseEvent, lines: string[]) => {
    const box = (ev.currentTarget as Element).closest('[data-graph]')!.getBoundingClientRect()
    setTip({ x: ev.clientX - box.left + 12, y: ev.clientY - box.top + 12, lines })
  }

  return (
    <div data-graph className="relative">
      <div className="flex flex-wrap gap-x-3 gap-y-1 px-2 pt-2 text-[11px]" style={{ color: TEXT2 }}>
        {g.entities.map((e) => (
          <button key={e.id} onClick={() => toggle(e.id)} title="Skrýt / zobrazit entitu"
            className={`flex items-center gap-1 cursor-pointer ${hidden.has(e.id) ? 'opacity-40 line-through' : ''}`}>
            <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: e.color }} />{e.id}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2 px-2 py-1">
        <button className="btn text-xs" aria-label="Oddálit graf" disabled={zoom <= 0.4}
          onClick={() => setZoom(Math.max(0.4, zoom - 0.2))}>−</button>
        <button className="btn text-xs" aria-label="Výchozí přiblížení" onClick={() => setZoom(1)}>{Math.round(zoom * 100)} %</button>
        <button className="btn text-xs" aria-label="Přiblížit graf" disabled={zoom >= 2.4}
          onClick={() => setZoom(Math.min(2.4, zoom + 0.2))}>+</button>
      </div>
      <div className="overflow-auto max-h-[32rem] cursor-grab active:cursor-grabbing"
        onPointerDown={(ev) => {
          if (ev.pointerType !== 'mouse' || ev.button !== 0 || (ev.target as Element).closest('[data-graph-node]')) return
          const el = ev.currentTarget
          drag.current = { x: ev.clientX, y: ev.clientY, left: el.scrollLeft, top: el.scrollTop }
          el.setPointerCapture(ev.pointerId)
          setTip(null)
        }}
        onPointerMove={(ev) => {
          if (!drag.current) return
          const { x, y, left, top } = drag.current
          ev.currentTarget.scrollTo(left + x - ev.clientX, top + y - ev.clientY)
        }}
        onLostPointerCapture={() => { drag.current = null }}>
        <svg width={g.width * zoom} height={g.height * zoom} viewBox={`0 0 ${g.width} ${g.height}`} role="img"
          aria-label="Diagram kauzálního modelu: příčiny vlevo, následky vpravo" style={{ display: 'block' }}
          onMouseLeave={() => { setHover(null); setTip(null) }}>
          <defs>
            {[['a', EDGE], ['h', TEXT]].map(([k, c]) => (
              <marker key={k} id={`arrow-${k}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
                orient="auto-start-reverse"><path d="M0,1 L10,5 L0,9 z" fill={c} /></marker>
            ))}
          </defs>
          {g.edges.map((e) => {
            const on = near ? (e.source === hover || e.target === hover) : true
            const strong = !!near && on
            return (
              <g key={e.id} opacity={on ? 1 : 0.15}>
                <path d={e.path} fill="none" stroke="transparent" strokeWidth={10}
                  onMouseMove={(ev) => showTip(ev, [e.tip])} onMouseLeave={() => setTip(null)} />
                <path d={e.path} fill="none" stroke={strong ? TEXT : EDGE} strokeWidth={strong ? 2 : 1.5}
                  strokeDasharray={e.lag ? '5 4' : undefined} markerEnd={`url(#arrow-${strong ? 'h' : 'a'})`}
                  pointerEvents="none" />
                {e.label && <text x={e.lx} y={e.ly} fontSize={10} fill={TEXT2} textAnchor="middle"
                  paintOrder="stroke" stroke={SURFACE} strokeWidth={3} pointerEvents="none">{e.label}</text>}
              </g>
            )
          })}
          {g.nodes.map((n) => {
            const on = !near || near.has(n.id)
            const label = `${n.evented ? '⚡ ' : ''}${n.id}${n.selfLag ? ' ↻' : ''}`
            return (
              <g key={n.id} data-graph-node opacity={on ? 1 : 0.25} style={{ cursor: onSelect ? 'pointer' : 'default' }}
                onMouseEnter={() => setHover(n.id)} onMouseMove={(ev) => showTip(ev, n.tip)}
                onMouseLeave={() => { setHover(null); setTip(null) }} onClick={() => onSelect?.(n.id)}>
                <circle cx={n.x} cy={n.y} r={R + 8} fill="transparent" />
                {selected === n.id && <circle cx={n.x} cy={n.y} r={R + 6} fill="none" stroke={TEXT} strokeWidth={2} />}
                {n.evented
                  ? <path d={`M${n.x},${n.y - R - 3} L${n.x + R + 3},${n.y} L${n.x},${n.y + R + 3} L${n.x - R - 3},${n.y} z`}
                      fill={n.latent ? SURFACE : n.color} stroke={n.latent ? n.color : SURFACE} strokeWidth={2}
                      strokeDasharray={n.latent ? '3 2' : undefined} />
                  : <circle cx={n.x} cy={n.y} r={R} fill={n.latent ? SURFACE : n.color}
                      stroke={n.latent ? n.color : SURFACE} strokeWidth={2} strokeDasharray={n.latent ? '3 2' : undefined} />}
                <text x={n.x} y={n.y - R - 7} fontSize={11} textAnchor="middle" fill={n.latent ? TEXT2 : TEXT}
                  fontStyle={n.latent ? 'italic' : 'normal'} paintOrder="stroke" stroke={SURFACE} strokeWidth={3}>{label}</text>
              </g>
            )
          })}
        </svg>
      </div>
      {tip && (
        <div className="absolute z-10 max-w-md rounded border border-slate-200 bg-white px-2 py-1 text-[11px] shadow"
          style={{ left: tip.x, top: tip.y, color: TEXT, pointerEvents: 'none' }}>
          {tip.lines.map((l, i) => <div key={i} className={i === 2 ? 'mono text-slate-700' : i ? 'text-slate-500' : 'font-semibold'}>{l}</div>)}
        </div>
      )}
    </div>
  )
}
