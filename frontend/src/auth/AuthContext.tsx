// Session state for the whole app: Supabase session + the backend's view of the account (/api/me).
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import type { Session } from '@supabase/supabase-js'
import { api, ApiError, setUnauthorizedHandler, type Me } from '../api'
import { authEnabled, supabase } from './supabase'

interface AuthState {
  enabled: boolean
  loading: boolean
  session: Session | null
  me: Me | null
  /** Backend refused the account (blocked / not on the allowlist / not invited). */
  denied: string | null
  refreshMe: () => Promise<void>
  signOut: () => Promise<void>
}

const Ctx = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const [session, setSession] = useState<Session | null>(null)
  const [loading, setLoading] = useState(authEnabled)
  const [me, setMe] = useState<Me | null>(null)
  const [denied, setDenied] = useState<string | null>(null)

  const refreshMe = useCallback(async () => {
    try {
      setMe(await api.me())
      setDenied(null)
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) setDenied(e.message)
    }
  }, [])

  const signOut = useCallback(async () => {
    setMe(null)
    setDenied(null)
    if (supabase) await supabase.auth.signOut()
    navigate('/prihlaseni', { replace: true })
  }, [navigate])

  useEffect(() => {
    if (!supabase) { void refreshMe(); return }
    supabase.auth.getSession().then(({ data }) => { setSession(data.session); setLoading(false) })
    const { data: sub } = supabase.auth.onAuthStateChange((event, s) => {
      setSession(s)
      if (event === 'PASSWORD_RECOVERY') navigate('/auth/nastaveni-hesla', { replace: true })
      if (event === 'SIGNED_OUT') { setMe(null); setDenied(null) }
    })
    return () => sub.subscription.unsubscribe()
  }, [navigate, refreshMe])

  // Backend says 401 (token expired or revoked): try one refresh, otherwise end the session.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (!supabase) return
      supabase.auth.refreshSession().then(({ data, error }) => {
        if (error || !data.session) {
          const here = window.location.pathname + window.location.search
          void supabase!.auth.signOut().then(() =>
            navigate(`/prihlaseni?next=${encodeURIComponent(here)}&vyprselo=1`, { replace: true }))
        }
      })
    })
    return () => setUnauthorizedHandler(null)
  }, [navigate])

  useEffect(() => { if (session) void refreshMe() }, [session?.user.id, refreshMe])  // eslint-disable-line react-hooks/exhaustive-deps

  // quota changes after jobs / 429 responses
  useEffect(() => {
    const h = () => { if (!authEnabled || session) void refreshMe() }
    window.addEventListener('s2d:usage', h)
    return () => window.removeEventListener('s2d:usage', h)
  }, [session, refreshMe])

  const value = useMemo<AuthState>(() => ({ enabled: authEnabled, loading, session, me, denied, refreshMe, signOut }),
    [loading, session, me, denied, refreshMe, signOut])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useAuth(): AuthState {
  const v = useContext(Ctx)
  if (!v) throw new Error('useAuth outside AuthProvider')
  return v
}

/** Route guard: unauthenticated visitors go to the login page and come back afterwards. */
export function RequireAuth({ children, admin = false }: { children: ReactNode; admin?: boolean }) {
  const { enabled, loading, session, me, denied, signOut } = useAuth()
  const loc = useLocation()
  if (enabled && loading) return <div className="text-slate-500">Ověřuji přihlášení…</div>
  if (enabled && !session) {
    return <Navigate to={`/prihlaseni?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />
  }
  if (denied) {
    return (
      <div className="max-w-md space-y-4">
        <h1 className="text-[28px] font-semibold tracking-[-0.015em]">Přístup zatím není povolen</h1>
        <p className="text-slate-600">{denied}</p>
        <p className="text-sm text-slate-500">Přihlášen jako {session?.user.email}.</p>
        <button className="btn" onClick={() => void signOut()}>Odhlásit se</button>
      </div>
    )
  }
  if (admin && me && me.role !== 'admin') return <div className="text-slate-600">Tato stránka je jen pro správce.</div>
  return <>{children}</>
}
