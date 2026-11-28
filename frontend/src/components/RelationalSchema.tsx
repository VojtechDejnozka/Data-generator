import { useEffect, useId, useRef, useState } from 'react'
import type { Table } from '../api'

type Connection = { source: string; target: string; path: string }

function connectionPath(from: DOMRect, to: DOMRect, root: DOMRect, sameCard: boolean) {
  const y1 = from.top + from.height / 2 - root.top
  const y2 = to.top + to.height / 2 - root.top
  const right = sameCard || to.left >= from.right || to.left === from.left
  const targetRight = sameCard || to.left === from.left || to.right <= from.left
  const x1 = (right ? from.right : from.left) - root.left
  const x2 = (targetRight ? to.right : to.left) - root.left
  const start = x1 + (right ? 12 : -12)
  const end = x2 + (targetRight ? 12 : -12)
  const bend = (y1 + y2) / 2
  return `M ${x1} ${y1} H ${start} V ${bend} H ${end} V ${y2} H ${x2}`
}

export default function RelationalSchema({ tables }: { tables: Table[] }) {
  const area = useRef<HTMLDivElement>(null)
  const arrow = useId()
  const [connections, setConnections] = useState<Connection[]>([])
  const [highlight, setHighlight] = useState<string | null>(null)
  const highlightedTarget = connections.find((edge) => edge.source === highlight)?.target

  useEffect(() => {
    const container = area.current
    if (!container) return
    const rows = new Map(Array.from(container.querySelectorAll<HTMLElement>('[data-column]'),
      (node) => [node.dataset.column, node]))
    const measure = () => {
      const root = container.getBoundingClientRect()
      setConnections(tables.flatMap((table) => table.columns.flatMap((column) => {
        const source = `${table.name}.${column.name}`
        const from = rows.get(source)
        const to = column.fk ? rows.get(column.fk) : undefined
        if (!from || !to || !column.fk) return []
        return [{ source, target: column.fk, path: connectionPath(from.getBoundingClientRect(),
          to.getBoundingClientRect(), root, from.closest('article') === to.closest('article')) }]
      })))
    }
    const observer = new ResizeObserver(measure)
    observer.observe(container)
    for (const node of rows.values()) observer.observe(node)
    measure()
    return () => observer.disconnect()
  }, [tables])

  return <section className="card p-4 space-y-3" aria-label="Relační schéma">
    <h2 className="font-semibold">Relační schéma</h2>
    <p className="text-xs text-slate-600">Šipka vede od cizího klíče (FK) k odkazovanému sloupci. Najeďte na odkaz a zvýrazní se jeho vazba.</p>
    <div ref={area} className="relative isolate grid gap-10 px-5 sm:grid-cols-2 xl:grid-cols-3 items-start">
      <svg className="pointer-events-none absolute inset-0 h-full w-full text-blue-700" aria-hidden="true">
        <defs>
          <marker id={arrow} viewBox="0 0 8 8" refX="8" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M 0 0 L 8 4 L 0 8 Z" fill="currentColor" />
          </marker>
        </defs>
        {connections.map((edge) => <path key={edge.source} d={edge.path} fill="none" stroke="currentColor"
          strokeWidth={highlight === edge.source ? 2.5 : 1.5} opacity={highlight && highlight !== edge.source ? 0.2 : 0.8}
          markerEnd={`url(#${arrow})`} />)}
      </svg>
      {tables.map((table) => <article key={table.name} id={`relation-${table.name}`} className="relative border border-slate-200 bg-white rounded-lg min-w-0 overflow-hidden target:ring-2 target:ring-blue-600">
        <h3 className="mono font-semibold text-sm px-3 py-2 bg-slate-100 break-all">{table.name}</h3>
        <ul className="divide-y divide-slate-100">
          {table.columns.map((column) => <li key={column.name} data-column={`${table.name}.${column.name}`}
            className={`px-3 py-1.5 text-xs ${highlightedTarget === `${table.name}.${column.name}` ? 'bg-blue-50' : ''}`}>
            <div className="flex gap-2 items-baseline">
              {column.pk && <span className="text-amber-800 font-semibold">PK</span>}
              {column.fk && <span className="text-blue-800 font-semibold">FK</span>}
              <span className="mono break-all flex-1">{column.name}</span>
              <span className="text-slate-500">{column.type}</span>
            </div>
            {column.fk && <a className="mono text-blue-700 underline break-all"
              onMouseEnter={() => setHighlight(`${table.name}.${column.name}`)} onMouseLeave={() => setHighlight(null)}
              onFocus={() => setHighlight(`${table.name}.${column.name}`)} onBlur={() => setHighlight(null)}
              href={`#relation-${column.fk.split('.')[0]}`}>→ {column.fk}</a>}
          </li>)}
        </ul>
      </article>)}
    </div>
  </section>
}
