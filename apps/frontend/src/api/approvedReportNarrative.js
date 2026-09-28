const CONTENT_KEYS = ['plain_summary', 'code_change_summary', 'test_evidence', 'risks', 'unknown_items', 'source_warnings']
const BINDINGS = ['anxin_board_report_hash', 'approval_snapshot_id', 'approval_snapshot_hash', 'report_version_id', 'report_content_hash', 'model_execution_result_id', 'execution_result_hash']
const SOURCES = { git_fact: '代码变更记录', prd_fact: '需求说明', ai_analysis: 'AI 分析', pm_external_fact: '项目经理提供', fixed_disclaimer: '固定提示' }
const SCOPES = { 前端: '页面与交互', 后端: '服务端逻辑', 数据库: '数据存储', 接口: '系统接口', 测试: '测试相关代码', 配置: '运行配置', 跨模块: '多个功能', 暂时无法确认: '涉及范围待核实' }
export const narrativeSourceLabel = value => SOURCES[value] || '来源待核实'
export const narrativeScopeLabel = value => SCOPES[value] || '涉及范围待核实'
export const narrativeRiskLabel = value => value === 'needs_client_action' ? '需甲方协助' : '尚待验证的分析判断'
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every(k => Object.hasOwn(value, k))
export function canonicalNarrativeJSON(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalNarrativeJSON).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonicalNarrativeJSON(value[k])}`).join(',')}}`
  if (typeof value === 'number' && !Number.isSafeInteger(value)) throw new Error('NARRATIVE_INVALID')
  return JSON.stringify(value)
}
export async function narrativeDigest(value) {
  const bytes = new TextEncoder().encode(canonicalNarrativeJSON(value))
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
}
export async function validateNarrative(value, projectId, report) {
  const reject = () => { throw new Error('NARRATIVE_BINDING_INVALID') }
  if (!exact(value, ['schema_version', 'project_id', ...BINDINGS, 'source_task_type', 'source_result', 'content', 'narrative_hash'])) reject()
  if (value.schema_version !== 'approved_report_narrative_v1' || String(value.project_id) !== String(projectId) || !BINDINGS.every(k => value[k] === report[k])) reject()
  if (!exact(value.content, CONTENT_KEYS)) reject()
  let source = value.source_result
  if (value.source_task_type === 'daily_report_regenerate') {
    if (!exact(source, ['new_report', 'correction_trace']) || !Array.isArray(source.correction_trace)) reject()
    source = source.new_report
  } else if (value.source_task_type !== 'daily_report_generate') reject()
  if (!exact(source, [...CONTENT_KEYS, 'feature_progress'])) reject()
  if (!Array.isArray(source.feature_progress) || source.feature_progress.length > 200) reject()
  for (const row of source.feature_progress) {
    if (!exact(row, ['feature', 'stage', 'source_type', 'implementation_scope', 'evidence_ids']) ||
      typeof row.feature !== 'string' || !row.feature.trim() ||
      !['开发中', '等待联调', '等待测试', '测试中', '已完成', '暂时无法确认'].includes(row.stage) ||
      !Object.hasOwn(SOURCES, row.source_type) || !Object.hasOwn(SCOPES, row.implementation_scope) ||
      !Array.isArray(row.evidence_ids) || !row.evidence_ids.length ||
      !row.evidence_ids.every(id => typeof id === 'string' && id.trim())) reject()
  }
  if (typeof value.content.plain_summary !== 'string' || !value.content.plain_summary.trim() || [...value.content.plain_summary].length > 12000) reject()
  for (const key of CONTENT_KEYS.slice(1)) {
    const rows = value.content[key]
    if (!Array.isArray(rows) || rows.length > 200) reject()
    for (const row of rows) {
      const keys = ['content', 'source_type', 'evidence_ids', ...(key === 'code_change_summary' ? ['implementation_scope'] : []), ...(key === 'risks' ? ['risk_level'] : [])]
      if (!exact(row, keys) || typeof row.content !== 'string' || !row.content.trim() || !Object.hasOwn(SOURCES, row.source_type) || !Array.isArray(row.evidence_ids) || !row.evidence_ids.length || !row.evidence_ids.every(id => typeof id === 'string' && id.trim())) reject()
      if (key === 'code_change_summary' && !Object.hasOwn(SCOPES, row.implementation_scope)) reject()
      if (key === 'risks' && !['suspected', 'needs_client_action'].includes(row.risk_level)) reject()
    }
  }
  if (!CONTENT_KEYS.every(k => canonicalNarrativeJSON(value.content[k]) === canonicalNarrativeJSON(source[k]))) reject()
  if (await narrativeDigest(value.source_result) !== report.report_content_hash) reject()
  const { narrative_hash, ...unsigned } = value
  if (await narrativeDigest(unsigned) !== narrative_hash) reject()
  return value.content
}
// Display only: never infer a module identity or change its frozen stage.
export function unassociatedNarrativeFeatures(value, report) {
  const source = value.source_task_type === 'daily_report_regenerate' ? value.source_result.new_report : value.source_result
  const rows = source.feature_progress
  const names = (report.modules || []).map(module => module.name)
  return rows.filter(row => names.filter(name => name === row.feature).length !== 1 ||
    rows.filter(other => other.feature === row.feature).length !== 1)
}
// Each selection gets its own generation; no response can populate a newer report.
export function createNarrativeLoader(fetchNarrative, publish) {
  let generation = 0
  return {
    cancel() { generation += 1 },
    async load(projectId, report) {
      const current = ++generation
      if (!report || report.schema_version !== 'anxin_board_report_v3') {
        publish({ state: report ? 'legacy' : 'idle', content: null }); return
      }
      publish({ state: 'loading', content: null })
      try {
        const response = await fetchNarrative(projectId, report.report_version_id, report.anxin_board_report_hash)
        if (current !== generation) return
        if (!response.ok) throw new Error('NARRATIVE_READ_FAILED')
        const content = await validateNarrative(response.body, projectId, report)
        if (current === generation) publish({ state: 'loaded', content, unassociatedFeatures: unassociatedNarrativeFeatures(response.body, report) })
      } catch {
        if (current === generation) publish({ state: 'failed', content: null })
      }
    }
  }
}
