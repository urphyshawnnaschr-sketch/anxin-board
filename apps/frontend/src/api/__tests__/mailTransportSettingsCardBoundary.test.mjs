import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const sourceUrl = new URL('../../components/MailTransportSettingsCard.vue', import.meta.url)
const source = (await readFile(sourceUrl, 'utf8')).replace(/\r\n/g, '\n')

test('changing provider clears any unsaved credential before applying transport defaults', () => {
  const start = source.indexOf('function applyProviderPreset()')
  const end = source.indexOf('\n}\n\nfunction fillSenderFromUsername', start)
  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  const body = source.slice(start, end)

  assert.match(body, /form\.password\s*=\s*['"]{2}/)
  assert.match(body, /form\.host\s*=\s*preset\.host/)
  assert.ok(
    body.indexOf('form.password') < body.indexOf('form.host'),
    'credential must be cleared before provider transport defaults are applied'
  )
})

test('unsupported provider credential input is disabled and warns against entering a password', () => {
  assert.match(
    source,
    /:disabled="!providerSupported \|\| saveState === 'saving' \|\| testState === 'testing'"/
  )
  assert.match(source, /当前版本暂不支持；请勿输入密码/)
})

test('unsupported provider cannot save or run connection test', () => {
  const canSaveStart = source.indexOf('const canSave = computed')
  const canTestStart = source.indexOf('const canTest = computed')
  assert.notEqual(canSaveStart, -1)
  assert.notEqual(canTestStart, -1)

  assert.match(source.slice(canSaveStart, canTestStart), /providerSupported\.value/)
  assert.match(source.slice(canTestStart, source.indexOf('function applyProviderPreset', canTestStart)), /providerSupported\.value/)
})

test('connection test also fails closed from transport facts independent of selector support', () => {
  assert.match(source, /isMailTransportConnectionTestSupported/)
  assert.match(
    source,
    /const transportTestSupported = computed\(\(\) => isMailTransportConnectionTestSupported\(form\)\)/
  )

  const canTestStart = source.indexOf('const canTest = computed')
  const canTestEnd = source.indexOf('function applyProviderPreset', canTestStart)
  assert.notEqual(canTestStart, -1)
  assert.notEqual(canTestEnd, -1)
  assert.match(source.slice(canTestStart, canTestEnd), /transportTestSupported\.value/)
  assert.match(source, /当前已保存的 SMTP 参数属于本版本不支持的认证模式；测试连接保持关闭。/)
})

test('common mailbox setup is self-guided without turning help links into authority', () => {
  assert.match(source, /选一个常用邮箱，服务器、端口和加密方式会自动填写/)
  assert.match(source, /选邮箱/)
  assert.match(source, /填账号和授权码/)
  assert.match(source, /测试连接/)
  assert.match(source, /:href="selectedProvider\.helpUrl"/)
  assert.match(source, /target="_blank"/)
  assert.match(source, /rel="noopener noreferrer"/)
  assert.match(source, /providerHelpAvailable/)
})

test('sender address convenience fill only writes an empty sender field', () => {
  const start = source.indexOf('function fillSenderFromUsername()')
  const end = source.indexOf('\n}\n\nfunction applySettings', start)
  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  const body = source.slice(start, end)
  assert.match(body, /!form\.from_identity\.trim\(\)/)
  assert.match(body, /form\.username\.trim\(\)/)
  assert.match(body, /form\.from_identity\s*=\s*form\.username\.trim\(\)/)
})

test('connection-test copy promises verification only and explicitly denies sending mail', () => {
  assert.match(source, /测试连接只检查账号、授权码、SMTP、TLS 和登录是否正常，不会给任何人发邮件。/)
  assert.match(source, /测试成功 ≠ 已经发信/)
})
