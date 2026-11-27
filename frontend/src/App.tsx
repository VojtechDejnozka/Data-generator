import { useEffect, useState } from 'react'
import { Link, Route, Routes } from 'react-router-dom'
import { api, type Json } from './api'
import Home from './pages/Home'
import NewScenario from './pages/NewScenario'
import SpecReview from './pages/SpecReview'
import RunView from './pages/RunView'
import Compare from './pages/Compare'

export default function App() {
  const [health, setHealth] = useState<Json>(null)
  useEffect(() => { api.health().then(setHealth).catch(() => setHealth({ ok: false })) }, [])
  return (
    <div className="min-h-screen">
      <header className="bg-white border-b border-slate-200">
        <div className="max-w-7xl mx-auto px-4 h-14 flex items-center gap-6">
          <Link to="/" className="font-bold text-lg tracking-tight">Story<span className="text-blue-600">2</span>Data</Link>
          <span className="text-sm text-slate-500 hidden md:inline">Od příběhu k datům</span>
          <nav className="flex gap-4 text-sm ml-4">
            <Link to="/new" className="hover:text-blue-700">Nový scénář</Link>
            <Link to="/" className="hover:text-blue-700">Běhy</Link>
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
        </Routes>
      </main>
    </div>
  )
}
