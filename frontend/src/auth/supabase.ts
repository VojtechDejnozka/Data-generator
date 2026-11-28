// Supabase client for the browser: publishable (anon) key only – never a secret key.
import { createClient, type SupabaseClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined
const key = (import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY ?? import.meta.env.VITE_SUPABASE_ANON_KEY) as string | undefined

/** False in local dev without Supabase (backend runs with AUTH_DISABLED=1). */
export const authEnabled = Boolean(url && key)

export const supabase: SupabaseClient | null = authEnabled
  ? createClient(url!, key!, { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } })
  : null

export const authFeatures = {
  google: import.meta.env.VITE_AUTH_GOOGLE === '1',
  magicLink: import.meta.env.VITE_AUTH_MAGIC_LINK === '1',
}

/** Current access token; supabase-js refreshes an expired session transparently. */
export async function accessToken(): Promise<string | null> {
  if (!supabase) return null
  const { data } = await supabase.auth.getSession()
  return data.session?.access_token ?? null
}

/** Only same-app relative paths are allowed as post-login targets (no open redirects). */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return '/'
  return next
}

/** Czech messages for Supabase Auth errors. */
export function authErrorMessage(e: unknown): string {
  const err = e as { code?: string; status?: number; message?: string }
  const code = err?.code ?? ''
  const msg = (err?.message ?? '').toLowerCase()
  if (code === 'invalid_credentials' || msg.includes('invalid login credentials')) return 'Nesprávný e-mail nebo heslo.'
  if (code === 'email_not_confirmed' || msg.includes('email not confirmed')) return 'E-mail zatím není potvrzený. Otevřete odkaz z potvrzovacího e-mailu.'
  if (code === 'user_already_exists' || msg.includes('already registered')) return 'Účet s tímto e-mailem už existuje. Zkuste se přihlásit nebo obnovit heslo.'
  if (code === 'weak_password' || msg.includes('password should')) return 'Heslo je příliš slabé – použijte alespoň 8 znaků a kombinaci písmen a číslic.'
  if (code === 'same_password') return 'Nové heslo musí být jiné než současné.'
  if (code === 'signup_disabled' || msg.includes('signups not allowed')) return 'Registrace je vypnutá. Požádejte správce o pozvánku.'
  if (code === 'otp_expired' || msg.includes('expired')) return 'Odkaz vypršel. Vyžádejte si nový.'
  if (code.startsWith('over_') || err?.status === 429) return 'Příliš mnoho pokusů. Zkuste to za chvíli znovu.'
  if (msg.includes('failed to fetch') || msg.includes('network')) return 'Nepodařilo se spojit se serverem. Zkontrolujte připojení.'
  return 'Akce se nezdařila. Zkuste to prosím znovu.'
}
