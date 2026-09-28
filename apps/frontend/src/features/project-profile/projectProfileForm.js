export const PROJECT_PROFILE_SCHEMA_VERSION = 'project_profile_manual_v1'

function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys)
  if (value && typeof value === 'object') {
    return Object.keys(value)
      .sort()
      .reduce((acc, key) => {
        acc[key] = sortKeys(value[key])
        return acc
      }, {})
  }
  return value
}

function canonicalJson(value) {
  return JSON.stringify(sortKeys(value))
}

function toArray(value) {
  return Array.isArray(value) ? value : []
}

function objectEntries(value) {
  return toArray(value).filter((item) => item && typeof item === 'object')
}

function linesToArray(text) {
  return String(text || '')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

function arrayToLines(arr) {
  return Array.isArray(arr) ? arr.join('\n') : ''
}

export function createEmptyProfileForm() {
  return {
    schema_version: PROJECT_PROFILE_SCHEMA_VERSION,
    project_summary: '',
    modules: [],
    domain_glossary: [],
    exclude_patterns_text: '',
    notes: ''
  }
}

export function createEmptyProfileContent() {
  return {
    schema_version: PROJECT_PROFILE_SCHEMA_VERSION,
    project_summary: '',
    modules: [],
    domain_glossary: [],
    exclude_patterns: [],
    notes: ''
  }
}

export function profileContentToForm(content) {
  const c = content || {}
  if (c.schema_version === "project_profile_v2") {
    const base = profileContentToForm({ ...c, schema_version: PROJECT_PROFILE_SCHEMA_VERSION, modules: c.planned_modules })
    return { ...base, schema_version: c.schema_version, original_planned_modules: cloneProfileValue(c.planned_modules || []), implementation_mappings: cloneProfileValue(c.implementation_mappings || []), unplanned_code_features: cloneProfileValue(c.unplanned_code_features || []) }
  }
  return {
    schema_version: PROJECT_PROFILE_SCHEMA_VERSION,
    project_summary: c.project_summary || '',
    modules: objectEntries(c.modules).map((m) => ({
      client_id: m.client_id || createProfileClientId(),
      name: m.name || '',
      description: m.description || '',
      prd_refs_text: arrayToLines(m.prd_refs),
      requirements_text: arrayToLines(m.requirements),
      paths: objectEntries(m.paths).map((p) => ({
        type: p.type || 'other',
        pattern: p.pattern || '',
        required: p.required !== false,
        note: p.note || ''
      })),
      exclusions_text: arrayToLines(m.exclusions)
    })),
    domain_glossary: objectEntries(c.domain_glossary).map((g) => ({
      term: g.term || '',
      definition: g.definition || '',
      aliases_text: arrayToLines(g.aliases)
    })),
    exclude_patterns_text: arrayToLines(c.exclude_patterns),
    notes: c.notes || ''
  }
}

export function profileFormToContent(form) {
  const f = form && typeof form === 'object' ? form : {}
  if (f.schema_version === "project_profile_v2") {
    const { modules, ...base } = profileFormToContent({ ...f, schema_version: PROJECT_PROFILE_SCHEMA_VERSION })
    const planned = modules.map(({ paths, ...m }) => m)
    const originals = new Map((f.original_planned_modules || []).map(m => [m.client_id, canonicalJson(m)]))
    const unchanged = new Set(planned.filter(m => originals.get(m.client_id) === canonicalJson(m)).map(m => m.client_id))
    return { ...base, schema_version: f.schema_version, planned_modules: planned, implementation_mappings: cloneProfileValue(f.implementation_mappings || []).filter(m => unchanged.has(m.planned_module_id)), unplanned_code_features: cloneProfileValue(f.unplanned_code_features || []) }
  }
  return {
    schema_version: PROJECT_PROFILE_SCHEMA_VERSION,
    project_summary: f.project_summary || '',
    modules: objectEntries(f.modules).map((m) => ({
      client_id: m.client_id,
      name: m.name || '',
      description: m.description || '',
      prd_refs: linesToArray(m.prd_refs_text),
      requirements: linesToArray(m.requirements_text),
      paths: objectEntries(m.paths).map((p) => ({
        type: p.type,
        pattern: p.pattern || '',
        required: !!p.required,
        note: p.note || ''
      })),
      exclusions: linesToArray(m.exclusions_text)
    })),
    domain_glossary: objectEntries(f.domain_glossary).map((g) => ({
      term: g.term || '',
      definition: g.definition || '',
      aliases: linesToArray(g.aliases_text)
    })),
    exclude_patterns: linesToArray(f.exclude_patterns_text),
    notes: f.notes || ''
  }
}

export function canonicalizeProfileContent(content) {
  return canonicalJson(content)
}

export function cloneProfileValue(value) {
  return JSON.parse(JSON.stringify(value))
}

export function createProfileClientId() {
  const random = Math.random().toString(36).slice(2, 10)
  return `mod-${Date.now().toString(36)}-${random}`
}

export function plannedModules(content) { return content?.schema_version === "project_profile_v2" ? (content.planned_modules || []) : (content?.modules || []) }
export function implementationLabel(status) { return ({not_started:"尚未开始",partial:"部分实现",implemented:"已实现",unknown:"暂时无法确认"})[status] || "暂时无法确认" }
