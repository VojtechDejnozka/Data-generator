// Deterministic chart renderer: (columns, rows, chart type, x, y, series) -> ECharts option.
// Palette = validated reference categorical order (dataviz skill); colour follows the entity name.
import * as echarts from 'echarts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { fmt, type Json } from '../api'

import { SERIES } from './palette'
export { SERIES }
const TEXT2 = '#6b7078'
const GRID = '#e3e5e8'
export const B_SUFFIX = ' · B'

export interface ChartSpec {
  columns: string[]
  rows: Json[][]
  chart: string
  x?: string | null
  y?: string[]
  series?: string | null
  title?: string
}

const baseName = (s: string) => (s.endsWith(B_SUFFIX) ? s.slice(0, -B_SUFFIX.length) : s)

function colorMap(names: string[]): Record<string, string> {
  const bases = Array.from(new Set(names.map(baseName))).sort((a, b) => a.localeCompare(b, 'cs'))
  const m: Record<string, string> = {}
  bases.forEach((b, i) => (m[b] = i < SERIES.length ? SERIES[i] : '#9c9c9c'))
  return m
}

interface Series { name: string; data: (number | null)[] }

function shape(spec: ChartSpec): { xs: Json[]; series: Series[] } {
  const ci = (c: string) => spec.columns.indexOf(c)
  const xCol = spec.x && ci(spec.x) >= 0 ? spec.x : spec.columns[0]
  const xi = ci(xCol)
  const ys = (spec.y && spec.y.length ? spec.y : spec.columns.filter((c) => c !== xCol)).filter((c) => ci(c) >= 0)
  const xs: Json[] = []
  const seen = new Set()
  for (const r of spec.rows) if (!seen.has(r[xi])) { seen.add(r[xi]); xs.push(r[xi]) }
  if (spec.chart === 'line' || spec.chart === 'area') xs.sort((a, b) => String(a).localeCompare(String(b)))
  const xIndex = new Map(xs.map((x, i) => [x, i]))
  if (spec.series && ci(spec.series) >= 0) {
    const si = ci(spec.series)
    const yi = ci(ys[0])
    const by = new Map<string, (number | null)[]>()
    for (const r of spec.rows) {
      const k = String(r[si])
      if (!by.has(k)) by.set(k, Array(xs.length).fill(null))
      by.get(k)![xIndex.get(r[xi])!] = r[yi]
    }
    return { xs, series: Array.from(by.entries()).map(([name, data]) => ({ name, data })) }
  }
  return {
    xs,
    series: ys.map((y) => {
      const data = Array(xs.length).fill(null)
      const yi = ci(y)
      for (const r of spec.rows) data[xIndex.get(r[xi])!] = r[yi]
      return { name: y, data }
    }),
  }
}

/** Should several y measures be split into small multiples (never a dual axis)? */
function needsFacets(series: Series[], hasSeriesCol: boolean, chart: string): boolean {
  // Only for line charts of measures in different units (e.g. visitors vs conversion %).
  if (hasSeriesCol || series.length < 2 || chart !== 'line') return false
  const maxes = series.map((s) => Math.max(...s.data.map((v) => Math.abs(v ?? 0)), 1e-9))
  return Math.max(...maxes) / Math.min(...maxes) > 15
}

const LABELS: Record<string, string> = {
  revenue_k_czk: 'Tržby (tis. Kč)', transactions: 'Nákupy', visitors: 'Návštěvnost', conversion_pct: 'Konverze (%)',
  avg_delay_days: 'Prům. zpoždění (dny)', oos_pct: 'Dny bez zásoby (%)', revenue_index: 'Index tržeb (před = 100)',
  footfall_change_pct: 'Změna návštěvnosti (%)', revenue_change_pct: 'Změna tržeb (%)', complaints: 'Stížnosti',
  per_month_before: 'Za měsíc – před', per_month_after: 'Za měsíc – po', value: 'Hodnota', n: 'Počet',
}
export const label = (c: string): string => (c.endsWith(B_SUFFIX) ? label(baseName(c)) + B_SUFFIX : LABELS[c] ?? c)

