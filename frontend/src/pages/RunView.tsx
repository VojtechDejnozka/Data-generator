import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, type Json } from '../api'
import ReportTab from '../components/ReportTab'
import FilesFromData from '../components/FilesFromData'
import DataBrowser from '../components/DataBrowser'
import { DashboardTab, DetectiveTab, GroundTruthTab, LogTab, SqlTab, WhatIfTab } from '../components/Tabs'
import { VerdictChip } from './Home'

const TABS: [string, string][] = [
  ['report', 'Report & důkaz'], ['data', 'Data browser'], ['dashboard', 'Dashboard'], ['sql', 'SQL'],
  ['whatif', 'What-if'], ['files', 'Soubory z dat'], ['detective', 'Detektiv'], ['truth', 'Ground truth'], ['log', 'Log & tokeny'],
]

export default function RunView() {
  const { rid } = useParams()
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') ?? 'report'
  const [run, setRun] = useState<Json>(null)
  const [report, setReport] = useState<Json>(null)
  const [schema, setSchema] = useState<Json>(null)
  const [sql, setSql] = useState('')
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    setRun(null); setReport(null); setSchema(null)
    api.run(rid!).then(setRun).catch((e) => setErr(String(e)))
    api.report(rid!).then(setReport).catch(() => undefined)
    api.schema(rid!).then(setSchema).catch(() => undefined)
  }, [rid])

  if (err) return <div className="text-red-700">{err}</div>
  if (!run) return <div className="text-slate-500">Načítám běh…</div>
  const go = (t: string) => setParams({ tab: t })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-xs text-slate-500 mono">{run.run_id}{run.parent_run_id && <> · what-if z <Link className="text-blue-700" to={`/runs/${run.parent_run_id}`}>{run.parent_run_id}</Link></>}</div>
          <h1 className="text-xl font-bold truncate">{run.name} <span className="text-slate-500 font-normal text-base">– {run.label}</span></h1>
        </div>
        <VerdictChip v={run.verdict?.verdict} />
        {run.mock && <span className="chip bg-amber-50 text-amber-800 border border-amber-200">mock LLM</span>}
        {run.parent_run_id && <Link className="btn" to={`/compare?a=${run.parent_run_id}&b=${run.run_id}`}>Porovnat s rodičem</Link>}
        <a className="btn" href={`/api/runs/${run.run_id}/export.zip`}>⬇ CSV (ZIP)</a>
        <a className="btn" href={`/api/runs/${run.run_id}/db`}>⬇ SQLite</a>
      </div>
      <div className="flex border-b border-slate-200 overflow-x-auto">
        {TABS.map(([id, label]) => <button key={id} className={`tab ${tab === id ? 'tab-active' : ''}`} onClick={() => go(id)}>{label}</button>)}
      </div>
      {tab === 'report' && (report ? <ReportTab report={report} onSql={(s) => { setSql(s); go('sql') }} /> : <div className="text-slate-500">Načítám report…</div>)}
      {tab === 'data' && schema && <DataBrowser runId={run.run_id} schema={schema} onFiles={(t) => setParams({ tab: 'files', table: t })} />}
      {tab === 'dashboard' && <DashboardTab runId={run.run_id} />}
      {tab === 'sql' && <SqlTab runId={run.run_id} initial={sql} />}
      {tab === 'whatif' && <WhatIfTab run={run} />}
      {tab === 'files' && <FilesFromData runId={run.run_id} initialTable={params.get('table') ?? undefined} />}
      {tab === 'detective' && <DetectiveTab runId={run.run_id} />}
      {tab === 'truth' && <GroundTruthTab runId={run.run_id} />}
      {tab === 'log' && <LogTab run={run} />}
    </div>
  )
}
