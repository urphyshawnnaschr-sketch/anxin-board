import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import vm from 'node:vm'
const source = fs.readFileSync(new URL('../../views/ProjectProfilePanel.vue', import.meta.url), 'utf8')
function read(diagnostic) {
  const context = { computed: fn => fn(), atlasTask: { value: { status: 'failed_after_send', error_code: 'PROFILE_GENERATION_STRICT_WIRE_INVALID', failure_diagnostic: diagnostic } } }
  vm.createContext(context)
  vm.runInContext(source.slice(source.indexOf('const atlasFailureCounts ='), source.indexOf('let atlasAdmissionUnknown =')) + '\nresult = { counts: atlasFailureCounts, reason: atlasFailureReason }', context)
  return context.result
}
const valid = { wire_reason: 'ROWS_INVALID', wire_rule: 'EXACT_KEYS', expected_row_count: 3, tool_call_count: 2, tool_call_ordinal: 1, missing_row_count: 3, extra_row_count: 1 }
test('exact row mismatch explains known counts without inferring keys', () => {
  const result = read(valid)
  assert.match(result.counts, /第 2 份结果含 1 个批外记录键，缺少 3 个本批记录键/)
  assert.match(result.reason, /AI 返回的内容未能对应本批分析范围/)
})
test('unsafe and inconsistent row diagnostics retain safe fallback', () => {
  for (const patch of [{ missing_row_count: '3' }, { extra_row_count: -1 }, { tool_call_ordinal: 2 }, { tool_call_ordinal: true }, { expected_row_count: 2 }, { tool_call_count: 17 }, { wire_rule: ' EXACT_KEYS' }, { missing_row_count: 2147483648 }]) {
    const result = read({ ...valid, ...patch })
    assert.doesNotMatch(result.counts, /批外记录键/)
    if ('tool_call_ordinal' in patch || 'tool_call_count' in patch) assert.doesNotMatch(result.reason, /第 .* 份结果/)
  }
  assert.match(read({ wire_reason: 'ROWS_INVALID' }).reason, /返回的记录不完整，或包含本批之外的记录/)
})

test('citation failures explain the actual saved branch without exposing model text', () => {
  for (const [rule, expected] of [
    ['UNKNOWN_WITH_CITATIONS', /判断为无法确认，但同时填写了实现证据引用/],
    ['POSITIVE_WITHOUT_CITATIONS', /判断为已实现或部分实现，但没有提供代码证据/],
    ['POSITIVE_FIRST_SLOT_EMPTY', /提供了代码引用，但引用排列不符合当前返回格式/]
  ]) {
    const result = read({ wire_reason: 'STATUS_CITATION_INVALID', wire_rule: rule, raw_body: 'private-marker' })
    assert.match(result.reason, expected)
    assert.doesNotMatch(result.reason, /private-marker/)
  }
  for (const rule of ['STATUS_CITATIONS', 'private-marker', '__proto__']) {
    const result = read({ wire_reason: 'STATUS_CITATION_INVALID', wire_rule: rule })
    assert.match(result.reason, /实现状态与代码证据不一致/)
    assert.doesNotMatch(result.reason, /private-marker|__proto__/)
  }
})
