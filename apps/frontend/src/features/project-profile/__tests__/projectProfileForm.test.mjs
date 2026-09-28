import assert from 'node:assert/strict'
import {
  createEmptyProfileForm,
  createEmptyProfileContent,
  profileContentToForm,
  profileFormToContent,
  canonicalizeProfileContent
} from '../projectProfileForm.js'

let passed = 0
let failed = 0

function check(name, fn) {
  try {
    fn()
    passed++
    console.log(`  PASS  ${name}`)
  } catch (err) {
    failed++
    console.error(`  FAIL  ${name}`)
    console.error(`        ${err.message}`)
  }
}

console.log('projectProfileForm boundary tests')
console.log('------')

// 1. content -> form -> content round trip preserves empty-derived shape
check('empty content round trips without throwing', () => {
  const form = profileContentToForm(createEmptyProfileContent())
  const content = profileFormToContent(form)
  assert.deepEqual(content, createEmptyProfileContent())
})

// 2. null content -> empty form
check('null content maps to empty form', () => {
  const form = profileContentToForm(null)
  assert.equal(form.modules.length, 0)
  assert.equal(form.domain_glossary.length, 0)
  assert.equal(form.project_summary, '')
})

// 3. [null] module entries are dropped (no throw on element access)
check('[null] module entries are dropped', () => {
  const content = { modules: [null, { client_id: 'x', name: 'A' }] }
  const form = profileContentToForm(content)
  assert.equal(form.modules.length, 1)
  assert.equal(form.modules[0].name, 'A')
})

// 4. non-object module members (e.g. strings) are dropped
check('non-object module members are dropped', () => {
  const content = { modules: ['garbage', 42, { client_id: 'y' }] }
  const form = profileContentToForm(content)
  assert.equal(form.modules.length, 1)
})

// 5. [null] path entries within a module are dropped
check('[null] path entries are dropped', () => {
  const content = { modules: [{ client_id: 'x', paths: [null, { pattern: 'a' }] }] }
  const form = profileContentToForm(content)
  assert.equal(form.modules[0].paths.length, 1)
  assert.equal(form.modules[0].paths[0].pattern, 'a')
})

// 6. [null] domain_glossary entries are dropped
check('[null] domain_glossary entries are dropped', () => {
  const content = { domain_glossary: [null, { term: 't1' }] }
  const form = profileContentToForm(content)
  assert.equal(form.domain_glossary.length, 1)
  assert.equal(form.domain_glossary[0].term, 't1')
})

// 7. null form to content maps to empty content (guard)
check('null form maps to empty content', () => {
  const content = profileFormToContent(null)
  assert.deepEqual(content, createEmptyProfileContent())
})

// 8. [null] modules in form are dropped on to-content path
check('[null] form modules are dropped on to-content', () => {
  const form = { modules: [null, { client_id: 'z', name: 'B' }] }
  const content = profileFormToContent(form)
  assert.equal(content.modules.length, 1)
  assert.equal(content.modules[0].name, 'B')
})

// 9. non-object modules in form are dropped on to-content path (no throw on m.client_id)
check('non-object form modules are dropped on to-content', () => {
  const form = { modules: [123, 'x', { client_id: 'k' }] }
  const content = profileFormToContent(form)
  assert.equal(content.modules.length, 1)
  assert.deepEqual(content.modules[0], {
    client_id: 'k',
    name: '',
    description: '',
    prd_refs: [],
    requirements: [],
    paths: [],
    exclusions: []
  })
})

// 10. canonicalize is stable and schema version preserved
check('canonicalize is deterministic and schema_version preserved', () => {
  const content = createEmptyProfileContent()
  const a = canonicalizeProfileContent(content)
  const b = canonicalizeProfileContent(JSON.parse(JSON.stringify(content)))
  assert.equal(a, b)
  const form = profileContentToForm(content)
  assert.equal(form.schema_version, content.schema_version)
})

console.log('------')
console.log(`RESULT: ${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
