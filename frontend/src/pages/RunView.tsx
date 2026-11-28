import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, type Json } from '../api'
import ReportTab from '../components/ReportTab'
import FilesFromData from '../components/FilesFromData'
import DataBrowser from '../components/DataBrowser'
import { DashboardTab, DetectiveTab, GroundTruthTab, LogTab, SqlTab } from '../components/Tabs'
import { VerdictChip } from './Home'

const TABS: [string, string][] = [
  ['report', 'Report & důkaz'], ['data', 'Data browser'], ['dashboard', 'Dashboard'], ['sql', 'SQL'],
  ['files', 'Soubory z dat'], ['detective', 'Detektiv'], ['truth', 'Ground truth'], ['log', 'Log & tokeny'],
]

export default function RunView() {
  const { rid } = useParams()
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const requestedTab = params.get('tab')
  const tab = TABS.some(([id]) => id === requestedTab) ? requestedTab : 'report'
  const [run, setRun] = useState<Json>(null)
  const [report, setReport] = useState<Json>(null)
  const [schema, setSchema] = useState<Json>(null)
  const [sql, setSql] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [reviewError, setReviewError] = useState('')
  const [openingReview, setOpeningReview] = useState(false)

  useEffect(() => {
    setRun(null); setReport(null); setSchema(null)
    api.run(rid!).then(setRun).catch((e) => setErr(String(e)))
    api.report(rid!).then(setReport).catch(() => undefined)
    api.schema(rid!).then(setSchema).catch(() => undefined)
  }, [rid])

  if (err) return <div className="text-red-700">{err}</div>
  if (!run) return <div className="text-slate-500">Načítám běh…</div>
  const go = (t: string) => setParams({ tab: t })
  const observation = run.observation
  const openReview = async () => {
    setOpeningReview(true)
    setReviewError('')
    try {
      const { session_id } = await api.reviewRun(run.run_id)
      nav(`/plan/${session_id}`, { state: { runSearch: params.toString() } })
    } catch (error) {
      setReviewError(String(error))
    } finally {
      setOpeningReview(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-xs text-slate-500 mono">{run.run_id}{run.parent_run_id && <> · navazuje na <Link className="text-blue-700" to={`/runs/${run.parent_run_id}`}>{run.parent_run_id}</Link></>}</div>
          <h1 className="text-[28px] leading-tight font-semibold tracking-[-0.015em] truncate">{run.name} <span className="text-slate-500 font-normal text-base">– {run.label}</span></h1>
        </div>
        <VerdictChip v={run.verdict?.verdict} />
        {run.engine === 'scm' && <span className="chip bg-indigo-50 text-indigo-800 border border-indigo-200"
          title={`hash čistého světa ${run.causal?.world_hash ?? ''}`}>SCM engine · validace {run.causal?.status}</span>}
        {run.mock && <span className="chip bg-amber-50 text-amber-800 border border-amber-200">mock LLM</span>}
        {run.parent_run_id && <Link className="btn" to={`/compare?${new URLSearchParams({
          a: run.parent_run_id, b: run.run_id, ...(tab ? { tab } : {}),
        })}`}>Porovnat s rodičem</Link>}
        {run.spec && <button className="btn" onClick={() => void openReview()} disabled={openingReview}>Zpět k návrhu</button>}
        {run.spec?.scm && <a className="btn" href={`/api/runs/${run.run_id}/scm.json`}>Stáhnout SCM (JSON)</a>}
        <a className="btn" href={`/api/runs/${run.run_id}/export.zip`}>⬇ CSV (ZIP)</a>
        <a className="btn" href={`/api/runs/${run.run_id}/db`}>⬇ SQLite</a>
      </div>
      {reviewError && <div role="alert" className="text-sm text-red-700">{reviewError}</div>}
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
        {observation.observation_id && <a className="text-blue-700 underline"
          href={`/api/runs/${run.run_id}/observations/${observation.observation_id}/db`}>Stáhnout pozorovaná data (SQLite)</a>}
      </div>}
      <div className="flex border-b border-slate-200 overflow-x-auto">
        {TABS.map(([id, label]) => <button key={id} className={`tab ${tab === id ? 'tab-active' : ''}`} onClick={() => go(id)}>{label}</button>)}
      </div>
      {tab === 'report' && (report ? <ReportTab report={report} onSql={(s) => { setSql(s); go('sql') }} /> : <div className="text-slate-500">Načítám report…</div>)}
      {tab === 'data' && schema && <DataBrowser runId={run.run_id} schema={schema} onFiles={(t) => setParams({ tab: 'files', table: t })} />}
      {tab === 'dashboard' && <DashboardTab key={run.run_id} runId={run.run_id} canRefresh={run.engine === 'scm' && run.status === 'done'} />}
      {tab === 'sql' && <SqlTab runId={run.run_id} initial={sql} />}
      {tab === 'files' && <FilesFromData runId={run.run_id} initialTable={params.get('table') ?? undefined} />}
      {tab === 'detective' && <DetectiveTab runId={run.run_id} />}
      {tab === 'truth' && <GroundTruthTab key={run.run_id} runId={run.run_id} />}
      {tab === 'log' && <LogTab run={run} />}
    </div>
  )
}

const fmtDifficulty = (value: number) => value.toLocaleString('cs-CZ', { maximumFractionDigits: 1 })
