import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
const source = fs.readFileSync(new URL('../../views/AnxinBoardView.vue', import.meta.url), 'utf8')
test('no report never enables the customer report shell', () => {
  const expression = source.match(/const boardVisible = computed\(\(\) => (.*)\)/)[1]
  for (const status of ['no-report', 'failed', 'loading']) assert.equal(Boolean(vm.runInNewContext(expression, { state: { value: status }, report: { value: null } })), false)
  assert.match(source, /@click="goWorkspace">前往项目工作台/)
})
test('customer template excludes implementation diagnostics and edition branding', () => {
  const template = source.slice(source.indexOf('<template>'), source.lastIndexOf('</template>'))
  for (const word of ['简洁书法版', 'ApprovalSnapshot', 'EvidenceSnapshot', 'V1/V2', '模型厂商', 'Token', 'pageScopeNote', 'approvalBasis']) assert.equal(template.includes(word), false, word)
  assert.match(template, /anxin-board-calligraphy.png/)
})

function summary(item) {
  const block = source.match(/function clientModuleSummary\(item\) \{[\s\S]*?\n\}/)?.[0]
  assert.ok(block)
  return vm.runInNewContext(`${block}; clientModuleSummary(item)`, {item})
}

test('legacy stage-only completion templates do not masquerade as specific facts', () => {
  for (const text of ['当前约定范围已完成。', '这项工作已经完成当前约定范围内的开发和检查，可以作为已完成内容展示。']) {
    const item = { stage: '已完成', summary: text }
    assert.equal(summary(item), null)
    assert.equal(item.summary, text)
    assert.equal(summary({ ...item, stage: '开发中' }), null)
  }
})

test('reviewed concrete facts and new ledger development summary stay unchanged', () => {
  for (const text of ['设备离线时保留最后上报时间，列表已显示状态未知。', '这项功能已完成当前约定范围内的开发；不代表已通过运行测试或验收。', '当前约定范围已完成。另有设备状态说明。']) {
    assert.equal(summary({ stage: '已完成', summary: text }), text)
  }
})
