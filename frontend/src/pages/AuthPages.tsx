// Přihlášení, registrace, zapomenuté heslo a nastavení nového hesla (Supabase Auth).
import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth/AuthContext'
import { authErrorMessage, authFeatures, safeNext, supabase } from '../auth/supabase'

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
const MIN_PASSWORD = 8

function passwordProblem(pw: string): string | null {
  if (pw.length < MIN_PASSWORD) return `Heslo musí mít alespoň ${MIN_PASSWORD} znaků.`
  if (!/[a-zA-Zá-žÁ-Ž]/.test(pw) || !/\d/.test(pw)) return 'Heslo musí obsahovat písmena i číslice.'
  return null
}

function Shell({ title, lead, children, footer }: { title: string; lead?: ReactNode; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="max-w-sm mx-auto pt-8">
      <h1 className="text-[28px] leading-tight font-semibold tracking-[-0.015em] mb-1">{title}</h1>
      {lead && <p className="text-slate-600 mb-6">{lead}</p>}
      {children}
      {footer && <div className="mt-6 text-sm text-slate-600 space-y-1">{footer}</div>}
    </div>
  )
}

function Field({ id, label, error, ...props }: { id: string; label: string; error?: string | null } & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div>
      <label htmlFor={id} className="label">{label}</label>
      <input id={id} className="input" aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-err` : undefined} {...props} />
      {error && <div id={`${id}-err`} className="text-[13px] text-red-700 mt-1">{error}</div>}
    </div>
  )
}

function Notice({ kind, children }: { kind: 'error' | 'ok'; children: ReactNode }) {
  return <div role={kind === 'error' ? 'alert' : 'status'}
    className={`text-sm rounded-md border px-3 py-2 ${kind === 'error' ? 'border-red-200 bg-red-50 text-red-800' : 'border-emerald-200 bg-emerald-50 text-emerald-800'}`}>{children}</div>
}

// --------------------------------------------------------------------------- přihlášení
export function Login() {
  const { enabled, session } = useAuth()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const next = safeNext(params.get('next'))
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(params.get('vyprselo') ? 'Přihlášení vypršelo, přihlaste se prosím znovu.' : null)
  const [sent, setSent] = useState(false)
  const [touched, setTouched] = useState(false)

  if (!enabled) return <Navigate to="/" replace />
  if (session) return <Navigate to={next} replace />
  const emailErr = touched && !EMAIL_RE.test(email) ? 'Zadejte platný e-mail.' : null

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (!EMAIL_RE.test(email) || !password) return
    setBusy(true); setErr(null)
    const { error } = await supabase!.auth.signInWithPassword({ email: email.trim(), password })
    setBusy(false)
    if (error) setErr(authErrorMessage(error))
    else navigate(next, { replace: true })
  }
  const magic = async () => {
    setTouched(true)
    if (!EMAIL_RE.test(email)) return
    setBusy(true); setErr(null)
    const { error } = await supabase!.auth.signInWithOtp({
      email: email.trim(), options: { shouldCreateUser: false, emailRedirectTo: window.location.origin + next },
    })
    setBusy(false)
    if (error) setErr(authErrorMessage(error)); else setSent(true)
  }
  const google = async () => {
    const { error } = await supabase!.auth.signInWithOAuth({ provider: 'google', options: { redirectTo: window.location.origin + next } })
    if (error) setErr(authErrorMessage(error))
  }

  return (
    <Shell title="Přihlášení" lead="Story2Data generuje propojená data z příběhu. Pro práci se svými běhy se přihlaste."
      footer={<>
        <div><Link className="underline decoration-slate-300 underline-offset-2 hover:decoration-ink" to="/zapomenute-heslo">Zapomněli jste heslo?</Link></div>
        <div>Nemáte účet? <Link className="underline decoration-slate-300 underline-offset-2 hover:decoration-ink" to={`/registrace${next !== '/' ? `?next=${encodeURIComponent(next)}` : ''}`}>Zaregistrujte se</Link></div>
      </>}>
      <form className="space-y-4" onSubmit={submit} noValidate>
        {err && <Notice kind="error">{err}</Notice>}
        {sent && <Notice kind="ok">Poslali jsme vám přihlašovací odkaz. Zkontrolujte e-mail.</Notice>}
        <Field id="email" label="E-mail" type="email" autoComplete="email" value={email} required
          onChange={(e) => setEmail(e.target.value)} onBlur={() => setTouched(true)} error={emailErr} />
        <Field id="password" label="Heslo" type="password" autoComplete="current-password" value={password} required
          onChange={(e) => setPassword(e.target.value)} error={touched && !password ? 'Zadejte heslo.' : null} />
        <button className="btn btn-primary w-full justify-center" disabled={busy}>{busy ? 'Přihlašuji…' : 'Přihlásit se'}</button>
        {(authFeatures.magicLink || authFeatures.google) && <div className="flex items-center gap-3 text-xs text-slate-400"><span className="h-px bg-line flex-1" />nebo<span className="h-px bg-line flex-1" /></div>}
        {authFeatures.magicLink && <button type="button" className="btn w-full justify-center" disabled={busy} onClick={() => void magic()}>Poslat přihlašovací odkaz e-mailem</button>}
        {authFeatures.google && <button type="button" className="btn w-full justify-center" onClick={() => void google()}>Pokračovat přes Google</button>}
      </form>
    </Shell>
  )
}

// --------------------------------------------------------------------------- registrace
export function Register() {
  const { enabled, session } = useAuth()
  const [params] = useSearchParams()
  const next = safeNext(params.get('next'))
  const [mode, setMode] = useState<string | null>(null)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [touched, setTouched] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  useEffect(() => { api.health().then((h) => setMode(h.signup_mode)).catch(() => setMode(null)) }, [])

  if (!enabled) return <Navigate to="/" replace />
  if (session) return <Navigate to={next} replace />
  const emailErr = touched && !EMAIL_RE.test(email) ? 'Zadejte platný e-mail.' : null
  const pwErr = touched ? passwordProblem(password) : null
  const pw2Err = touched && password2 !== password ? 'Hesla se neshodují.' : null

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (!EMAIL_RE.test(email) || passwordProblem(password) || password !== password2) return
    setBusy(true); setErr(null)
    const { data, error } = await supabase!.auth.signUp({
      email: email.trim(), password, options: { emailRedirectTo: `${window.location.origin}/prihlaseni?next=${encodeURIComponent(next)}` },
    })
    setBusy(false)
    if (error) { setErr(authErrorMessage(error)); return }
    if (!data.session) setDone(true)  // e-mail confirmation required (default)
  }

  if (mode === 'invite_only') {
    return <Shell title="Registrace" lead="Registrace je jen na pozvánku. Pokud jste pozvánku dostali, otevřete odkaz z e-mailu."
      footer={<div>Máte účet? <Link className="underline decoration-slate-300 underline-offset-2" to="/prihlaseni">Přihlaste se</Link></div>}><span /></Shell>
  }
  if (done) {
    return <Shell title="Zkontrolujte e-mail" lead={<>Na adresu <strong>{email}</strong> jsme poslali odkaz pro potvrzení registrace. Po potvrzení se můžete přihlásit.</>}
      footer={<Link className="underline decoration-slate-300 underline-offset-2" to="/prihlaseni">Zpět na přihlášení</Link>}><span /></Shell>
  }
  return (
    <Shell title="Registrace" lead={mode === 'email_allowlist'
      ? 'Přístup mají jen povolené e-mailové adresy. Jinak účet počká na schválení správcem.'
      : 'Vytvořte si účet – vaše běhy uvidíte jen vy.'}
      footer={<div>Máte účet? <Link className="underline decoration-slate-300 underline-offset-2 hover:decoration-ink" to="/prihlaseni">Přihlaste se</Link></div>}>
      <form className="space-y-4" onSubmit={submit} noValidate>
        {err && <Notice kind="error">{err}</Notice>}
        <Field id="email" label="E-mail" type="email" autoComplete="email" value={email}
          onChange={(e) => setEmail(e.target.value)} error={emailErr} />
        <Field id="password" label={`Heslo (min. ${MIN_PASSWORD} znaků, písmena a číslice)`} type="password" autoComplete="new-password"
          value={password} onChange={(e) => setPassword(e.target.value)} error={pwErr} />
        <Field id="password2" label="Heslo znovu" type="password" autoComplete="new-password" value={password2}
          onChange={(e) => setPassword2(e.target.value)} error={pw2Err} />
        <button className="btn btn-primary w-full justify-center" disabled={busy}>{busy ? 'Registruji…' : 'Vytvořit účet'}</button>
      </form>
    </Shell>
  )
}

// --------------------------------------------------------------------------- zapomenuté heslo
export function ForgotPassword() {
  const { enabled } = useAuth()
  const [email, setEmail] = useState('')
  const [touched, setTouched] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [sent, setSent] = useState(false)
  if (!enabled) return <Navigate to="/" replace />

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (!EMAIL_RE.test(email)) return
    setBusy(true); setErr(null)
    const { error } = await supabase!.auth.resetPasswordForEmail(email.trim(), { redirectTo: `${window.location.origin}/auth/nastaveni-hesla` })
    setBusy(false)
    // Same answer whether or not the account exists (no account enumeration); only rate limits are reported.
    if (error && (error.status === 429 || error.code?.startsWith('over_'))) setErr(authErrorMessage(error))
    else setSent(true)
  }
  return (
    <Shell title="Zapomenuté heslo" lead="Pošleme vám odkaz, přes který si nastavíte nové heslo."
      footer={<Link className="underline decoration-slate-300 underline-offset-2 hover:decoration-ink" to="/prihlaseni">Zpět na přihlášení</Link>}>
      {sent ? <Notice kind="ok">Pokud účet s adresou {email} existuje, poslali jsme na ni odkaz pro obnovení hesla.</Notice> : (
        <form className="space-y-4" onSubmit={submit} noValidate>
          {err && <Notice kind="error">{err}</Notice>}
          <Field id="email" label="E-mail" type="email" autoComplete="email" value={email}
            onChange={(e) => setEmail(e.target.value)} error={touched && !EMAIL_RE.test(email) ? 'Zadejte platný e-mail.' : null} />
          <button className="btn btn-primary w-full justify-center" disabled={busy}>{busy ? 'Odesílám…' : 'Poslat odkaz'}</button>
        </form>)}
    </Shell>
  )
}

// --------------------------------------------------------------------------- nastavení hesla (z odkazu v e-mailu / pozvánky)
export function ResetPassword() {
  const { enabled, session, loading } = useAuth()
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [touched, setTouched] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [waited, setWaited] = useState(false)
  useEffect(() => { const t = setTimeout(() => setWaited(true), 1500); return () => clearTimeout(t) }, [])
  if (!enabled) return <Navigate to="/" replace />
  if (!session && (loading || !waited)) return <div className="text-slate-500">Ověřuji odkaz…</div>
  if (!session) {
    return <Shell title="Odkaz je neplatný" lead="Odkaz pro nastavení hesla vypršel nebo už byl použit."
      footer={<Link className="underline decoration-slate-300 underline-offset-2" to="/zapomenute-heslo">Vyžádat nový odkaz</Link>}><span /></Shell>
  }
  const pwErr = touched ? passwordProblem(password) : null
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (passwordProblem(password) || password !== password2) return
    setBusy(true); setErr(null)
    const { error } = await supabase!.auth.updateUser({ password })
    setBusy(false)
    if (error) setErr(authErrorMessage(error)); else navigate('/', { replace: true })
  }
  return (
    <Shell title="Nastavení hesla" lead={`Účet ${session.user.email}`}>
      <form className="space-y-4" onSubmit={submit} noValidate>
        {err && <Notice kind="error">{err}</Notice>}
        <Field id="password" label={`Nové heslo (min. ${MIN_PASSWORD} znaků, písmena a číslice)`} type="password" autoComplete="new-password"
          value={password} onChange={(e) => setPassword(e.target.value)} error={pwErr} />
        <Field id="password2" label="Nové heslo znovu" type="password" autoComplete="new-password" value={password2}
          onChange={(e) => setPassword2(e.target.value)} error={touched && password2 !== password ? 'Hesla se neshodují.' : null} />
        <button className="btn btn-primary w-full justify-center" disabled={busy}>{busy ? 'Ukládám…' : 'Uložit heslo'}</button>
      </form>
    </Shell>
  )
}
