import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, download, type DownloadWhat, type Json } from '../api'
import ReportTab from '../components/ReportTab'
import FilesFromData from '../components/FilesFromData'
import DataBrowser from '../components/DataBrowser'
import { DashboardTab, DetectiveTab, GroundTruthTab, LogTab, SqlTab, WhatIfTab } from '../components/Tabs'
import { VerdictChip } from './Home'
import JobProgress from '../components/JobProgress'

const TABS: [string, string][] = [
  ['report', 'Report & důkaz'], ['data', 'Data browser'], ['dashboard', 'Dashboard'], ['sql', 'SQL'],
  ['whatif', 'What-if'], ['files', 'Soubory z dat'], ['detective', 'Detektiv'], ['truth', 'Ground truth'], ['log', 'Log & tokeny'],
]

const SHARED_TABS = new Set(['report', 'data', 'dashboard', 'sql', 'truth'])

export default function RunView({ runId, shared = false }: { runId?: string; shared?: boolean } = {}) {
  const params0 = useParams()
  const rid = runId ?? params0.rid
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') ?? 'report'
  const [run, setRun] = useState<Json>(null)
  const [report, setReport] = useState<Json>(null)
  const [schema, setSchema] = useState<Json>(null)
  const [sql, setSql] = useState('')
  const [err, setErr] = useState<string | null>(null)

  const [reload, setReload] = useState(0)
  useEffect(() => {
    setRun(null); setReport(null); setSchema(null)
    api.run(rid!).then(setRun).catch((e) => setErr(String(e)))
    api.report(rid!).then(setReport).catch(() => undefined)
    api.schema(rid!).then(setSchema).catch(() => undefined)
  }, [rid, reload])

  if (err) return <div className="text-red-700">{err}</div>
  if (!run) return <div className="text-slate-500">Načítám běh…</div>
  if (run.status === 'running' && run.job_id && run.own && !shared) {
    // the job is resumable: watching it drives the remaining stages (also after closing the tab)
    return <JobProgress jobId={run.job_id} title={`Běh ${run.run_id} pokračuje`} onDone={() => setReload((n) => n + 1)} />
  }
  const go = (t: string) => setParams({ tab: t })
  const observation = run.observation
  const tabs = shared ? TABS.filter(([id]) => SHARED_TABS.has(id)) : TABS
  const dl = (what: DownloadWhat, extra = {}) => download(run.run_id, what, extra).catch((e) => setErr(String(e)))

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-xs text-slate-500 mono">{run.run_id}{!shared && run.parent_run_id && <> · what-if z <Link className="text-blue-700" to={`/runs/${run.parent_run_id}`}>{run.parent_run_id}</Link></>}
            {shared && <span className="font-sans"> · sdílený náhled jen pro čtení</span>}
            {!shared && run.is_preset && <span className="font-sans"> · ukázkový běh (jen pro čtení, what-if si můžete vytvořit)</span>}</div>
          <h1 className="text-[28px] leading-tight font-semibold tracking-[-0.015em] truncate">{run.name} <span className="text-slate-500 font-normal text-base">– {run.label}</span></h1>
        </div>
        <VerdictChip v={run.verdict?.verdict} />
        {run.engine === 'scm' && <span className="chip bg-indigo-50 text-indigo-800 border border-indigo-200"
          title={`hash čistého světa ${run.causal?.world_hash ?? ''}`}>SCM engine · validace {run.causal?.status}</span>}
        {run.mock && <span className="chip bg-amber-50 text-amber-800 border border-amber-200">mock LLM</span>}
        {!shared && run.parent_run_id && <Link className="btn" to={`/compare?a=${run.parent_run_id}&b=${run.run_id}`}>Porovnat s rodičem</Link>}
        {!shared && <button className="btn" onClick={() => void dl('zip')}>⬇ CSV (ZIP)</button>}
        {!shared && <button className="btn" onClick={() => void dl('db')}>⬇ SQLite</button>}
        {!shared && run.own && <ShareButton runId={run.run_id} initial={run.share_token ?? null} />}
      </div>
      {observation && <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-slate-600">
        {observation.status === 'invalid_world' ? <span>Pozorovaná data nelze připravit, protože čistá data neprošla validací.</span>
          : observation.mode === 'manual' ? <span>Pozorovaná data s ručně zadanými chybami.</span>
          : <span>
            Cílová obtížnost {observation.target}/100.
            {observation.achieved != null && ` Odhad ${fmtDifficulty(observation.achieved)}/100.`}
            {observation.status === 'unreachable_below_baseline' && ' Cíl je nižší než odhad obtížnosti výchozích dat.'}
            {observation.status !== 'target_reached' && (observation.used_baseline
              ? ' Z dostupných variant byla použita výchozí pozorovaná data.'
              : ' Cíl nebyl dosažen. Použito nejlepší nalezené nastavení.')}
            {observation.achieved == null && ' Obtížnost se nepodařilo vyhodnotit.'}
          </span>}
        {!shared && observation.observation_id && <button className="text-blue-700 underline cursor-pointer"
          onClick={() => void dl('obs_db', { obs_id: observation.observation_id })}>Stáhnout pozorovaná data (SQLite)</button>}
      </div>}
      <div className="flex border-b border-slate-200 overflow-x-auto">
        {tabs.map(([id, label]) => <button key={id} className={`tab ${tab === id ? 'tab-active' : ''}`} onClick={() => go(id)}>{label}</button>)}
      </div>
      {tab === 'report' && (report ? <ReportTab report={report} onSql={(s) => { setSql(s); go('sql') }} /> : <div className="text-slate-500">Načítám report…</div>)}
      {tab === 'data' && schema && <DataBrowser runId={run.run_id} schema={schema} onFiles={(t) => setParams({ tab: 'files', table: t })} />}
      {tab === 'dashboard' && <DashboardTab runId={run.run_id} />}
      {tab === 'sql' && <SqlTab runId={run.run_id} initial={sql} />}
      {tab === 'whatif' && <WhatIfTab run={run} />}
      {tab === 'files' && <FilesFromData runId={run.run_id} initialTable={params.get('table') ?? undefined} />}
      {tab === 'detective' && <DetectiveTab runId={run.run_id} />}
      {tab === 'truth' && <GroundTruthTab key={run.run_id} runId={run.run_id} />}
      {tab === 'log' && <LogTab run={run} />}
    </div>
  )
}

