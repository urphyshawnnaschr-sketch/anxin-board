export const MAIL_PROVIDER_PRESETS = Object.freeze([
  Object.freeze({
    key: 'qq',
    label: 'QQ 邮箱',
    host: 'smtp.qq.com',
    port: 587,
    security: 'starttls',
    supported: true,
    credentialLabel: 'QQ 邮箱授权码',
    help: '使用 QQ 邮箱授权码，不要填写 QQ 登录密码。',
    helpUrl: 'https://help.mail.qq.com/detail/106/985',
    helpLinkLabel: '打开 QQ 邮箱授权码教程'
  }),
  Object.freeze({
    key: '163',
    label: '163 邮箱',
    host: 'smtp.163.com',
    port: 465,
    security: 'implicit_tls',
    supported: true,
    credentialLabel: '客户端授权码',
    help: '先在网易邮箱开启客户端协议，并使用客户端授权码。',
    helpUrl: 'https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2a5feb28b66796d3b',
    helpLinkLabel: '打开网易邮箱授权码教程'
  }),
  Object.freeze({
    key: '126',
    label: '126 邮箱',
    host: 'smtp.126.com',
    port: 465,
    security: 'implicit_tls',
    supported: true,
    credentialLabel: '客户端授权码',
    help: '先在网易邮箱开启客户端协议，并使用客户端授权码。',
    helpUrl: 'https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2a5feb28b66796d3b',
    helpLinkLabel: '打开网易邮箱授权码教程'
  }),
  Object.freeze({
    key: 'gmail',
    label: 'Gmail',
    host: 'smtp.gmail.com',
    port: 587,
    security: 'starttls',
    supported: true,
    credentialLabel: 'Google 应用专用密码',
    help: '当前版本使用应用专用密码；Google 账号需要先开启两步验证。',
    helpUrl: 'https://support.google.com/accounts/answer/185833?hl=zh-CN',
    helpLinkLabel: '打开 Google 应用专用密码教程'
  }),
  Object.freeze({
    key: 'icloud',
    label: 'iCloud Mail',
    host: 'smtp.mail.me.com',
    port: 587,
    security: 'starttls',
    supported: true,
    credentialLabel: 'Apple App 专用密码',
    help: '使用 Apple 账号生成的 App 专用密码，不要填写 Apple 账号主密码。',
    helpUrl: 'https://support.apple.com/zh-cn/102654',
    helpLinkLabel: '打开 Apple App 专用密码教程'
  }),
  Object.freeze({
    key: 'outlook',
    label: 'Outlook / Hotmail',
    host: 'smtp-mail.outlook.com',
    port: 587,
    security: 'starttls',
    supported: false,
    credentialLabel: 'OAuth2 / Modern Auth',
    help: 'Microsoft 当前要求 OAuth2 / Modern Auth；安心看板 V1 尚未接入该认证方式。',
    helpUrl: 'https://support.microsoft.com/zh-cn/outlook/pop-imap-and-smtp-settings-for-outlook-com',
    helpLinkLabel: '查看 Microsoft 官方说明'
  }),
  Object.freeze({
    key: 'custom',
    label: '其他 / 自定义 SMTP',
    host: '',
    port: 587,
    security: 'starttls',
    supported: true,
    credentialLabel: 'SMTP 密码 / 应用专用密码',
    help: '请按照邮箱服务商提供的 SMTP 参数填写。',
    helpUrl: '',
    helpLinkLabel: ''
  })
])

export function getMailProviderPreset(key) {
  return MAIL_PROVIDER_PRESETS.find((item) => item.key === key) || MAIL_PROVIDER_PRESETS.at(-1)
}

function canonicalTransportFacts(profile) {
  return {
    host: String(profile?.host || '').trim().toLowerCase(),
    port: Number(profile?.port),
    security: String(profile?.security || '').trim().toLowerCase()
  }
}

export function isMailTransportConnectionTestSupported(profile) {
  const facts = canonicalTransportFacts(profile)
  return !MAIL_PROVIDER_PRESETS.some((item) => (
    item.supported === false
    && item.host.toLowerCase() === facts.host
    && item.port === facts.port
    && item.security === facts.security
  ))
}

export function detectMailProvider(profile) {
  if (!profile) return 'qq'
  const facts = canonicalTransportFacts(profile)
  const match = MAIL_PROVIDER_PRESETS.find((item) => (
    item.key !== 'custom'
    && item.host.toLowerCase() === facts.host
    && item.port === facts.port
    && item.security === facts.security
  ))
  return match?.key || 'custom'
}
