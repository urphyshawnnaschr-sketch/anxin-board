import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const source = readFileSync(new URL('../../views/ReportReviewView.vue', import.meta.url), 'utf8')
const start = source.indexOf('function detailMessage(')
const end = source.indexOf('function localTimezone(', start)
const detailMessage = vm.runInNewContext(`(${source.slice(start, end).trim()})`)

test('report review retains output-contract identifiers and safe field locations', () => {
  const text = detailMessage({ detail: {
    message: '输出未通过校验。失败回执 #7。',
    code: 'MODEL_EXECUTION_RESULT_OUTPUT_INVALID',
    cause_code: 'AI_OUTPUT_CONTRACT_INVALID', stage: 'output_contract',
    validation_issues: [{ code: 'AI_ENUM_INVALID', path: '$.result.new_report.feature_progress[0].stage' }]
  } }, 'fallback')
  assert.match(text, /失败回执 #7/)
  assert.match(text, /失败步骤：校验 AI 输出/)
  assert.match(text, /MODEL_EXECUTION_RESULT_OUTPUT_INVALID \/ AI_OUTPUT_CONTRACT_INVALID/)
  assert.match(text, /AI_ENUM_INVALID @ \$\.result\.new_report\.feature_progress\[0\]\.stage/)
})

test('diagnostic display rejects arbitrary objects, raw values and malformed identifiers', () => {
  const text = detailMessage({ detail: {
    message: '安全提示', code: 'bad secret', cause_code: { secret: 'private' },
    credential_error_code: 'UNSAFE\nSECRET', stage: 'private secret',
    raw_response: 'private', headers: { Authorization: 'private' },
    validation_issues: [
      { code: 'AI_ENUM_INVALID', path: '$.result.x', message: 'private', value: 'private' },
      { code: 'AI_BAD secret', path: '$.result' },
      { code: 'AI_ENUM_INVALID', path: '$.result["private"]' }, null
    ]
  } }, 'fallback')
  assert.match(text, /AI_ENUM_INVALID @ \$\.result\.x/)
  assert.doesNotMatch(text, /private|secret|UNSAFE|SECRET|Authorization|\[object Object\]/)
})

test('diagnostics are bounded and malformed detail falls back without inventing a failure', () => {
  const text = detailMessage({ detail: {
    validation_issues: Array.from({ length: 20 }, (_, n) => ({ code: 'AI_SCHEMA_INVALID', path: `$.result.items[${n}]` }))
  } }, '安全后备提示')
  assert.equal((text.match(/AI_SCHEMA_INVALID @/g) || []).length, 6)
  assert.match(text, /^安全后备提示/)
  assert.equal(detailMessage({ detail: { message: {} } }, 'fallback'), 'fallback')
  assert.equal(detailMessage(null, 'fallback'), 'fallback')
})
