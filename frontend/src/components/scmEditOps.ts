// Pure helpers for SCM edit operations (POST /api/scm/edit).
import { type Json } from '../api'

export const isObj = (x: Json) => x !== null && typeof x === 'object' && !Array.isArray(x)

/** set_parameter operations that turn ``a`` into ``b`` (leaf paths; arrays are leaves). */
export function diffParams(a: Json, b: Json, prefix = ''): { path: string; value: Json }[] {
  if (isObj(a) && isObj(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)])
    return [...keys].flatMap((k) => diffParams(a[k], b[k], prefix ? `${prefix}.${k}` : k))
  }
  if (JSON.stringify(a) === JSON.stringify(b)) return []
  return [{ path: prefix, value: b === undefined ? null : b }]
}

