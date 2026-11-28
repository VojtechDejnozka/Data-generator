// Účet (profil, kvóta, změna hesla), správa uživatelů (admin) a veřejný read-only náhled sdíleného běhu.
import { useEffect, useState, type FormEvent } from 'react'
import { useParams } from 'react-router-dom'
import { api, fmt, setShareToken, type Json, type Me } from '../api'
import { useAuth } from '../auth/AuthContext'
import { authErrorMessage, supabase } from '../auth/supabase'
import RunView from './RunView'

export function QuotaLine({ q }: { q: Me['quota'] }) {
  if (q.unlimited) return <span>bez limitu (správce)</span>
  const k = (n: number) => (n >= 1000 ? `${Math.round(n / 1000).toLocaleString('cs-CZ')} tis.` : String(n))
  return <span title={`Obnoví se ${new Date(q.resets_at).toLocaleString('cs-CZ')}`}>
    zbývá {q.runs_left}/{q.runs_limit} běhů · {k(q.tokens_left ?? 0)}/{k(q.tokens_limit)} tokenů dnes
  </span>
}

export function Account() {
  const { me, refreshMe, signOut, enabled, session } = useAuth()
  const [name, setName] = useState('')
  const [saved, setSaved] = useState(false)
  const [pw, setPw] = useState('')
  const [pwMsg, setPwMsg] = useState<string | null>(null)
  useEffect(() => { if (me) setName(me.display_name) }, [me])
  if (!me) return <div className="text-slate-500">Načítám účet…</div>

  const saveName = async (e: FormEvent) => {
    e.preventDefault()
    await api.updateMe(name)
    setSaved(true)
    void refreshMe()
  }
  const changePw = async (e: FormEvent) => {
    e.preventDefault()
    if (pw.length < 8 || !/\d/.test(pw)) { setPwMsg('Heslo musí mít alespoň 8 znaků a obsahovat číslici.'); return }
    const { error } = await supabase!.auth.updateUser({ password: pw })
    setPwMsg(error ? authErrorMessage(error) : 'Heslo bylo změněno.')
    if (!error) setPw('')
  }
  const q = me.quota
  return (
    <div className="max-w-xl space-y-8">
      <div>
        <h1 className="text-[28px] font-semibold tracking-[-0.015em]">Účet</h1>
        <p className="text-slate-600">{session?.user.email ?? me.email} · {me.role === 'admin' ? 'správce' : 'uživatel'}</p>
      </div>
      <section className="space-y-2">
        <h2 className="font-semibold">Denní limit</h2>
        {q.unlimited ? <p className="text-slate-600">Správce nemá denní limit.</p> : (
          <table className="data max-w-sm"><tbody>
            <tr><td>Běhy generátoru</td><td className="text-right">{q.runs_used} / {q.runs_limit}</td></tr>
            <tr><td>Tokeny LLM</td><td className="text-right">{fmt(q.tokens_used)} / {fmt(q.tokens_limit)}</td></tr>
            <tr><td>Obnovení</td><td className="text-right">{new Date(q.resets_at).toLocaleString('cs-CZ')}</td></tr>
          </tbody></table>)}
        <p className="text-xs text-slate-500">Limit chrání sdílený klíč k LLM API. Mock režim tokeny nepočítá.</p>
      </section>
      <form className="space-y-2" onSubmit={saveName}>
        <h2 className="font-semibold">Zobrazované jméno</h2>
        <div className="flex gap-2"><input className="input" maxLength={80} value={name} onChange={(e) => { setName(e.target.value); setSaved(false) }} />
          <button className="btn">Uložit</button></div>
        {saved && <div className="text-sm text-emerald-800" role="status">Uloženo.</div>}
      </form>
      {enabled && (
        <form className="space-y-2" onSubmit={changePw}>
          <h2 className="font-semibold">Změna hesla</h2>
          <div className="flex gap-2"><input className="input" type="password" autoComplete="new-password" placeholder="Nové heslo"
            value={pw} onChange={(e) => setPw(e.target.value)} /><button className="btn">Změnit</button></div>
          {pwMsg && <div className="text-sm text-slate-700" role="status">{pwMsg}</div>}
        </form>)}
      {enabled && <button className="btn btn-danger" onClick={() => void signOut()}>Odhlásit se</button>}
    </div>
  )
}

