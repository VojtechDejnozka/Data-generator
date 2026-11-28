import { type Json } from '../api'
import { isObj } from './scmEditOps'

const HINT: Record<string, string> = {
  intercept: 'Absolutní člen', coefficients: 'Koeficienty', sd: 'Směrodatná odchylka', mean: 'Střední hodnota',
  predictor: 'Prediktor (výraz)', expression: 'Výraz', strength: 'Síla vztahu', threshold: 'Práh',
  cutpoints: 'Prahy kategorií', phi: 'Autokorelace', rate: 'Intenzita λ', dispersion: 'Disperze',
  templates: 'Textové šablony podle stavu rodiče', select_by: 'Rodič určující výběr šablony',
  vague_templates: 'Neurčité šablony', explicitness: 'Podíl konkrétních šablon', cases: 'Podmíněné případy',
  when: 'Podmínka', value: 'Hodnota', default: 'Výchozí hodnota', logits: 'Prediktory jednotlivých kategorií',
  p: 'Pravděpodobnost', values: 'Hodnoty', reference: 'Referenční pravděpodobnosti',
  categories: 'Kategorie', probs: 'Pravděpodobnosti', noise: 'Strukturální šum', distribution: 'Rozdělení',
}

const DISTRIBUTIONS: Record<string, Record<string, number>> = {
  normal: { mean: 0, sd: 1 }, uniform: { low: 0, high: 1 }, lognormal: { log_mean: 0, log_sd: 1 },
  exponential: { rate: 1 }, gamma: { shape: 1, scale: 1 }, bernoulli: { p: 0.5 }, binomial: { n: 1, p: 0.5 },
  poisson: { rate: 1 }, negative_binomial: { mean: 1, dispersion: 1 }, categorical: {}, beta: { alpha: 1, beta: 1 },
  student_t: { df: 3, loc: 0, scale: 1 }, constant: { value: 0 },
}

function NumberField({ value, onChange, label }: { value: number | null; onChange: (v: number | null) => void; label: string }) {
  return <input className="input py-0.5 mono" type="number" step="any" aria-label={label} value={value ?? ''}
    onChange={(e) => onChange(Number.isFinite(e.target.valueAsNumber) ? e.target.valueAsNumber : null)} />
}

function Field({ name, value, onChange }: { name: string; value: Json; onChange: (v: Json) => void }) {
  if (typeof value === 'number' || value === null) return <NumberField value={value} onChange={onChange} label={name} />
  if (typeof value === 'boolean') return <input aria-label={name} type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />
  if (typeof value === 'string') return <input aria-label={name} className="input py-0.5 mono" value={value} onChange={(e) => onChange(e.target.value)} />
  if (Array.isArray(value)) {
    if (value.every((item) => typeof item === 'string')) return <textarea aria-label={name} className="input h-16" value={value.join('\n')}
      onChange={(e) => onChange(e.target.value.split('\n'))} />
    return <div className="space-y-1">{value.map((item, i) => <div key={i} className="border-l border-slate-200 pl-2">
      <Field name={name} value={item} onChange={(v) => onChange(value.map((x, j) => i === j ? v : x))} />
    </div>)}</div>
  }
  if (isObj(value)) return <ParamFields value={value} onChange={onChange} />
  return null
}

function ParamFields({ value, onChange }: { value: Json; onChange: (v: Json, key: string) => void }) {
  return <div className="grid sm:grid-cols-[minmax(110px,max-content)_1fr] gap-x-3 gap-y-1 items-baseline">
    {Object.entries(value).map(([key, item]) => <div key={key} className="contents">
      <span className="text-xs text-slate-700">{HINT[key] ?? key}</span>
      <Field name={HINT[key] ?? key} value={item} onChange={(v) => onChange({ ...value, [key]: v }, key)} />
    </div>)}
  </div>
}

