import { narrativeDigest } from './approvedReportNarrative.js'
import { localReadHeaders, handleLocalSessionFailure } from './localSession.js'

const BINDINGS = ['report_version_id', 'anxin_board_report_hash', 'approval_snapshot_id', 'approval_snapshot_hash', 'git_snapshot_id', 'git_facts_hash']
const SOURCE_KEYS = ['branch', 'from_commit', 'to_commit', 'commits', 'commit_count', 'changed_file_count', 'added_lines', 'deleted_lines', 'diff_bytes']
const METRIC_KEYS = ['added_lines', 'deleted_lines', 'changed_file_count', 'commit_count', 'net_added_lines', 'line_change_total']
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key))
const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const commit = value => typeof value === 'string' && /^[a-f0-9]{40}$/.test(value)
const count = value => Number.isSafeInteger(value) && value >= 0

export async function validateReportGitMetrics(value, projectId, report) {
  const reject = () => { throw new Error('REPORT_GIT_METRICS_INVALID') }
  if (!exact(value, ['schema_version', 'project_id', ...BINDINGS, 'source_git_facts', 'metrics', 'metrics_hash']) ||
      value.schema_version !== 'approved_report_git_metrics_v1' || !Number.isSafeInteger(value.project_id) || value.project_id <= 0 ||
      String(value.project_id) !== String(projectId) || !BINDINGS.every(key => value[key] === report[key])) reject()
  const source = value.source_git_facts, metrics = value.metrics
  if (!exact(source, SOURCE_KEYS) || !exact(metrics, METRIC_KEYS) ||
      typeof source.branch !== 'string' || !source.branch || source.branch.trim() !== source.branch || /[\x00-\x1f\x7f]/.test(source.branch) ||
      source.branch !== report.git_branch || source.from_commit !== report.git_from_commit || source.to_commit !== report.git_to_commit ||
      !commit(source.from_commit) || !commit(source.to_commit) ||
      !Array.isArray(source.commits) || !source.commits.every(commit) || new Set(source.commits).size !== source.commits.length ||
      !['commit_count', 'changed_file_count', 'added_lines', 'deleted_lines', 'diff_bytes'].every(key => count(source[key])) ||
      source.commits.length !== source.commit_count ||
      !['added_lines', 'deleted_lines', 'changed_file_count', 'commit_count'].every(key => count(metrics[key]) && metrics[key] === source[key]) ||
      (source.changed_file_count === 0 && (source.added_lines !== 0 || source.deleted_lines !== 0)) ||
      !Number.isSafeInteger(metrics.net_added_lines) || !count(metrics.line_change_total) ||
      metrics.net_added_lines !== metrics.added_lines - metrics.deleted_lines ||
      metrics.line_change_total !== metrics.added_lines + metrics.deleted_lines) reject()
  const { metrics_hash, ...unsigned } = value
  if (!hash(value.git_facts_hash) || await narrativeDigest(source) !== value.git_facts_hash ||
      !hash(metrics_hash) || await narrativeDigest(unsigned) !== metrics_hash) reject()
  return value
}

export async function getApprovedReportGitMetrics(projectId, reportVersionId, reportHash) {
  try {
    const headers = await localReadHeaders()
    const params = new URLSearchParams({ report_hash: reportHash })
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/anxin-board/reports/${encodeURIComponent(reportVersionId)}/git-metrics?${params}`, {
      method: 'GET', headers, cache: 'no-store', redirect: 'error'
    })
    const body = await response.json()
    handleLocalSessionFailure(response.status, body, headers)
    return { status: response.status, ok: response.ok, body }
  } catch {
    return { status: 0, ok: false, body: {} }
  }
}

export function createReportGitMetricsLoader(fetchMetrics, publish) {
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
        const response = await fetchMetrics(projectId, report.report_version_id, report.anxin_board_report_hash)
        if (current !== generation) return
        if (!response.ok) throw new Error('REPORT_GIT_METRICS_READ_FAILED')
        const value = await validateReportGitMetrics(response.body, projectId, report)
        if (current === generation) publish({ state: 'loaded', value })
      } catch {
        if (current === generation) publish({ state: 'failed', value: null })
      }
    }
  }
}
