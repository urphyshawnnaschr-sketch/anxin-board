import { summarizeModuleMappings } from './projectProfilePresentation.js'

export function projectBaselineNextAction({ setupReady, moduleReady, baselineStatus, baselineProfileId, confirmedProfile }) {
  if (!setupReady) return { section: 'setup', key: 'setup', label: '下一步：准备项目 →', title: '准备项目资料', description: '确认仓库信息并上传、确认当前 PRD。' }
  if (!moduleReady) return { section: 'modules', key: 'modules', label: '下一步：确认功能模块 →', title: '确认功能模块', description: '检查 AI 建议并确认本项目功能模块。' }
  const boundProfile = confirmedProfile?.status === 'confirmed' && confirmedProfile.id != null
    && baselineProfileId != null && String(confirmedProfile.id) === String(baselineProfileId)
  const mappings = boundProfile ? summarizeModuleMappings(confirmedProfile.content) : null
  if (baselineStatus === 'established' && mappings && mappings.unknown === mappings.total) {
    return { section: 'modules', key: 'full-analysis', title: '核对代码范围并全量分析',
      label: '核对代码范围并全量分析 →', description: '历史记录已确认，但所有模块的实现情况仍待核实。请核对代码范围，再对照需求进行全量分析。' }
  }
  if (baselineStatus === 'established') return { section: 'git', key: 'git', label: '下一步：开始研发分析 →', title: '开始本次研发分析', description: '先确认本轮研发范围，确认后新提交不会混入本轮报告。' }
  if (baselineStatus === 'candidate_pending') return { section: 'modules', key: 'baseline', label: '下一步：确认分析台账候选 →', title: '确认分析台账候选', description: '全仓分析已完成，先检查并确认当前实现候选，再进入日常增量分析。' }
  if (baselineStatus === 'upgrade_required') return { section: 'modules', key: 'baseline', label: '下一步：升级并建立分析台账 →', title: '升级功能档案并建立分析台账', description: '当前还是旧版功能档案；先升级为 V2，再对当前 HEAD 做一次全量代码分析。' }
  return { section: 'modules', key: 'baseline', label: '下一步：首次建立分析台账 →', title: '首次建立分析台账', description: '先对当前 HEAD 做一次全量分析并人工确认，之后才进入日常增量研发分析。' }
}
