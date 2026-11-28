import { type Json } from '../api'

export const isObj = (x: Json) => x !== null && typeof x === 'object' && !Array.isArray(x)

