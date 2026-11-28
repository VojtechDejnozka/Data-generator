import { useEffect, useState } from 'react'
import { Link, NavLink, Route, Routes } from 'react-router-dom'
import { api, type Json } from './api'
import Home from './pages/Home'
import NewScenario from './pages/NewScenario'
import SpecReview from './pages/SpecReview'
import RunView from './pages/RunView'
import Compare from './pages/Compare'
import DocsHome from './pages/DocsHome'
import DocSpecReview from './pages/DocSpecReview'
import DocRunView from './pages/DocRunView'

const navCls = ({ isActive }: { isActive: boolean }) =>
  `${isActive ? 'mark text-ink' : 'text-slate-500 hover:text-ink'}`

export default function App() {
  const [health, setHealth] = useState<Json>(null)
  useEffect(() => { api.health().then(setHealth).catch(() => setHealth({ ok: false })) }, [])
  return (
    <div className="min-h-screen">
      <header>
        <div className="max-w-6xl mx-auto px-5 md:px-8 pt-6 pb-4 flex flex-wrap items-baseline gap-x-8 gap-y-2">
          <Link to="/" className="text-[17px] font-semibold tracking-[-0.01em]">Story2Data</Link>
          <nav className="flex items-baseline gap-6 text-sm">
            <NavLink to="/" end className={navCls}>Běhy</NavLink>
            <NavLink to="/new" className={navCls}>Nový scénář</NavLink>
            <NavLink to="/docs" className={navCls}>Dokumenty</NavLink>
          </nav>
          <div className="ml-auto text-xs text-slate-500 flex items-center gap-1.5">
            {health?.ok === false && <><span className="size-1.5 rounded-full bg-red-700" aria-hidden="true" /><span className="text-red-700">Backend neodpovídá</span></>}
            {health?.ok && (health.mock
              ? <><span className="size-1.5 rounded-full bg-amber-400" aria-hidden="true" /><span title="LLM_MOCK=1 nebo chybí klíč">Mock režim, odpovědi z fixtures</span></>
              : <><span className="size-1.5 rounded-full bg-emerald-700" aria-hidden="true" /><span title={`STRONG ${health.strong_model}, fallback: ${health.strong_fallbacks?.join(', ')}`}>Gemini {health.strong_model}</span></>)}
          </div>
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-5 md:px-8 pt-6 pb-24">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/new" element={<NewScenario />} />
          <Route path="/plan/:sid" element={<SpecReview />} />
          <Route path="/runs/:rid" element={<RunView />} />
          <Route path="/compare" element={<Compare />} />
          <Route path="/docs" element={<DocsHome />} />
          <Route path="/docs/plan/:sid" element={<DocSpecReview />} />
          <Route path="/docs/runs/:rid" element={<DocRunView />} />
        </Routes>
      </main>
    </div>
  )
}
