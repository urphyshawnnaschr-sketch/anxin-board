# 用现有微信 ClawBot 接收报告图片

安心看板把已正式确认的报告生成为分页 PNG，经客户现有 OpenClaw 发送到已绑定的微信单聊。客户可以在微信里直接查看图片，无需打开 HTML 或公开报告网址。这个发送步骤不调用大模型。

## 接入前需要的信息

请由客户的 OpenClaw 管理员提供以下配置；在安心看板设置页填写访问令牌，不在聊天、工单或代码中粘贴它。

| 配置 | 填写内容 |
| --- | --- |
| 网关地址 | 本机示例 `http://127.0.0.1:18789`；远程填写可达的 HTTPS 网关基址。支持反向代理路径前缀。 |
| 绑定账号 | OpenClaw 微信渠道的 `accountId`，用于确定由哪个已绑定机器人发出。 |
| 微信接收标识 | 客户对应的 `xxx@im.wechat` 标识，不能填写微信昵称、微信号或手机号。 |
| 接收人名称 | 便于发送前人工确认的名称；不参与路由。 |
| 会话标识 | 默认 `main`；客户对工具权限按会话管理时，由管理员提供适用的 `sessionKey`。 |
| 访问令牌 | OpenClaw Gateway 的访问 token；使用 password 模式时填写网关密码。不是微信插件的 bot token。 |

账号和接收标识应由管理员从已绑定账号和对应微信会话的路由元数据取得。客户先在微信向自己的 ClawBot 发一条消息，使插件取得这名接收人的会话上下文。安心看板不读取或保存插件的 `context_token`；插件按 `accountId + 接收标识` 使用其已有缓存。

当前接口依据 OpenClaw `2026.9.6`（提交 `4735f663`）和腾讯微信插件 `2.4.9`（提交 `24de5c9e`）源码核对，客户实际版本尚未确认。仅支持已有微信单聊绑定，不声称群聊可用，也不保证失效登录或过期会话仍能主动发送。会话过期时需由客户在 OpenClaw 中恢复绑定或重新与机器人交互。

## 网关条件

- 访问令牌在此接口上具有网关操作员权限。网关应保留在本机、私有网络或有身份保护的入口；安心看板不会自动修改客户网关权限。
- 远程地址必须使用 HTTPS；HTTP 仅接受回环地址。地址中不能带用户名、密码、查询参数或片段。TLS 证书必须可验证。
- 此连接不继承电脑的 HTTP 代理设置、不跟随 HTTP 重定向。若服务有跳转，填写其最终 HTTPS 网关地址。
- 网关必须加载 `openclaw-weixin` 渠道，并允许所选会话使用 `message` 的 `send` 动作。工具策略拒绝时应由管理员检查当前权限，不需要放开命令执行或整个工具目录。
- `gateway.uploads.enabled` 不能为 `false`，否则网关会拒绝 base64 图片上传。该开关在所核对版本中默认为允许。
- 每页 PNG 限制为 1,300,000 字节，编码后可放入网关默认的 2 MiB HTTP 请求上限。若客户代理配置了更小的请求体上限，也需相应调整。

## 发送协议

安心看板后端只调用 `POST <网关基址>/tools/invoke`。鉴权通过 `Authorization: Bearer <网关访问令牌>`；令牌不会进入 URL。以下仅为接口示意，不含可执行的真实接收地址或图片：

```json
{
  "tool": "message",
  "action": "send",
  "sessionKey": "main",
  "args": {
    "channel": "openclaw-weixin",
    "target": "synthetic_customer@im.wechat",
    "accountId": "synthetic-bot",
    "buffer": "<PNG 原始字节的 base64>",
    "contentType": "image/png",
    "filename": "anxin-report-7-page-01.png",
    "message": "安心看板正式报告｜第 1/3 页"
  }
}
```

OpenClaw 将 `buffer` 暂存在网关的受管媒体目录，再交给微信插件上传。腾讯插件按图片类型发送原生微信 IMAGE 消息。这里不提交公网图片 URL，也不使用 HTML 附件。不能同时传入 `media`、`path` 或 `filePath`，因为这些字段可能覆盖 `buffer`。