export function Admin() {
  const [data, setData] = useState<Json>(null)
  const [err, setErr] = useState<string | null>(null)
  const [email, setEmail] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const load = () => api.adminUsers().then(setData).catch((e) => setErr(String(e)))
  useEffect(() => { void load() }, [])
  const patch = async (id: string, p: Json) => {
    try { await api.adminUpdateUser(id, p); await load() } catch (e) { setErr(String(e)) }
  }
  const invite = async (e: FormEvent) => {
    e.preventDefault()
    setMsg(null)
    try { await api.adminInvite(email); setMsg(`Pozvánka odeslána na ${email}.`); setEmail(''); await load() } catch (e2) { setMsg(String(e2)) }
  }
  if (err) return <div className="text-red-700">{err}</div>
  if (!data) return <div className="text-slate-500">Načítám uživatele…</div>
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline gap-x-6">
        <h1 className="text-[28px] font-semibold tracking-[-0.015em]">Uživatelé</h1>
        <span className="text-sm text-slate-500">režim registrace: <span className="mono">{data.signup_mode}</span> · dnes {fmt(data.totals_today.tokens)} tokenů, ${data.totals_today.cost_usd}</span>
      </div>
      <form className="flex gap-2 max-w-md" onSubmit={invite}>
        <input className="input" type="email" placeholder="e-mail pro pozvánku" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <button className="btn">Pozvat</button>
      </form>
      {msg && <div className="text-sm text-slate-700" role="status">{msg}</div>}
      <div className="overflow-x-auto">
        <table className="data">
          <thead><tr><th>E-mail</th><th>Role</th><th>Stav</th><th>Schválen</th><th className="text-right">Běhy dnes</th><th className="text-right">Tokeny dnes</th><th className="text-right">Cena dnes</th><th>Registrace</th><th /></tr></thead>
          <tbody>{data.users.map((u: Json) => (
            <tr key={u.id}>
              <td>{u.email}{u.display_name && <span className="text-slate-500"> · {u.display_name}</span>}</td>
              <td>{u.role === 'admin' ? 'správce' : 'uživatel'}</td>
              <td className={u.status === 'blocked' ? 'text-red-700' : ''}>{u.status === 'blocked' ? 'zablokován' : 'aktivní'}</td>
              <td>{u.approved ? 'ano' : 'ne'}</td>
              <td className="text-right">{u.today.runs}</td>
              <td className="text-right">{fmt(u.today.tokens)}</td>
              <td className="text-right">${u.today.cost_usd}</td>
              <td className="text-slate-500 text-xs">{u.created_at ? new Date(u.created_at).toLocaleDateString('cs-CZ') : '–'}</td>
              <td className="whitespace-nowrap space-x-1">
                {!u.approved && <button className="btn text-xs" onClick={() => void patch(u.id, { approved: true })}>Schválit</button>}
                {u.status === 'blocked'
                  ? <button className="btn text-xs" onClick={() => void patch(u.id, { status: 'active' })}>Odblokovat</button>
                  : <button className="btn btn-danger text-xs" onClick={() => { if (confirm(`Zablokovat ${u.email}?`)) void patch(u.id, { status: 'blocked' }) }}>Zablokovat</button>}
                {u.role !== 'admin' && <button className="btn text-xs" onClick={() => { if (confirm(`Udělat ${u.email} správcem?`)) void patch(u.id, { role: 'admin' }) }}>Správce</button>}
              </td>
            </tr>))}</tbody>
        </table>
      </div>
    </div>
  )
}

/** Public read-only view of a shared run (/s/:token). No account needed. */
export function SharedRun() {
  const { token } = useParams()
  const [ready, setReady] = useState(false)
  const [rid, setRid] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    setShareToken(token ?? null)
    setReady(true)
    api.run('shared').then((r) => setRid(r.run_id)).catch((e) => setErr(String(e)))
    return () => setShareToken(null)
  }, [token])
  if (err) return <div className="max-w-md"><h1 className="text-[28px] font-semibold">Sdílený běh není dostupný</h1><p className="text-slate-600">{err}</p></div>
  if (!ready || !rid) return <div className="text-slate-500">Načítám sdílený běh…</div>
  return <RunView runId={rid} shared />
}
