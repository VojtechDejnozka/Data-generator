import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, download, type DownloadWhat, type Json } from '../api'
import ReportTab from '../components/ReportTab'
import FilesFromData from '../components/FilesFromData'
import DataBrowser from '../components/DataBrowser'
import { DashboardTab, GroundTruthTab, LogTab, SqlTab } from '../components/Tabs'
import JobProgress from '../components/JobProgress'

const TABS: [string, string][] = [
  ['report', 'Report & důkaz'], ['data', 'Data browser'], ['dashboard', 'Dashboard'], ['sql', 'SQL'],
  ['files', 'Soubory z dat'], ['truth', 'Ground truth'], ['log', 'Log & tokeny'],
]

const SHARED_TABS = new Set(['report', 'data', 'dashboard', 'sql', 'truth'])

export default function RunView({ runId, shared = false }: { runId?: string; shared?: boolean } = {}) {
  const params0 = useParams()
  const rid = runId ?? params0.rid
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const tabs = shared ? TABS.filter(([id]) => SHARED_TABS.has(id)) : TABS
  const requestedTab = params.get('tab')
  const tab = tabs.some(([id]) => id === requestedTab) ? requestedTab : 'report'
  const [run, setRun] = useState<Json>(null)
  const [report, setReport] = useState<Json>(null)
  const [schema, setSchema] = useState<Json>(null)
  const [sql, setSql] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [reviewError, setReviewError] = useState('')
  const [downloadError, setDownloadError] = useState('')
  const [openingReview, setOpeningReview] = useState(false)

  const [reload, setReload] = useState(0)
  useEffect(() => {
    setRun(null); setReport(null); setSchema(null); setDownloadError('')
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
  const dl = (what: DownloadWhat | 'scm') => {
    setDownloadError('')
    return download(run.run_id, what).catch((e) => setDownloadError(String(e)))
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-xs text-slate-500 mono">{run.run_id}{!shared && run.parent_run_id && <> · navazuje na <Link className="text-blue-700" to={`/runs/${run.parent_run_id}`}>{run.parent_run_id}</Link></>}
            {shared && <span className="font-sans"> · sdílený náhled jen pro čtení</span>}
            {!shared && run.is_preset && <span className="font-sans"> · ukázkový běh</span>}</div>
          <h1 className="text-[28px] leading-tight font-semibold tracking-[-0.015em] truncate">{run.name} <span className="text-slate-500 font-normal text-base">– {run.label}</span></h1>
        </div>
        {run.mock && <span className="chip bg-amber-50 text-amber-800 border border-amber-200">mock LLM</span>}
        {!shared && run.parent_run_id && <Link className="btn" to={`/compare?${new URLSearchParams({
          a: run.parent_run_id, b: run.run_id, ...(tab ? { tab } : {}),
        })}`}>Porovnat s rodičem</Link>}
        {!shared && run.spec && <button className="btn" onClick={() => void openReview()} disabled={openingReview}>Zpět k návrhu</button>}
        {!shared && <DownloadMenu hasScm={Boolean(run.spec?.scm)} onDownload={dl} />}
        {!shared && run.own && <ShareButton runId={run.run_id} initial={run.share_token ?? null} />}
      </div>
      {reviewError && <div role="alert" className="text-sm text-red-700">{reviewError}</div>}
      {downloadError && <div role="alert" className="text-sm text-red-700">{downloadError}</div>}
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
      </div>}
      <div className="flex border-b border-slate-200 overflow-x-auto">
        {tabs.map(([id, label]) => <button key={id} className={`tab ${tab === id ? 'tab-active' : ''}`} onClick={() => go(id)}>{label}</button>)}
      </div>
      {tab === 'report' && (report ? <ReportTab report={report} onSql={(s) => { setSql(s); go('sql') }} /> : <div className="text-slate-500">Načítám report…</div>)}
      {tab === 'data' && schema && <DataBrowser runId={run.run_id} schema={schema} onFiles={shared ? undefined : (t) => setParams({ tab: 'files', table: t })} />}
      {tab === 'dashboard' && <DashboardTab key={run.run_id} runId={run.run_id} canRefresh={!shared && run.own && run.engine === 'scm' && run.status === 'done'} />}
      {tab === 'sql' && <SqlTab runId={run.run_id} initial={sql} />}
      {tab === 'files' && <FilesFromData runId={run.run_id} initialTable={params.get('table') ?? undefined} />}
      {tab === 'truth' && <GroundTruthTab key={run.run_id} runId={run.run_id} />}
      {tab === 'log' && <LogTab run={run} />}
    </div>
  )
}

function DownloadMenu({ hasScm, onDownload }: { hasScm: boolean; onDownload: (what: DownloadWhat | 'scm') => void }) {
  const menu = useRef<HTMLDetailsElement>(null)
  const summary = useRef<HTMLElement>(null)
  const close = () => {
    if (menu.current) menu.current.open = false
  }
  useEffect(() => {
    const closeOutside = (event: PointerEvent) => {
      if (menu.current?.open && event.target instanceof Node && !menu.current.contains(event.target)) close()
    }
    document.addEventListener('pointerdown', closeOutside)
    return () => document.removeEventListener('pointerdown', closeOutside)
  }, [])
  const choose = (what: DownloadWhat | 'scm') => {
    close()
    summary.current?.focus()
    onDownload(what)
  }
  return (
    <details ref={menu} className="group relative" onKeyDown={(event) => {
      if (event.key === 'Escape' && menu.current?.open) {
        event.stopPropagation()
        close()
        summary.current?.focus()
      }
    }}>
      <summary ref={summary} className="btn cursor-pointer list-none [&::-webkit-details-marker]:hidden">
        Stáhnout <span aria-hidden="true" className="text-xs group-open:rotate-180">▾</span>
      </summary>
      <div className="card absolute right-0 z-20 mt-2 min-w-40 space-y-0.5 p-1 shadow-sm">
        {hasScm && <button type="button" className="w-full rounded px-3 py-2 text-left text-sm hover:bg-slate-100" onClick={() => choose('scm')}>SCM (JSON)</button>}
        <button type="button" className="w-full rounded px-3 py-2 text-left text-sm hover:bg-slate-100" onClick={() => choose('zip')}>CSV (ZIP)</button>
        <button type="button" className="w-full rounded px-3 py-2 text-left text-sm hover:bg-slate-100" onClick={() => choose('db')}>SQLite</button>
      </div>
    </details>
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
