import { useEffect, useState, type ReactNode } from 'react'
import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { api, type Json } from './api'
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
    <div className="flex items-baseline gap-4 text-sm">
      {me?.quota && <span className="text-xs text-slate-500 hidden md:inline"><QuotaLine q={me.quota} /></span>}
      {me?.role === 'admin' && <NavLink to="/admin" className={navCls}>Uživatelé</NavLink>}
      <NavLink to="/ucet" className={navCls} title={me?.email}>{me?.display_name || session?.user.email || 'Účet'}</NavLink>
      {enabled && <button className="text-slate-500 hover:text-ink cursor-pointer" onClick={() => void signOut()}>Odhlásit</button>}
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState<Json>(null)
  const { enabled, session } = useAuth()
  const loc = useLocation()
  const publicPage = PUBLIC_PREFIXES.some((p) => loc.pathname.startsWith(p))
  const showNav = !publicPage && (!enabled || session)
  useEffect(() => { api.health().then(setHealth).catch(() => setHealth({ ok: false })) }, [])
  return (
    <div className="min-h-screen">
      <header>
        <div className="max-w-6xl mx-auto px-5 md:px-8 pt-6 pb-4 flex flex-wrap items-baseline gap-x-8 gap-y-2">
          <Link to="/" className="text-[17px] font-semibold tracking-[-0.01em]">Story2Data</Link>
          {showNav && <nav className="flex items-baseline gap-6 text-sm">
            <NavLink to="/" end className={navCls}>Běhy</NavLink>
            <NavLink to="/new" className={navCls}>Nový scénář</NavLink>
            <NavLink to="/docs" className={navCls}>Dokumenty</NavLink>
          </nav>}
          <div className="ml-auto flex flex-wrap items-baseline gap-x-6 gap-y-1">
          {showNav && <UserMenu />}
          <div className="text-xs text-slate-500 flex items-center gap-1.5">
            {health?.ok === false && <><span className="size-1.5 rounded-full bg-red-700" aria-hidden="true" /><span className="text-red-700">Backend neodpovídá</span></>}
            {health?.ok && (health.mock
              ? <span className="chip bg-amber-50 text-amber-800 border border-amber-200" title="LLM_MOCK=1 nebo chybí klíč">MOCK režim (fixtures)</span>
              : <span className="chip bg-emerald-50 text-emerald-800 border border-emerald-200" title={`fallback: ${health.strong_fallbacks?.join(', ')}`}>
                  OpenAI · STRONG {health.strong_model} · CHEAP {health.cheap_model}
                </span>)}
          </div>
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
