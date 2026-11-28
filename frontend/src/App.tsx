import { useEffect, useState, type ReactNode } from 'react'
import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { api, type BackendHealth } from './api'
import { RequireAuth, useAuth } from './auth/AuthContext'
import { ForgotPassword, Login, Register, ResetPassword } from './pages/AuthPages'
import { Account, Admin, QuotaLine, SharedRun } from './pages/AccountPages'
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

const guard = (el: ReactNode, admin = false) => <RequireAuth admin={admin}>{el}</RequireAuth>
const PUBLIC_PREFIXES = ['/prihlaseni', '/registrace', '/zapomenute-heslo', '/auth/', '/s/']

function UserMenu() {
  const { enabled, session, me, signOut } = useAuth()
  if (enabled && !session) return null
  return (
    <div className="flex shrink-0 items-center gap-2.5 text-sm md:gap-3">
      {me?.quota && <span className="hidden text-xs text-slate-500 lg:inline"><QuotaLine q={me.quota} /></span>}
      {me?.role === 'admin' && <NavLink to="/admin" className={navCls}>Uživatelé</NavLink>}
      <NavLink to="/ucet" className={(props) => `${navCls(props)} block max-w-32 truncate xl:max-w-44`}
        title={me?.email || me?.display_name || session?.user.email}>
        <span className="sm:hidden">Účet</span>
        <span className="hidden sm:inline">{me?.display_name || session?.user.email || 'Účet'}</span>
      </NavLink>
      {enabled && <button className="text-slate-500 hover:text-ink cursor-pointer" onClick={() => void signOut()}>Odhlásit</button>}
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState<BackendHealth | null>(null)
  const { enabled, session } = useAuth()
  const loc = useLocation()
  const publicPage = PUBLIC_PREFIXES.some((p) => loc.pathname.startsWith(p))
  const showNav = !publicPage && (!enabled || session)
  const provider = ({ openai: 'OpenAI', gemini: 'Gemini' } as Record<string, string>)[health?.provider ?? ''] ?? 'LLM'
  const modelDetails = [
    provider,
    health?.strong_model && `STRONG ${health.strong_model}`,
    health?.cheap_model && `CHEAP ${health.cheap_model}`,
    health?.strong_fallbacks?.length && `Náhradní modely STRONG ${health.strong_fallbacks.join(', ')}`,
    health?.cheap_fallbacks?.length && `Náhradní modely CHEAP ${health.cheap_fallbacks.join(', ')}`,
  ].filter(Boolean).join(' · ')
  useEffect(() => {
    let active = true
    let pending: AbortController | null = null
    const check = async () => {
      if (pending) return
      const request = new AbortController()
      pending = request
      const timeout = window.setTimeout(() => request.abort(), 5000)
      try {
        const status = await api.health(request.signal)
        if (active) setHealth(status)
      } catch {
        if (active) setHealth({ ok: false })
      } finally {
        window.clearTimeout(timeout)
        pending = null
      }
    }
    void check()
    const interval = window.setInterval(check, 15000)
    window.addEventListener('focus', check)
    window.addEventListener('online', check)
    return () => {
      active = false
      pending?.abort()
      window.clearInterval(interval)
      window.removeEventListener('focus', check)
      window.removeEventListener('online', check)
    }
  }, [])
  return (
    <div className="min-h-screen">
      <header>
        <div className="mx-auto flex max-w-6xl items-center gap-3 overflow-x-auto whitespace-nowrap px-4 py-4 md:gap-5 md:px-8">
          <Link to="/" className="shrink-0 text-[17px] font-semibold tracking-[-0.01em]">Story2Data</Link>
          {showNav && <nav className="flex shrink-0 items-center gap-3 text-sm md:gap-5">
            <NavLink to="/" end className={navCls}>Běhy</NavLink>
            <NavLink to="/new" className={navCls}>Nový scénář</NavLink>
            <NavLink to="/docs" className={navCls}>Dokumenty</NavLink>
          </nav>}
          <div className="ml-auto flex shrink-0 items-center gap-2.5 md:gap-3">
            {showNav && <UserMenu />}
            {health?.ok === false && <span role="status" className="flex items-center gap-1.5 text-xs text-red-700">
              <span className="size-1.5 rounded-full bg-red-700" aria-hidden="true" /> Backend nedostupný
            </span>}
            {health?.ok && (health.mock
              ? <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-800" title="LLM_MOCK=1 nebo chybí klíč">Mock</span>
              : <span className="max-w-36 truncate rounded border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 text-[11px] text-emerald-800"
                  title={modelDetails} aria-label={modelDetails}>
                  {health.strong_model || provider}
                </span>)}
          </div>
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-5 md:px-8 pt-6 pb-24">
        <Routes>
          <Route path="/prihlaseni" element={<Login />} />
          <Route path="/registrace" element={<Register />} />
          <Route path="/zapomenute-heslo" element={<ForgotPassword />} />
          <Route path="/auth/nastaveni-hesla" element={<ResetPassword />} />
          <Route path="/s/:token" element={<SharedRun />} />
          <Route path="/" element={guard(<Home />)} />
          <Route path="/new" element={guard(<NewScenario />)} />
          <Route path="/plan/:sid" element={guard(<SpecReview />)} />
          <Route path="/runs/:rid" element={guard(<RunView />)} />
          <Route path="/compare" element={guard(<Compare />)} />
          <Route path="/docs" element={guard(<DocsHome />)} />
          <Route path="/docs/plan/:sid" element={guard(<DocSpecReview />)} />
          <Route path="/docs/runs/:rid" element={guard(<DocRunView />)} />
          <Route path="/ucet" element={guard(<Account />)} />
          <Route path="/admin" element={guard(<Admin />, true)} />
          <Route path="*" element={<div className="text-slate-600">Stránka neexistuje. <Link className="underline" to="/">Zpět na běhy</Link></div>} />
        </Routes>
      </main>
    </div>
  )
}