如果其他集成工具需要模拟调用，必须把 `dryRun: true` 放入 `args`。官方文档明确说明顶层 `dryRun` 被忽略。模拟调用不能证明微信已登录、图片能上传或客户实际收件；本功能没有把它作为收件验收。

## 结果与实际验收

HTTP 200 仅代表工具调用获得响应。安心看板还检查嵌套的渠道、接收人、`deliveryStatus: sent` 和非空消息标识；回执中的排队、部分失败、取消、模拟发送或路由冲突均不能显示为已提交。未知格式、重复 JSON 字段或非预期字段类型会保留为结果未知，不回显网关原始响应。

| 看板结果 | 含义与处理 |
| --- | --- |
| 已提交微信 | 网关返回对应渠道和接收人的发送结果；仍不代表手机已收件或客户已阅读。 |
| 发送失败 | 网关明确拒绝，例如鉴权、工具权限、上传开关或图片大小限制。 |
| 部分发送 | 前面的分页已提交，后续页未全部成功；核对缺失页。 |
| 结果未知 | 超时、连接中断、服务器错误或响应无法确认；消息可能已发出，应先检查微信。 |

安心看板对每页只发起一次 HTTP 请求；超时或失败不会自动重试。重复点击、刷新页面不应造成重复外发。网关返回排队或部分失败时，后续投递仍可能发生，因此遇到结果未知先核对客户微信，不要靠连续重发判断是否成功。

如果记录明确提示“本次未投递，修正配置后可重试”，请先核对令牌、消息工具权限或图片上传设置并保存配置，再生成新预览、人工确认。此入口仅适用于投递前明确拒绝的配置错误，或配置变动导致全部图片尚未提交的情况；已提交、部分提交或结果未知的报告不适用。旧预览仍保留原记录，不能与另一次尝试的请求标识混用。

实际验收需使用客户配置后的真实微信：

1. 查看正式报告的全部图片预览，核对接收人、报告版本和页数。
2. 明确确认发送；核对看板逐页结果。
3. 在客户微信中确认每页图片都可直接查看，顺序、中文内容、书法图和模型来源信息完整。
4. 核对刷新历史记录不会再次发送，并记录实际收件结论。

客户网关地址、实际版本和凭据尚未提供时，只能完成本机模拟网关验收。真实微信收件必须保留为未验证。

## 官方依据

- [Tools invoke HTTP API](https://docs.openclaw.ai/gateway/tools-invoke-http-api)：直接工具调用、鉴权、请求上限和顶层 `dryRun` 语义。
- [OpenClaw message 参数](https://github.com/openclaw/openclaw/blob/4735f663f61425b98fed9ae54550699a323e7db5/src/agents/tools/message-tool-schema.ts)：`buffer`、`contentType`、`filename`。
- [base64 媒体处理](https://github.com/openclaw/openclaw/blob/4735f663f61425b98fed9ae54550699a323e7db5/src/infra/outbound/message-action-params.ts)：上传字节落为网关媒体路径。
- [OpenClaw 发送返回值](https://github.com/openclaw/openclaw/blob/4735f663f61425b98fed9ae54550699a323e7db5/src/infra/outbound/message.ts)：状态、消息标识、部分失败。
- [上传策略](https://github.com/openclaw/openclaw/blob/4735f663f61425b98fed9ae54550699a323e7db5/src/gateway/upload-policy.ts)：`gateway.uploads.enabled`。
- [腾讯微信渠道适配](https://github.com/Tencent/openclaw-weixin/blob/24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c/src/channel.ts)：账号路由、会话上下文和媒体发送。
- [腾讯图片发送](https://github.com/Tencent/openclaw-weixin/blob/24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c/src/messaging/send-media.ts)：按图片类型发送 IMAGE。
- [腾讯消息回执](https://github.com/Tencent/openclaw-weixin/blob/24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c/src/messaging/send.ts)：返回发送标识，没有手机阅读回执。
