import { narrativeDigest } from './approvedReportNarrative.js'

const BINDINGS = ['report_version_id', 'report_content_hash', 'anxin_board_report_hash', 'approval_snapshot_id', 'approval_snapshot_hash', 'profile_id', 'profile_content_hash']
const KEYS = ['schema_version', 'project_id', ...BINDINGS, 'source', 'attribution', 'confirmed_by', 'confirmed_at', 'cross_project_reuse_ack', 'historical_baseline', 'approved_git_head', 'modules', 'idempotency_key', 'module_narrative_hash']
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key))
const text = value => typeof value === 'string' && value.trim().length > 0
const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const positive = value => Number.isSafeInteger(value) && value > 0

export async function validateModuleNarrative(value, projectId, report) {
  const reject = () => { throw new Error('MODULE_NARRATIVE_BINDING_INVALID') }
  if (!exact(value, KEYS) || value.schema_version !== 'approved_module_narrative_v1' ||
      String(value.project_id) !== String(projectId) || !BINDINGS.every(key => value[key] === report[key])) reject()
  if (!exact(value.source, ['project_id', 'profile_id', 'profile_content_hash', 'task_id', 'task_identity_hash', 'output_hash', 'exact_head']) ||
      !positive(value.source.project_id) || !positive(value.source.profile_id) || !text(value.source.task_id) ||
      !['profile_content_hash', 'task_identity_hash', 'output_hash'].every(key => hash(value.source[key])) ||
      !/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(value.source.exact_head)) reject()
  if (value.attribution !== '已确认的模块说明' || ![value.confirmed_by, value.confirmed_at, value.idempotency_key].every(text) ||
      typeof value.cross_project_reuse_ack !== 'boolean' || typeof value.historical_baseline !== 'boolean' ||
      value.approved_git_head !== report.git_to_commit ||
      value.historical_baseline !== (value.source.exact_head !== value.approved_git_head) ||
      (value.source.project_id !== value.project_id && !value.cross_project_reuse_ack)) reject()
  if (!Array.isArray(value.modules) || value.modules.length !== report.modules.length) reject()
  for (const [index, row] of value.modules.entries()) {
    if (!exact(row, ['module_id', 'summary', 'remaining', 'requirement_refs', 'source_requirements']) ||
        row.module_id !== report.modules[index].module_id || !text(row.summary) || !text(row.remaining) ||
        !Array.isArray(row.requirement_refs) || !row.requirement_refs.length || row.requirement_refs.length > 100 ||
        !row.requirement_refs.every(ref => Number.isInteger(ref) && ref >= 0) ||
        new Set(row.requirement_refs).size !== row.requirement_refs.length ||
        !Array.isArray(row.source_requirements) || row.source_requirements.length < row.requirement_refs.length ||
        row.source_requirements.length > 100 || !row.requirement_refs.every(ref => ref < row.source_requirements.length)) reject()
    const indexes = new Set()
    for (const original of row.source_requirements) {
      if (!exact(original, ['requirement_index', 'requirement_text', 'status', 'rationale', 'evidence_ids']) ||
          !Number.isInteger(original.requirement_index) || original.requirement_index < 0 ||
          original.requirement_index >= row.source_requirements.length || indexes.has(original.requirement_index) ||
          !text(original.requirement_text) || !text(original.rationale) ||
          !['implemented', 'partial', 'unknown'].includes(original.status) ||
          !Array.isArray(original.evidence_ids) || original.evidence_ids.length > 100 ||
          new Set(original.evidence_ids).size !== original.evidence_ids.length ||
          !original.evidence_ids.every(ref => typeof ref === 'string' && /^repo-code-[A-Za-z0-9_-]{1,128}$/.test(ref)) ||
          (original.status !== 'unknown' && !original.evidence_ids.length)) reject()
      indexes.add(original.requirement_index)
    }
  }
  const { module_narrative_hash, ...unsigned } = value
  if (!hash(module_narrative_hash) || await narrativeDigest(unsigned) !== module_narrative_hash) reject()
  return value
}

export function createModuleNarrativeLoader(fetchNarrative, publish) {
  let generation = 0
  return {
    cancel() { generation += 1 },
    async load(projectId, report) {
      const current = ++generation
      if (!report || report.schema_version !== 'anxin_board_report_v3') {
        publish({ state: 'absent', value: null }); return
      }
      publish({ state: 'loading', value: null })
      try {
        const response = await fetchNarrative(projectId, report.report_version_id, report.anxin_board_report_hash)
        if (current !== generation) return
        if (!response.ok) throw new Error('MODULE_NARRATIVE_READ_FAILED')
        const value = response.body === null ? null : await validateModuleNarrative(response.body, projectId, report)
        if (current === generation) publish({ state: value ? 'loaded' : 'absent', value })
      } catch {
        if (current === generation) publish({ state: 'failed', value: null })
      }
    }
  }
}
