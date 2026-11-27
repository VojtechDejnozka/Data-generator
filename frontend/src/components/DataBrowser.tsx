import { useEffect, useMemo, useState } from 'react'
import { api, fmt, type Json } from '../api'
import { StrategyChip } from '../pages/SpecReview'

interface View { table: string; filter_col?: string; filter_val?: string; filter_op?: string; sort?: string; desc?: boolean; page: number }

function Erd({ schema, onPick }: { schema: Json; onPick: (t: string) => void }) {
  // Layered layout by FK depth; boxes per layer, cubic edges child -> parent.
  const layout = useMemo(() => {
    const parents: Record<string, string[]> = {}
    schema.tables.forEach((t: Json) => (parents[t.name] = []))
    schema.edges.forEach((e: Json) => e.from !== e.to && parents[e.from]?.push(e.to))
    const depth: Record<string, number> = {}
    const d = (n: string, seen = new Set<string>()): number => {
      if (depth[n] !== undefined) return depth[n]
      if (seen.has(n)) return 0
      seen.add(n)
      depth[n] = parents[n].length ? Math.max(...parents[n].map((p) => d(p, seen))) + 1 : 0
      return depth[n]
    }
    schema.tables.forEach((t: Json) => d(t.name))
    const layers: string[][] = []
    Object.entries(depth).forEach(([n, l]) => (layers[l] ??= []).push(n))
    const W = 170, H = 34, GX = 60, GY = 14
    const pos: Record<string, { x: number; y: number }> = {}
    layers.forEach((ns, li) => ns.sort().forEach((n, i) => (pos[n] = { x: li * (W + GX) + 10, y: i * (H + GY) + 10 })))
    const width = layers.length * (W + GX)
    const height = Math.max(...layers.map((l) => l.length)) * (H + GY) + 20
    return { pos, W, H, width, height }
  }, [schema])
  const { pos, W, H, width, height } = layout
  const seenEdges = new Set<string>()
  return (
    <svg width={width} height={height} className="max-w-full">
      {schema.edges.map((e: Json, i: number) => {
        const key = `${e.from}-${e.to}`
        if (seenEdges.has(key) || !pos[e.from] || !pos[e.to] || e.from === e.to) return null
        seenEdges.add(key)
        const a = pos[e.from], b = pos[e.to]
        const x1 = a.x, y1 = a.y + H / 2, x2 = b.x + W, y2 = b.y + H / 2
        return <path key={i} d={`M${x1},${y1} C${x1 - 30},${y1} ${x2 + 30},${y2} ${x2},${y2}`} fill="none" stroke="#9dc8f7" strokeWidth={1.5} />
      })}
      {schema.tables.map((t: Json) => (
        <g key={t.name} transform={`translate(${pos[t.name].x},${pos[t.name].y})`} className="cursor-pointer" onClick={() => onPick(t.name)}>
          <rect width={W} height={H} rx={6} fill="#fff" stroke={t.strategy === 'simulation' ? '#6a4ce2' : '#00b894'} strokeWidth={1.5} />
          <text x={8} y={15} fontSize={12} fontWeight={600} fill="#191919" fontFamily="ui-monospace">{t.name}</text>
          <text x={8} y={28} fontSize={10} fill="#666">{fmt(t.rows)} řádků · {t.columns.length} sl.</text>
        </g>
      ))}
    </svg>
  )
}

