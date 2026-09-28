import assert from 'node:assert/strict'
import test from 'node:test'

const {
  MAIL_PROVIDER_PRESETS,
  detectMailProvider,
  getMailProviderPreset,
  isMailTransportConnectionTestSupported
} = await import('../mailProviderPresets.js')

test('common supported presets carry exact transport facts only', () => {
  const qq = getMailProviderPreset('qq')
  assert.deepEqual(
    { host: qq.host, port: qq.port, security: qq.security, supported: qq.supported },
    { host: 'smtp.qq.com', port: 587, security: 'starttls', supported: true }
  )

  const gmail = getMailProviderPreset('gmail')
  assert.equal(gmail.host, 'smtp.gmail.com')
  assert.equal(gmail.port, 587)
  assert.equal(gmail.security, 'starttls')
  assert.equal(gmail.supported, true)

  const icloud = getMailProviderPreset('icloud')
  assert.equal(icloud.host, 'smtp.mail.me.com')
  assert.equal(icloud.port, 587)
  assert.equal(icloud.security, 'starttls')
})

test('common provider help links are presentation-only https documentation links', () => {
  const allowedHosts = new Set([
    'help.mail.qq.com',
    'help.mail.163.com',
    'support.google.com',
    'support.apple.com',
    'support.microsoft.com'
  ])

  for (const preset of MAIL_PROVIDER_PRESETS.filter((item) => item.key !== 'custom')) {
    assert.ok(preset.helpUrl, preset.key)
    assert.ok(preset.helpLinkLabel, preset.key)
    const url = new URL(preset.helpUrl)
    assert.equal(url.protocol, 'https:', preset.key)
    assert.equal(allowedHosts.has(url.hostname), true, `${preset.key}:${url.hostname}`)
  }

  const custom = getMailProviderPreset('custom')
  assert.equal(custom.helpUrl, '')
  assert.equal(custom.helpLinkLabel, '')
  assert.equal(MAIL_PROVIDER_PRESETS.some((item) => 'password' in item || 'secret' in item || 'token' in item), false)
})

test('outlook is visible but fail-closed because current adapter has no OAuth2', () => {
  const outlook = getMailProviderPreset('outlook')
  assert.equal(outlook.host, 'smtp-mail.outlook.com')
  assert.equal(outlook.port, 587)
  assert.equal(outlook.security, 'starttls')
  assert.equal(outlook.supported, false)
  assert.match(outlook.help, /OAuth2/)
})

test('saved exact transport facts detect preset without storing provider authority', () => {
  assert.equal(detectMailProvider({ host: 'smtp.qq.com', port: 587, security: 'starttls' }), 'qq')
  assert.equal(detectMailProvider({ host: 'smtp.126.com', port: 465, security: 'implicit_tls' }), '126')
  assert.equal(detectMailProvider({ host: 'smtp.internal.example', port: 2525, security: 'starttls' }), 'custom')
  assert.equal(MAIL_PROVIDER_PRESETS.some((item) => 'password' in item), false)
})

test('connection-test UX capability follows transport facts, not a presentation selector', () => {
  assert.equal(isMailTransportConnectionTestSupported({
    providerKey: 'custom',
    host: 'smtp-mail.outlook.com',
    port: 587,
    security: 'starttls'
  }), false)
  assert.equal(isMailTransportConnectionTestSupported({
    providerKey: 'custom',
    host: ' SMTP-MAIL.OUTLOOK.COM ',
    port: '587',
    security: 'STARTTLS'
  }), false)
  assert.equal(isMailTransportConnectionTestSupported({
    providerKey: 'outlook',
    host: 'smtp.internal.example',
    port: 2525,
    security: 'starttls'
  }), true)
})

test('legitimate custom and existing supported preset tuples remain test-capable in UX', () => {
  assert.equal(isMailTransportConnectionTestSupported({
    host: 'smtp.internal.example',
    port: 2525,
    security: 'starttls'
  }), true)

  for (const preset of MAIL_PROVIDER_PRESETS.filter((item) => item.supported === true && item.key !== 'custom')) {
    assert.equal(isMailTransportConnectionTestSupported(preset), true, preset.key)
  }
})