function DistributionEditor({ value, categories, onChange }: { value: Json; categories: string[]; onChange: (v: Json) => void }) {
  return <div className="space-y-2">
    <label><span className="label">Rozdělení</span><select className="input max-w-xs" value={value.family} onChange={(e) => {
      const family = e.target.value
      onChange({ family, params: { ...DISTRIBUTIONS[family] }, ...(family === 'categorical' ? {
        categories, probs: categories.map(() => 1 / categories.length),
      } : {}) })
    }}>{Object.keys(DISTRIBUTIONS).filter((family) => family !== 'categorical' || categories.length > 0 || value.family === family)
      .map((family) => <option key={family}>{family}</option>)}</select></label>
    {value.family === 'categorical' ? <ParamFields value={{ categories: value.categories ?? [], probs: value.probs ?? [] }}
      onChange={(v) => onChange({ ...value, ...v })} />
      : <ParamFields value={value.params ?? {}} onChange={(params) => onChange({ ...value, params })} />}
  </div>
}

function CptEditor({ variable, onChange }: { variable: Json; onChange: (params: Json) => void }) {
  const params = variable.mechanism.params
  const categories: string[] = variable.categories?.length ? variable.categories : Object.keys(params.default)
  const rows: Json[] = params.table ?? []
  const aliases: string[] = variable.parents.map((p: Json) => p.alias ?? p.variable)
  const update = (index: number, value: Json) => onChange({ ...params, table: rows.map((row, i) => i === index ? value : row) })
  return <div className="space-y-2">
    <div className="overflow-x-auto"><table className="data">
      <caption className="text-left text-xs mb-1 text-slate-600">Pravděpodobnosti v řádku musí dát dohromady 1. Prázdná podmínka přijímá všechny stavy rodiče.</caption>
      <thead><tr><th>Podmínka</th>{categories.map((c) => <th key={c}>{c}</th>)}<th aria-label="Akce" /></tr></thead>
      <tbody>{[...rows, { probs: params.default }].map((row, i) => {
        const fallback = i === rows.length
        const sum = Object.values(row.probs as Record<string, number>).reduce((a, b) => a + b, 0)
        const valid = Math.abs(sum - 1) <= 1e-6 && Object.values(row.probs).every((v) => typeof v === 'number' && v >= 0)
        return <tr key={fallback ? 'default' : i}>
          <td>{fallback ? 'Výchozí' : aliases.map((alias) => <label key={alias} className="block text-xs">{alias}
            <input className="input py-0.5" value={row.when[alias] ?? ''} onChange={(e) => {
              const when = { ...row.when }
              if (e.target.value) when[alias] = e.target.value
              else delete when[alias]
              update(i, { ...row, when })
            }} />
          </label>)}{!valid && <span className="block text-xs text-red-700">Neplatný součet nebo záporná hodnota</span>}</td>
          {categories.map((category) => <td key={category}><NumberField label={category} value={category in row.probs ? row.probs[category] : 0}
            onChange={(value) => {
              const probs = { ...row.probs, [category]: value }
              if (fallback) onChange({ ...params, default: probs })
              else update(i, { ...row, probs })
            }} /></td>)}
          <td>{!fallback && <button className="btn text-xs" onClick={() => onChange({ ...params, table: rows.filter((_, j) => i !== j) })}>Odebrat</button>}</td>
        </tr>
      })}</tbody>
    </table></div>
    {aliases.length > 0 && <button className="btn text-xs" onClick={() => onChange({ ...params, table: [...rows, { when: {}, probs: { ...params.default } }] })}>Přidat podmínku</button>}
    <ParamFields value={{ strength: params.strength ?? 1, ...(params.reference ? { reference: params.reference } : {}) }}
      onChange={(v) => onChange({ ...params, ...v })} />
  </div>
}

