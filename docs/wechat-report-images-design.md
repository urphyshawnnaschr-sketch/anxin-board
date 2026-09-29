# 微信正式报告图片发送

用户已确认：客户已有 OpenClaw 和微信 ClawBot 绑定，部署位置暂不清楚，先做可配置接入。正式报告要以图片直接显示在微信中，长报告分页。保留邮件能力与已有正式模板、确认记录、模型来源，禁止重新调用模型分析。

## 用户流程

1. 在报告发送区域选择微信，配置 OpenClaw 网关地址、绑定账号、微信接收标识、接收人名称和访问令牌。令牌仅保存在 Windows 凭据管理器，不回显。
2. 仅从当前正式确认报告生成图片预览；页面展示接收人、报告版本和全部分页截图。
3. 用户明确确认发送当前预览；后端重新核对报告、文案、配置版本，逐页发送同一份被冻结的 PNG。
4. 保存每页结果；网关接受不等于微信实收。部分发送、超时未知或应用中断时不自动重试。刷新、重复点击、重新生成同内容预览都不能再次外发已尝试的同一份内容。

唯一显式恢复例外：若尝试为 `failed`，所有页均为 `not_sent` 或 `rejected`，且每个拒绝均属于网关已证明未投递的配置错误（`GATEWAY_AUTH_REJECTED`、`GATEWAY_POLICY_REJECTED`、`GATEWAY_UPLOADS_DISABLED`、`GATEWAY_TOOL_UNAVAILABLE`、`GATEWAY_INPUT_INVALID`），用户保存更高版本配置、生成新预览并用新幂等键再次人工确认后，可建立下一次独立尝试。配置在首张提交前变动造成全部图片为 `not_sent` 时，也适用这个明确未提交的恢复入口。旧预览或旧幂等键保持原记录；若二者已经分别绑定不同尝试，拒绝混用。一般工具拒绝、已接受、部分发送、未知和发送中均不适用。每次尝试保留不可变配置版本和同文档目标递增序号，不删除或重写历史。

## 实现边界

- 以公开仓库 main `09dc2b1f7a0faf9db64934bcac9359a45698e6aa` 为基线，独立分支 `codex/wechat-report-images`；本次开工远端核对无开放 PR。
- 新增独立微信渠道，不改邮件发送权限、邮件记录或模型资格门。
- 后端渲染同一份正式 HTML；嵌入现有书法图片。通过随包分发的固定 Playwright/Chromium 截图，禁止网络及报告中的脚本执行，禁止任意客户端 HTML/URL 输入。
- 每页 PNG 最多 1,300,000 字节，最多 24 页。分页保证内容完整，不截断普通表格行/段落；超长块应按文字行处理或明确拒绝，不悄悄漏字。
- 网关使用官方 `/tools/invoke`、`message` 的 `send` 动作，固定 `openclaw-weixin` 渠道，发送 base64 PNG；不需要公开图片地址，不触发大模型。
- 仅允许 HTTPS 网关，回环地址可用 HTTP；拒绝 URL 用户信息、查询参数、片段、重定向，禁止 HTTP 客户端自动重试。配置变更须使旧预览失效。
- 本地 API 读写沿用会话保护；PNG 通过受保护请求读取，不泄露为公开静态 URL。只读取已存在正式报告；测试全部使用合成数据、内存凭据和本机模拟网关。
- 客户地址/凭据尚未提供，因此本次可完成可配置功能及本地验收；真实微信收件必须单独标为未验证，不能称整体微信交付完成。

## 组件接口

`app.report_screenshot`:

```python
@dataclass(frozen=True)
class ReportImage:
    png: bytes
    width: int
    height: int
def render_report_images(html: str) -> tuple[ReportImage, ...]: ...
class ReportScreenshotError(RuntimeError):
    code: str
```

`app.wechat_gateway`:

```python
@dataclass(frozen=True)
class GatewayResult:
    state: str  # accepted / rejected / unknown
    code: str   # safe fixed machine code, never response text
    message_id: str | None = None
def normalize_gateway_url(value: str) -> str: ...
def send_image(config: dict, token: str, png: bytes, *, filename: str, caption: str) -> GatewayResult: ...
# config contains gateway_url, account_id, target, session_key.
```

API (all paths under `/api/projects/{project_id}`):

- `GET /wechat-settings`: `{configured, version_no, gateway_url, account_id, target, recipient_label, session_key, token_configured}`; no secret refs/values.
- `POST /wechat-settings`: fields above, `expected_version_no`, `token` (optional, omitted retains token only for unchanged gateway); returns same safe representation.
- `POST /wechat-preview`: `{expected_report_version_id, expected_report_hash, expected_module_narrative_hash, expected_config_version_no}`. Returns `{preview_id, report_version_id, report_label, config_version_no, recipient_label, created_at, pages:[{index,width,height,sha256,url}]}`. Indexes are 1-based.
- `GET /wechat-preview/{preview_id}/images/{index}`: exact immutable PNG, `Cache-Control: no-store`.
- `POST /wechat-send`: `{preview_id, human_confirmed:true}` plus `Local-Idempotency-Key`. Returns `{attempt_id, state, report_version_id, recipient_label, created_at, pages:[{index,state,code}]}`. Attempt states: sending / accepted / partial / failed / unknown. Accepted means all pages accepted by Gateway, not proof of receipt.
- Attempt responses also include `retry_requires_config_change`: true only for the narrow confirmed-unsent failures above. A higher config version, fresh preview, fresh key and explicit confirmation are still required; this flag never starts a retry.
- `GET /wechat-history`: safe array of attempts (newest first, limit 20).

DB schema additions initialize idempotently and preserve existing tables. Preview/attempt content and target identities are immutable; page status persists before/after every external request. Claim attempt atomically, reserve the document+target identity across previews, and never hold a DB writer transaction during network or browser work. A stale `sending` page after process restart is shown as unknown, never resumed automatically.

The document+target identity binds project, exact standalone HTML, bound account and WeChat target; gateway URL, token, session key, recipient label and configuration version cannot bypass it. Attempts are uniquely reserved by `(document_target_hash, sequence_no)` and by preview; the next sequence is allocated under the same SQLite writer transaction after the narrow retry checks.
