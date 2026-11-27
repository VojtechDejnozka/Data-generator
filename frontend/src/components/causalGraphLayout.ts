// Pure layout of an SCM diagram: variables in causal layers (longest path over within-step edges),
// left = causes, right = effects. Edges: forward S-curves; feedback through a time lag is routed
// below the graph. No rendering here (see CausalGraph.tsx).
import { type Json } from '../api'
import { SERIES } from './palette'

export const DX = 178
export const DY = 70
export const PAD = { left: 80, right: 90, top: 26, bottom: 16 }
export const R = 8

export interface GNode {
  id: string; entity: string; color: string; x: number; y: number; latent: boolean; evented: boolean
  selfLag: boolean; tip: string[]
}
export interface GEdge {
  id: string; source: string; target: string; path: string; lx: number; ly: number; label: string
  lag: boolean; back: boolean; tip: string
}
export interface GraphLayout { nodes: GNode[]; edges: GEdge[]; width: number; height: number; entities: { id: string; color: string }[] }

export function entityColors(scm: Json): Map<string, string> {
  return new Map(scm.entities.map((e: Json, i: number) => [e.entity_id, SERIES[i] ?? '#9c9c9c']))
}

export function layoutGraph(scm: Json, edgesIn: Json[], mechanisms: Record<string, string>,
                            showLabels: boolean, hidden: Set<string>): GraphLayout {
  const colors = entityColors(scm)
  const vars: Json[] = scm.variables.filter((v: Json) => (showLabels || (v.role !== 'label' && v.role !== 'text'))
    && !hidden.has(v.entity))
  const ids = new Set(vars.map((v) => v.variable_id))
  const edges = edgesIn.filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target)
  const selfLag = new Set(edgesIn.filter((e) => e.source === e.target).map((e) => e.source))
  const evented = new Set<string>(scm.events.flatMap((e: Json) => e.affected_variables))
  const parents = new Map<string, string[]>([...ids].map((id) => [id, []]))
  for (const e of edges) if (!e.lag) parents.get(e.target)!.push(e.source)
  const rank = new Map<string, number>()
  const visit = (id: string, depth: number): number => {
    if (rank.has(id)) return rank.get(id)!
    if (depth > ids.size) return 0 // guard; validated specs are acyclic within a time step
    const r = Math.max(-1, ...parents.get(id)!.map((p) => visit(p, depth + 1))) + 1
    rank.set(id, r)
    return r
  }
  for (const id of ids) visit(id, 0)
  const layers: string[][] = []
  for (const [id, r] of rank) (layers[r] ??= []).push(id)
  const entOf = new Map(vars.map((v) => [v.variable_id, v.entity]))
  const entOrder = new Map(scm.entities.map((e: Json, i: number) => [e.entity_id, i]))
  const slot = new Map<string, number>()
  const maxLayer = Math.max(1, ...layers.map((l) => l?.length ?? 0))
  layers.forEach((layer, li) => {
    if (!layer) return
    const bary = (id: string) => {
      const ps = (parents.get(id) ?? []).filter((p) => slot.has(p))
      return ps.length ? ps.reduce((s, p) => s + slot.get(p)!, 0) / ps.length : (entOrder.get(entOf.get(id)) as number)
    }
    layer.sort((a, b) => (li ? bary(a) - bary(b) : 0) || ((entOrder.get(entOf.get(a)) as number) - (entOrder.get(entOf.get(b)) as number)) || a.localeCompare(b))
    const off = (maxLayer - layer.length) / 2
    layer.forEach((id, i) => slot.set(id, i + off))
  })
  const pos = (id: string) => ({ x: PAD.left + (rank.get(id) ?? 0) * DX, y: PAD.top + 14 + (slot.get(id) ?? 0) * DY })
  const nodes: GNode[] = vars.map((v) => {
    const p = pos(v.variable_id)
    const latent = v.observability === 'latent' || !v.column
    return {
      id: v.variable_id, entity: v.entity, color: colors.get(v.entity) ?? '#9c9c9c', ...p, latent,
      evented: evented.has(v.variable_id), selfLag: selfLag.has(v.variable_id),
      tip: [`${v.variable_id} (${v.entity}${v.column ? ` · sloupec ${v.column}` : ''})`,
            `${latent ? 'latentní / nematerializovaná' : 'pozorovaná'} · ${v.data_type}`,
            mechanisms[v.variable_id] ?? v.mechanism.type,
            ...(evented.has(v.variable_id) ? ['⚡ mění ji událost'] : [])],
    }
  })
  const bottom = PAD.top + 14 + (maxLayer - 1) * DY
  const out: GEdge[] = edges.map((e) => {
    const s = pos(e.source), t = pos(e.target)
    const back = (rank.get(e.target) ?? 0) <= (rank.get(e.source) ?? 0)
    let path: string, lx: number, ly: number
    if (!back) {
      const x1 = s.x + R, x2 = t.x - R - 3, mx = (x1 + x2) / 2
      path = `M${x1},${s.y} C${mx},${s.y} ${mx},${t.y} ${x2},${t.y}`
      lx = mx; ly = (s.y + t.y) / 2 - 4
    } else { // feedback over time: loop under the graph, right-to-left
      const yb = bottom + 30 + Math.min(40, Math.abs(s.x - t.x) / 12)
      path = `M${s.x},${s.y + R} C${s.x},${yb} ${t.x},${yb} ${t.x},${t.y + R + 3}`
      // midpoint of the cubic (both control points at yb) – the label sits on the curve
      lx = (s.x + t.x) / 2; ly = 0.75 * yb + 0.125 * (s.y + t.y + 2 * R) - 5
    }
    const sign = e.effect_direction === '+' ? '+' : e.effect_direction === '-' ? '−' : ''
    return {
      id: `${e.source}>${e.target}`, source: e.source, target: e.target, path, lx, ly, lag: !!e.lag, back,
      label: [sign, e.lag ? `t−${e.lag}` : '', e.aggregation ? `[${e.aggregation}]` : ''].filter(Boolean).join(' '),
      tip: `${e.source} → ${e.target}${e.lag ? ` · zpoždění ${e.lag} krok(y)` : ''}${e.aggregation ? ` · agregace ${e.aggregation}` : ''}`
        + `${e.optional_coefficient != null ? ` · koeficient ${e.optional_coefficient}` : ''} · ${e.entity_mapping}`,
    }
  })
  const hasBack = out.some((e) => e.back)
  const maxRank = Math.max(0, ...rank.values())
  return {
    nodes, edges: out, width: PAD.left + maxRank * DX + PAD.right,
    height: bottom + (hasBack ? 80 : 0) + PAD.bottom,
    entities: scm.entities.map((e: Json) => ({ id: e.entity_id, color: colors.get(e.entity_id)! })),
  }
}