function Coefficients({ values, aliases, onChange }: { values: Json; aliases: string[]; onChange: (v: Json) => void }) {
  const terms = [...new Set([...aliases, ...Object.keys(values)])]
  if (!terms.length) return null
  return <fieldset className="space-y-1">
    <legend className="label">Koeficienty</legend>
    {terms.map((term) => <label key={term} className="grid grid-cols-[1fr_1fr] items-center gap-2 text-xs mono">
      {term}<NumberField label={`Koeficient ${term}`} value={term in values ? values[term] : 0}
        onChange={(n) => onChange({ ...values, [term]: n })} />
    </label>)}
  </fieldset>
}

export default function ScmEditor({ variable, parentTypes, schema, onChange }:
  { variable: Json; parentTypes: Map<string, string>; schema?: Json; onChange: (v: Json) => void }) {
  const params = variable.mechanism.params
  const setParams = (p: Json) => onChange({ ...variable, mechanism: { ...variable.mechanism, params: p } })
  if (variable.mechanism.type === 'categorical_cpt') return <CptEditor variable={variable} onChange={setParams} />
  const aliases: string[] = variable.parents.filter((p: Json) => ['float', 'int', 'bool'].includes(parentTypes.get(p.variable) ?? ''))
    .map((p: Json) => p.alias ?? p.variable)
  if (variable.mechanism.type === 'categorical_softmax') return <div className="grid lg:grid-cols-2 gap-3">
    {(variable.categories as string[]).map((category) => {
      const predictor = params.logits[category] ?? {}
      const setPredictor = (next: Json) => setParams({ ...params, logits: { ...params.logits, [category]: next } })
      return <fieldset className="border border-slate-200 p-2 space-y-2" key={category}>
        <legend className="label px-1">{category}</legend>
        <ParamFields value={predictor.predictor ? { predictor: predictor.predictor }
          : { intercept: predictor.intercept ?? 0, predictor: '' }}
          onChange={(v, key) => setPredictor({ ...predictor, [key]: v[key] })} />
        {!predictor.predictor && <Coefficients values={predictor.coefficients ?? {}} aliases={aliases}
          onChange={(coefficients) => setPredictor({ ...predictor, coefficients })} />}
      </fieldset>
    })}
  </div>
  const properties = schema?.properties ?? {}
  const { noise, distribution, coefficients, ...rest } = params
  const fields = { ...Object.fromEntries(Object.entries<Json>(properties).filter(([, p]) => p.default != null).map(([k, p]) => [k, p.default])), ...rest }
  for (const key of ['predictor', 'select_by']) if (key in properties) fields[key] = fields[key] ?? ''
  const scalarFields = Object.fromEntries(Object.entries(fields).filter(([k]) =>
    !['noise', 'distribution', 'coefficients'].includes(k) && (k !== 'intercept' || !fields.predictor)))
  return <div className="grid lg:grid-cols-2 gap-5">
    <div className="space-y-2">
      {Object.keys(scalarFields).length > 0 && <ParamFields value={scalarFields} onChange={(v, key) => setParams({
        ...params, [key]: key === 'select_by' && v[key] === '' ? null : v[key],
      })} />}
      {!fields.predictor && (coefficients || 'coefficients' in properties) && <Coefficients values={coefficients ?? {}} aliases={aliases}
        onChange={(v) => setParams({ ...params, coefficients: v })} />}
    </div>
    {distribution && <DistributionEditor value={distribution} categories={variable.categories ?? []} onChange={(v) => setParams({ ...params, distribution: v })} />}
    {(noise || 'noise' in properties) && <div className="space-y-2">
      <label className="text-xs font-semibold"><input type="checkbox" checked={!!noise} onChange={(e) => setParams({ ...params,
        noise: e.target.checked ? { family: 'normal', params: { mean: 0, sd: 1 } } : null })} /> Strukturální šum</label>
      {noise && <DistributionEditor value={noise} categories={[]} onChange={(v) => setParams({ ...params, noise: v })} />}
    </div>}
  </div>
}