function ShareButton({ runId, initial }: { runId: string; initial: string | null }) {
  const [token, setToken] = useState<string | null>(initial)
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const url = token ? `${window.location.origin}/s/${token}` : ''
  const toggle = async (enabled: boolean) => { setToken((await api.share(runId, enabled)).share_token); setCopied(false) }
  return (
    <div className="relative">
      <button className="btn" aria-expanded={open} onClick={() => setOpen(!open)}>{token ? 'Sdíleno' : 'Sdílet'}</button>
      {open && (
        <div className="absolute right-0 z-10 mt-2 w-80 card p-3 space-y-2 text-sm shadow-sm">
          {token ? <>
            <div className="text-slate-600">Kdokoli s odkazem uvidí report, data a dashboard tohoto běhu (jen pro čtení).</div>
            <input className="input mono text-xs" readOnly value={url} onFocus={(e) => e.target.select()} aria-label="Odkaz pro sdílení" />
            <div className="flex gap-2">
              <button className="btn" onClick={() => { void navigator.clipboard.writeText(url); setCopied(true) }}>{copied ? 'Zkopírováno' : 'Kopírovat'}</button>
              <button className="btn btn-danger" onClick={() => void toggle(false)}>Zrušit sdílení</button>
            </div>
          </> : <>
            <div className="text-slate-600">Vytvoří veřejný odkaz jen pro čtení – např. pro porotu. Lze ho kdykoli zrušit.</div>
            <button className="btn btn-primary" onClick={() => void toggle(true)}>Vytvořit odkaz</button>
          </>}
        </div>)}
    </div>
  )
}

const fmtDifficulty = (value: number) => value.toLocaleString('cs-CZ', { maximumFractionDigits: 1 })