function buildOption(spec: ChartSpec): echarts.EChartsCoreOption {
  const { xs, series } = shape(spec)
  const colors = colorMap(series.map((s) => s.name))
  const isLine = spec.chart === 'line' || spec.chart === 'area'
  const axisCommon = {
    axisLine: { lineStyle: { color: GRID } }, axisTick: { show: false },
    axisLabel: { color: TEXT2, fontSize: 11 }, splitLine: { lineStyle: { color: GRID } },
  }
  const tooltip = {
    trigger: isLine ? 'axis' : 'item', confine: true,
    axisPointer: { type: isLine ? 'line' : 'shadow' },
    valueFormatter: (v: Json) => fmt(v),
  }
  const legend = series.length > 1
    ? { type: 'scroll', top: 0, textStyle: { color: '#23262b', fontSize: 11 }, itemWidth: 14, itemHeight: 8 }
    : undefined

  if (spec.chart === 'pie') {
    return {
      color: SERIES, tooltip: { trigger: 'item', valueFormatter: (v: Json) => fmt(v) },
      series: [{
        type: 'pie', radius: ['45%', '72%'], itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: { formatter: '{b}: {d} %', color: '#23262b' },
        data: xs.map((x, i) => ({ name: String(x), value: series[0]?.data[i] ?? 0 })),
      }],
    }
  }

  if (spec.chart === 'heatmap') {
    const names = series.map((s) => s.name)
    const data: Json[] = []
    let max = 0
    series.forEach((s, yi) => s.data.forEach((v, xi) => { if (v !== null) { data.push([xi, yi, v]); max = Math.max(max, v) } }))
    return {
      tooltip: { position: 'top', formatter: (p: Json) => `${names[p.value[1]]}<br/>${xs[p.value[0]]}: <b>${fmt(p.value[2])}</b>` },
      grid: { left: 8, right: 16, top: 8, bottom: 56, containLabel: true },
      xAxis: { type: 'category', data: xs, ...axisCommon, splitLine: { show: false } },
      yAxis: { type: 'category', data: names, ...axisCommon, splitLine: { show: false } },
      visualMap: { min: 0, max: max || 1, calculable: true, orient: 'horizontal', left: 'center', bottom: 0, itemHeight: 120,
        inRange: { color: ['#cce3fd', '#84b9f3', '#338ced', '#1b64be', '#0e3d7e'] }, textStyle: { color: TEXT2 } },
      series: [{ type: 'heatmap', data, itemStyle: { borderColor: '#fff', borderWidth: 2 } }],
    }
  }

  const facet = needsFacets(series, !!spec.series, spec.chart)
  if (facet) {
    const n = series.length
    const h = 100 / n
    return {
      tooltip: { ...tooltip, trigger: 'axis' },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      title: series.map((s, i) => ({ text: label(s.name), top: `${i * h}%`, right: 16, textStyle: { fontSize: 11, color: TEXT2, fontWeight: 600 } })),
      grid: series.map((_, i) => ({ left: 8, right: 16, top: `${i * h + 6}%`, height: `${h - 13}%`, containLabel: true })),
      xAxis: series.map((_, i) => ({ type: 'category', data: xs, gridIndex: i, ...axisCommon, axisLabel: { ...axisCommon.axisLabel, show: i === n - 1 } })),
      yAxis: series.map((_, i) => ({ type: 'value', gridIndex: i, ...axisCommon, splitNumber: 2 })),
      series: series.map((s, i) => ({
        name: label(s.name), type: 'line', xAxisIndex: i, yAxisIndex: i, data: s.data, showSymbol: false, symbolSize: 8,
        lineStyle: { width: 2 }, itemStyle: { color: SERIES[i] }, connectNulls: true,
      })),
    }
  }

  const stacked = spec.chart === 'stacked_bar'
  const type = isLine ? 'line' : spec.chart === 'scatter' ? 'scatter' : 'bar'
  return {
    tooltip, legend,
    grid: { left: 8, right: 16, top: legend ? 32 : 12, bottom: 8, containLabel: true },
    xAxis: { type: 'category', data: xs, ...axisCommon, splitLine: { show: false } },
    yAxis: { type: 'value', ...axisCommon, name: series.length === 1 || spec.series ? label(spec.y?.[0] ?? '') : '',
      nameTextStyle: { color: TEXT2, fontSize: 11, align: 'left' } },
    series: series.map((s) => {
      const isB = s.name.endsWith(B_SUFFIX)
      const c = colors[baseName(s.name)]
      return {
        name: label(s.name), type, data: s.data, stack: stacked ? 'total' : undefined,
        showSymbol: false, symbolSize: 8, connectNulls: true,
        lineStyle: { width: 2, type: isB ? 'dashed' : 'solid' },
        areaStyle: spec.chart === 'area' ? { opacity: 0.15 } : undefined,
        itemStyle: { color: isB && !isLine ? c + '99' : c, borderRadius: stacked ? 0 : [4, 4, 0, 0],
          borderColor: stacked ? '#fff' : undefined, borderWidth: stacked ? 1 : 0 },
        barMaxWidth: 28, barGap: '15%',
        emphasis: { focus: 'series' },
      }
    }),
  }
}

export function ResultTable({ columns, rows, max = 200 }: { columns: string[]; rows: Json[][]; max?: number }) {
  return (
    <div className="overflow-auto max-h-80 border border-slate-200 rounded-lg">
      <table className="data">
        <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.slice(0, max).map((r, i) => (
            <tr key={i}>{r.map((v, j) => <td key={j} className={typeof v === 'number' ? 'text-right tabular-nums' : ''}>{fmt(v)}</td>)}</tr>
          ))}
        </tbody>
      </table>
      {rows.length > max && <div className="text-xs text-slate-500 p-2">… a dalších {rows.length - max} řádků</div>}
    </div>
  )
}

export default function Chart({ spec, height = 300 }: { spec: ChartSpec; height?: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const [asTable, setAsTable] = useState(false)
  const option = useMemo(() => (spec.chart === 'kpi' || spec.chart === 'table' ? null : buildOption(spec)), [spec])

  useEffect(() => {
    if (!ref.current || !option || asTable) return
    const chart = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    chart.setOption({ textStyle: { fontFamily: '"Schibsted Grotesk", system-ui, sans-serif' }, ...option }, true)
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(ref.current)
    return () => { ro.disconnect(); chart.dispose() }
  }, [option, asTable])

  if (!spec.rows?.length) return <div className="text-sm text-slate-500 p-4">Žádná data</div>
  if (spec.chart === 'kpi') {
    const v = spec.rows[0]?.[spec.columns.indexOf(spec.y?.[0] ?? 'value')] ?? spec.rows[0]?.[0]
    return <div className="text-3xl font-semibold tabular-nums py-3">{fmt(v)}</div>
  }
  if (spec.chart === 'table') return <ResultTable columns={spec.columns} rows={spec.rows} />
  return (
    <div>
      <div className="flex justify-end mb-1">
        <button className="text-xs text-slate-500 hover:text-slate-800 cursor-pointer" onClick={() => setAsTable(!asTable)}>
          {asTable ? 'Zobrazit graf' : 'Zobrazit tabulku'}
        </button>
      </div>
      {asTable ? <ResultTable columns={spec.columns} rows={spec.rows} /> : <div ref={ref} style={{ height, width: '100%' }} />}
    </div>
  )
}
