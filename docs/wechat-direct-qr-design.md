# 扫码绑定微信并直接接收报告

2026-09-29 用户明确选择：安心看板直接管理微信连接，客户扫码即可；原有 OpenClaw 网关配置作为高级选项保留。此决定在已经完成并实际安装的网关发送版本上继续实现，不丢弃其报告、图片和发送记录。

## 客户流程

默认显示“绑定微信”。点击后由后端向腾讯官方登录接口取得短期二维码，在本机编码为 PNG。客户自行在微信确认授权；需要配对码时由客户填写。扫码成功后即结束扫码轮询，用户可查看已正式确认报告的全部图片，再确认发送，无需先发消息。有已保存的本人上下文时携带，否则按官方发送实现省略该可选字段；接口是否接受和手机是否收到分别核验。绑定本身不发送报告、不自动回复、不调用大模型。

原有 OpenClaw 表单位于折叠的高级入口。扫码绑定能否和另一台 OpenClaw 同时保持登录没有正式保证，因此开始绑定前明确说明可能影响原连接；需要保留原部署时使用高级入口。这里不承诺替用户迁移或修改外部 OpenClaw。

## 后端边界

- 沿用腾讯 `openclaw-weixin` 2.4.9 固定源码 `24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c` 的公开扫码、接收上下文和原生图片协议。
- 初始 API 为 `https://ilinkai.weixin.qq.com`。只允许核实过的腾讯 HTTPS API/CDN 主机，不使用环境代理，不跟随通用 HTTP 重定向；协议内换区状态也先校验主机。
- 二维码5分钟有效；流程按项目和配置版本绑定，旧流程、并发确认、取消、项目切换和重启不能覆盖新配置。
- 收件人固定为扫码确认响应中的 `ilink_user_id`。只接受该扫码人的单聊 USER 入站消息所带 `context_token`；不采用任意第一条消息，不保留聊天正文。
- 令牌、上下文、接收游标分别存入 Windows 凭据管理器，数据库只保留引用；二维码会话只在受限内存中存在。已有 secret store 的2048字节限制保持，错误不回显秘密。
- 登录轮询、上下文刷新仅由明确操作触发；程序启动不自动联网。页面轮询串行，在取消、卸载组件、项目切换和到期时停止。
- 图片上传按官方 AES-128-ECB/PKCS7 协议，使用固定版本 cryptography 库。CDN不接收机器人 Bearer token。每页只发送一条原生 IMAGE，报告已含页内说明，不另发 caption。
- 发送继续使用不可变预览、逐页持久化、幂等记录。模式切换不能绕开同一报告和接收目标的去重；结果未知不自动重试。网关受理和手机实收分开记录。
- 解绑只解除本机可用绑定及凭据，不删除发送历史，不声称已撤销微信服务器或其他软件的绑定。恢复旧备份不得抹掉微信外发历史或复活已停用的绑定。

## 前后端接口

全部位于 `/api/projects/{project_id}`，沿用本机会话保护，写请求校验来源。

| 方法与路径 | 请求 | 安全响应 |
| --- | --- | --- |
| GET `/wechat-binding` | 无 | `transport,binding_state,version_no,account_id,target,recipient_label,context_ready` |
| POST `/wechat-login/start` | `expected_version_no` | `flow_id,status,expires_at,qr_url` |
| POST `/wechat-login/poll` | `flow_id,verification_code?` | `flow_id,status,expires_at,binding` |
| GET `/wechat-login/{flow_id}/qr.png` | 无 | 受会话保护、不可缓存的 PNG |
| POST `/wechat-login/cancel` | `flow_id` | `cancelled,binding` |
| POST `/wechat-binding/refresh` | `expected_version_no` | 安全绑定对象 |
| POST `/wechat-binding/disconnect` | `expected_version_no,human_confirmed:true` | 安全绑定对象 |

`binding_state` 为 `unbound / awaiting_message / ready / disconnected`；登录状态为 `wait / scaned / need_verifycode / verify_code_blocked / expired / awaiting_message / ready`。异常状态使用固定安全错误。已有 `/wechat-settings` 保留字段，追加 `transport` 和 `binding_state`；有效扫码人和令牌引用对应的 `awaiting_message` 或 `ready` 绑定可为 `configured=true`。`context_ready` 仍如实表示是否已有本人上下文，不是首次发送前提。扫码完成后的 poll 只读绑定，只有显式 refresh 才拉取会话。

## 验收

先使用内存凭据和可控 HTTP 响应验证扫码状态、owner筛选、取消/过期/并发、发送加密字节、错误分类、历史去重和模式兼容，再用浏览器验证客户实际操作。重新构建和实际安装最终包，核对原数据及正式报告。真实微信扫码和手机收件由用户操作授权并确认，不能用合成测试代替。

依据：[登录协议](https://github.com/Tencent/openclaw-weixin/blob/24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c/docs/protocol.md)、[登录实现](https://github.com/Tencent/openclaw-weixin/blob/24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c/src/auth/login-qr.ts)、[图片上传](https://github.com/Tencent/openclaw-weixin/blob/24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c/src/cdn/upload.ts)、[消息发送](https://github.com/Tencent/openclaw-weixin/blob/24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c/src/messaging/send.ts)。
