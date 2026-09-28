// Presentation only: counts require a complete, identity-bound requirement result.
export function summarizeModuleMappings(content) {
  if (content?.schema_version !== 'project_profile_v2') return null
  const modules = content.planned_modules
  const mappings = content.implementation_mappings
  if (!Array.isArray(modules) || !modules.length || !Array.isArray(mappings) || mappings.length !== modules.length) return null
  const remaining = new Set()
  for (const module of modules) {
    const id = module?.client_id
    if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(id) || remaining.has(id)) return null
    remaining.add(id)
  }
  const counts = { total: modules.length, implemented: 0, partial: 0, unknown: 0, not_started: 0 }
  for (const mapping of mappings) {
    if (!remaining.has(mapping?.planned_module_id) || !['implemented', 'partial', 'unknown', 'not_started'].includes(mapping?.status)) return null
    remaining.delete(mapping.planned_module_id)
    counts[mapping.status]++
  }
  return remaining.size === 0 ? counts : null
}

export function summarizeRequirementRows(rows, total) {
  if (!Number.isInteger(total) || total < 0 || !Array.isArray(rows) || rows.length !== total) return null
  const counts = { total, implemented: 0, partial: 0, unknown: 0 }
  const seen = new Set()
  for (const row of rows) {
    if (!Number.isInteger(row?.requirement_index) || row.requirement_index < 0 || row.requirement_index >= total || seen.has(row.requirement_index) || !['implemented', 'partial', 'unknown'].includes(row.status)) return null
    seen.add(row.requirement_index)
    counts[row.status]++
  }
  return counts
}

export function summarizeAtlasRequirements(task, profile, hasUnsaved = false) {
  if (hasUnsaved || task?.status !== 'succeeded' || task.profile_id == null || profile?.id == null || String(task.profile_id) !== String(profile.id) || typeof task.generated_content_hash !== 'string' || !/^[0-9a-f]{64}$/.test(task.generated_content_hash) || task.generated_content_hash !== profile.content_hash) return null
  const modules = profile.content?.planned_modules
  if (!Array.isArray(modules) || !modules.length) return null
  const counts = { total: 0, implemented: 0, partial: 0, unknown: 0 }
  for (const module of modules) {
    if (!Array.isArray(module.requirements)) return null
    const summary = summarizeRequirementRows(task.module_requirements?.[module.client_id], module.requirements.length)
    if (!summary) return null
    for (const key of Object.keys(counts)) counts[key] += summary[key]
  }
  return counts
}

export function atlasTaskPresentation(status, preflightReady = false) {
  const presentation = ({
    queued: { label: '等待分析', tone: 'info' },
    running: { label: '正在核对代码证据', tone: 'info' },
    succeeded: { label: '分析已完成', tone: 'success' },
    unknown: { label: '分析结果暂无法确认', tone: 'warning' },
    failed_pre_send: { label: '分析准备未完成', tone: 'error' },
    failed_after_send: { label: '分析已停止', tone: 'error' }
  })[status] || { label: '任务状态暂不可用', tone: 'warning' }
  if (preflightReady && ['succeeded', 'unknown', 'failed_pre_send', 'failed_after_send'].includes(status)) {
    return { ...presentation, label: '上次' + presentation.label, previousRecord: true, foldPreviousRecord: status !== 'unknown' }
  }
  return presentation
}
