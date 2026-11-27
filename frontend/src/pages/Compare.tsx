import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, fmt, type Json } from '../api'
import Chart, { B_SUFFIX, label } from '../components/Chart'

/** Build overlay chart specs (A solid, B dashed) for one evidence query. */
function overlays(e: Json): { title: string; spec: Json }[] {
  const A = e.a, B = e.b
  if (!A?.rows?.length || !B?.rows?.length) return []
  const xi = (cols: string[]) => cols.indexOf(e.x ?? cols[0])
  if (e.series) {
    const si = A.columns.indexOf(e.series)
    const names = new Set(A.rows.map((r: Json[]) => r[si]))
    if (names.size > 3 || e.chart === 'stacked_bar') return []
    const yi = A.columns.indexOf(e.y[0])
    const rows = [
      ...A.rows.map((r: Json[]) => [r[xi(A.columns)], r[si], r[yi]]),
      ...B.rows.map((r: Json[]) => [r[B.columns.indexOf(e.x)], r[B.columns.indexOf(e.series)] + B_SUFFIX, r[B.columns.indexOf(e.y[0])]]),
    ]
    return [{ title: e.title, spec: { columns: [e.x, e.series, e.y[0]], rows, chart: e.chart === 'bar' ? 'bar' : 'line', x: e.x, y: [e.y[0]], series: e.series } }]
  }
  return (e.y as string[]).map((y) => {
    const map = new Map<Json, Json[]>()
    A.rows.forEach((r: Json[]) => map.set(r[xi(A.columns)], [r[xi(A.columns)], r[A.columns.indexOf(y)], null]))
    B.rows.forEach((r: Json[]) => {
      const x = r[xi(B.columns)]
      const row = map.get(x) ?? [x, null, null]
      row[2] = r[B.columns.indexOf(y)]
      map.set(x, row)
    })
    return { title: `${e.title} – ${label(y)}`, spec: { columns: [e.x, y, y + B_SUFFIX], rows: Array.from(map.values()), chart: e.chart === 'bar' ? 'bar' : 'line', x: e.x, y: [y, y + B_SUFFIX] } }
  })
}

export default function Compare() {
  const [p] = useSearchParams()
  const a = p.get('a')!, b = p.get('b')!
  const [d, setD] = useState<Json>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { api.compare(a, b).then(setD).catch((e) => setErr(String(e))) }, [a, b])
  if (err) return <div className="text-red-700">{err}</div>
  if (!d) return <div className="text-slate-500">Porovnávám…</div>
  return (
    <div className="space-y-5">
      <div className="card p-5">
        <h1 className="text-xl font-bold mb-3">Porovnání běhů (what-if)</h1>
        <div className="grid md:grid-cols-2 gap-4 text-sm">
          {(['a', 'b'] as const).map((k) => (
            <div key={k} className="border border-slate-200 rounded-lg p-3">
              <div className="text-xs text-slate-500">{k === 'a' ? 'A – plná čára' : 'B – přerušovaná čára'}</div>
              <Link className="font-medium text-blue-700 mono" to={`/runs/${d[k].run_id}`}>{d[k].run_id}</Link>
              <div>{d[k].label} · verdikt {d[k].verdict?.verdict}</div>
            </div>
          ))}
        </div>
        <ul className="mt-4 text-sm space-y-1">{d.explanation.map((x: string) => <li key={x}>• {x}</li>)}</ul>
      </div>
      <div className="card p-5">
        <h2 className="font-semibold mb-2">Klíčové metriky</h2>
        <table className="data">
          <thead><tr><th>Metrika</th><th className="text-right">A</th><th className="text-right">B</th><th className="text-right">Rozdíl</th><th className="text-right">%</th></tr></thead>
          <tbody>{d.metrics.map((m: Json) => (
            <tr key={m.key}><td>{m.label}</td><td className="text-right tabular-nums">{fmt(m.a)}</td><td className="text-right tabular-nums">{fmt(m.b)}</td>
              <td className="text-right tabular-nums">{m.diff === null ? '–' : (m.diff > 0 ? '+' : '') + fmt(m.diff)}</td>
              <td className={`text-right tabular-nums ${m.diff_pct && Math.abs(m.diff_pct) >= 5 ? 'font-semibold' : 'text-slate-500'}`}>{m.diff_pct === null ? '–' : `${m.diff_pct > 0 ? '+' : ''}${m.diff_pct} %`}</td></tr>
          ))}</tbody>
        </table>
        <div className="text-xs text-slate-500 mt-2">Metriky obou běhů měří stejné entity (dodavatel z běhu A).</div>
      </div>
      <h2 className="font-semibold text-lg">Překryté grafy důkazních dotazů</h2>
      <div className="grid lg:grid-cols-2 gap-4">
        {d.evidence.flatMap((e: Json) => {
          const ov = overlays(e)
          if (ov.length) return ov.map((o, i) => <div key={e.id + i} className="card p-4"><div className="font-medium text-sm">{o.title}</div><Chart spec={o.spec} height={240} /></div>)
          return [(
            <div key={e.id} className="card p-4 lg:col-span-2"><div className="font-medium text-sm mb-1">{e.title}</div>
              <div className="grid md:grid-cols-2 gap-3">
                <div><div className="text-xs text-slate-500">A</div><Chart spec={{ ...e.a, chart: e.chart, x: e.x, y: e.y, series: e.series }} height={230} /></div>
                <div><div className="text-xs text-slate-500">B</div><Chart spec={{ ...e.b, chart: e.chart, x: e.x, y: e.y, series: e.series }} height={230} /></div>
              </div>
            </div>)]
        })}
      </div>
    </div>
  )
}