export default function DataBrowser({ runId, schema }: { runId: string; schema: Json }) {
  const [view, setView] = useState<View>({ table: schema.tables[0]?.name, page: 1 })
  const [data, setData] = useState<Json>(null)
  const [err, setErr] = useState<string | null>(null)
  const [history, setHistory] = useState<View[]>([])
  const [selRow, setSelRow] = useState<number | null>(null)
  const [fc, setFc] = useState('')
  const [fv, setFv] = useState('')
  const [fo, setFo] = useState('eq')
  const [showErd, setShowErd] = useState(false)

  useEffect(() => {
    setErr(null)
    api.table(runId, view.table, { page: view.page, page_size: 50, sort: view.sort, desc: view.desc, filter_col: view.filter_col, filter_val: view.filter_val, filter_op: view.filter_op })
      .then((d) => { setData(d); setSelRow(null) }).catch((e) => setErr(String(e)))
  }, [runId, view])

  const go = (v: View) => { setHistory((h) => [...h, view]); setView(v); setFc(v.filter_col ?? ''); setFv(v.filter_val ?? ''); setFo(v.filter_op ?? 'eq') }
  const meta = schema.tables.find((t: Json) => t.name === view.table)
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1

  return (
    <div className="grid lg:grid-cols-5 gap-4">
      <div className="card p-3 lg:col-span-1 self-start">
        <div className="flex items-center mb-2"><h3 className="font-semibold text-sm flex-1">Tabulky</h3>
          <button className="text-xs text-blue-700 cursor-pointer" onClick={() => setShowErd(!showErd)}>{showErd ? 'skrýt ERD' : 'ERD'}</button></div>
        {schema.tables.map((t: Json) => (
          <button key={t.name} onClick={() => go({ table: t.name, page: 1 })}
            className={`w-full text-left px-2 py-1.5 rounded text-sm cursor-pointer ${t.name === view.table ? 'bg-blue-50 text-blue-800' : 'hover:bg-slate-50'}`}>
            <div className="mono">{t.name}</div><div className="text-[11px] text-slate-500">{fmt(t.rows)} řádků</div>
          </button>
        ))}
      </div>
      <div className="lg:col-span-4 space-y-3 min-w-0">
        {showErd && <div className="card p-3 overflow-auto"><div className="text-xs text-slate-500 mb-1">Schéma (fialová = simulace, zelená = LLM). Šipky vedou od tabulky s FK k odkazované tabulce. Klik otevře tabulku.</div><Erd schema={schema} onPick={(t) => go({ table: t, page: 1 })} /></div>}
        <div className="card p-3">
          <div className="flex flex-wrap items-center gap-2 mb-2">
            {history.length > 0 && <button className="btn text-xs" onClick={() => { const h = [...history]; const p = h.pop()!; setHistory(h); setView(p) }}>← zpět</button>}
            <h3 className="font-semibold mono">{view.table}</h3>
            {meta && <StrategyChip s={meta.strategy} />}
            <span className="text-xs text-slate-500 flex-1 truncate">{meta?.description}</span>
            <select className="input py-1 text-xs" style={{ width: 170 }} value={fc} onChange={(e) => setFc(e.target.value)}>
              <option value="">— filtr sloupec —</option>{data?.columns.map((c: string) => <option key={c}>{c}</option>)}
            </select>
            <select className="input py-1 text-xs" style={{ width: 100 }} value={fo} onChange={(e) => setFo(e.target.value)}>
              <option value="eq">=</option><option value="contains">obsahuje</option><option value="gte">≥</option><option value="lte">≤</option>
            </select>
            <input className="input py-1 text-xs" style={{ width: 150 }} value={fv} onChange={(e) => setFv(e.target.value)} placeholder="hodnota" onKeyDown={(e) => e.key === 'Enter' && go({ ...view, filter_col: fc, filter_val: fv, filter_op: fo, page: 1 })} />
            <button className="btn text-xs" onClick={() => go({ ...view, filter_col: fc, filter_val: fv, filter_op: fo, page: 1 })}>Filtrovat</button>
            {view.filter_col && <button className="btn text-xs" onClick={() => go({ table: view.table, page: 1 })}>✕</button>}
          </div>
          {err && <div className="text-red-700 text-sm">{err}</div>}
          {data && (
            <>
              <div className="overflow-auto max-h-[60vh] border border-slate-200 rounded">
                <table className="data">
                  <thead><tr>{data.columns.map((c: string) => (
                    <th key={c} className="cursor-pointer select-none" onClick={() => setView({ ...view, sort: c, desc: view.sort === c ? !view.desc : false, page: 1 })}>
                      {data.pk.includes(c) && '🔑 '}{c}{data.fks[c] && <span className="text-blue-600" title={data.fks[c]}> ↗</span>}{view.sort === c ? (view.desc ? ' ▼' : ' ▲') : ''}
                    </th>))}</tr></thead>
                  <tbody>
                    {data.rows.map((row: Json[], i: number) => (
                      <tr key={i} className={selRow === i ? 'bg-blue-50' : 'hover:bg-slate-50'} onClick={() => setSelRow(i)}>
                        {row.map((v, j) => {
                          const col = data.columns[j]
                          const fk = data.fks[col]
                          if (fk && v !== null) {
                            const [rt, rc] = fk.split('.')
                            return <td key={j}><button className="text-blue-700 underline decoration-dotted cursor-pointer tabular-nums" title={`→ ${fk}`} onClick={(e) => { e.stopPropagation(); go({ table: rt, filter_col: rc, filter_val: String(v), page: 1 }) }}>{fmt(v)}</button></td>
                          }
                          return <td key={j} className={`${typeof v === 'number' ? 'text-right tabular-nums' : ''} ${typeof v === 'string' && v.length > 60 ? 'min-w-80' : ''}`}>{fmt(v)}</td>
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex items-center gap-2 mt-2 text-sm">
                <button className="btn text-xs" disabled={view.page <= 1} onClick={() => setView({ ...view, page: view.page - 1 })}>‹</button>
                <span>strana {view.page} / {pages}</span>
                <button className="btn text-xs" disabled={view.page >= pages} onClick={() => setView({ ...view, page: view.page + 1 })}>›</button>
                <span className="text-slate-500 ml-2">{fmt(data.total)} řádků</span>
                {selRow !== null && data.referenced_by.length > 0 && data.pk.length === 1 && (
                  <span className="ml-auto flex flex-wrap gap-1 items-center text-xs">Odkazováno z:
                    {data.referenced_by.map((r: Json) => {
                      const pkVal = data.rows[selRow][data.columns.indexOf(r.ref)]
                      return <button key={r.table + r.column} className="chip bg-blue-50 text-blue-800 cursor-pointer" onClick={() => go({ table: r.table, filter_col: r.column, filter_val: String(pkVal), page: 1 })}>{r.table}.{r.column}</button>
                    })}
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
