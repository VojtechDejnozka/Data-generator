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
  `px-3 py-1.5 rounded-lg ${isActive ? 'bg-blue-50 text-blue-800 font-medium' : 'hover:bg-slate-100'}`

export default function App() {
  const [health, setHealth] = useState<Json>(null)
  useEffect(() => { api.health().then(setHealth).catch(() => setHealth({ ok: false })) }, [])
  return (
    <div className="min-h-screen">
      <header className="bg-white border-b border-slate-200">
        <div className="max-w-7xl mx-auto px-4 h-14 flex items-center gap-6">
          <Link to="/" className="font-bold text-lg tracking-tight">Story<span className="text-blue-600">2</span>Data</Link>
          <span className="text-sm text-slate-500 hidden md:inline">Od příběhu k datům</span>
          <nav className="flex gap-1 text-sm ml-4">
            <NavLink to="/" end className={navCls}>Data – běhy</NavLink>
            <NavLink to="/new" className={navCls}>Nový scénář</NavLink>
            <NavLink to="/docs" className={navCls}>Dokumenty</NavLink>
          </nav>
          <div className="ml-auto text-xs">
            {health?.ok === false && <span className="chip bg-red-50 text-red-700">backend nedostupný</span>}
            {health?.ok && (health.mock
              ? <span className="chip bg-amber-50 text-amber-800 border border-amber-200" title="LLM_MOCK=1 nebo chybí klíč">MOCK režim (fixtures)</span>
              : <span className="chip bg-emerald-50 text-emerald-800 border border-emerald-200" title={`fallback: ${health.strong_fallbacks?.join(', ')}`}>
                  Gemini · STRONG {health.strong_model} · CHEAP {health.cheap_model}
                </span>)}
          </div>
        </div>
      </header>
      <main className="max-w-7xl mx-auto px-4 py-6">
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
