# 微信报告图片 Implementation Plan

**Goal:** 在正式报告最后发送环节配置并通过客户现有微信 ClawBot 接收直接可读的分页截图。
**Architecture:** 已确认报告 HTML → 本地隔离浏览器分页截图 → 冻结图片预览 → 幂等逐页发送 → 持久记录；微信与邮件各自保留结果。
**Tech Stack:** Python/FastAPI/SQLite、Vue、Playwright/Chromium、OpenClaw HTTP tools API。
**Spec:** `docs/wechat-report-images-design.md`

## Global Constraints

仅合成数据验证，不读取生产数据库/凭据、不调用模型、不重新发送邮件；用户未提供客户网关，真实微信收件不可宣称通过。公开仓库不得包含真实报告、日志、数据库、令牌、截图样本。不提交推送、不覆盖已交付安装包，直到本次功能及验收结果可供用户审阅。

## Task 1: 报告图片渲染与运行包依赖

Files: `apps/backend/app/report_screenshot.py`, `apps/backend/requirements.txt`, `installer/windows/build-runtime.ps1`, `tests/backend/test_report_screenshot.py`.

- [x] 先编写并运行失败测试：合成中文、内嵌图片、长模块表分页、超长段、外部网络/脚本禁止、图片字节上限、明确缺失运行时错误。
- [x] 实现 spec 中 ReportImage/render_report_images；用同一份 HTML 页面布局渲染，不生成或改写业务文字。
- [x] 固定 Python Playwright 版本及对应 Chromium；安装包收集 driver+浏览器并纳入原 manifest，客户无需 Node/Python 开发环境。
- [x] 验证真实截图，检查分页边界与中文清晰度；PS1 语法解析通过。

## Task 2: OpenClaw PNG transport

Files: `apps/backend/app/wechat_gateway.py`, `tests/backend/test_wechat_gateway.py`, `docs/wechat-openclaw-setup.md`.

- [x] 用官方固定源码确认 HTTP base64 图片协议、响应语义及目标绑定限制。
- [x] 先编写失败测试：真实本机 HTTP 端点接收PNG，拒绝重定向，超时返回unknown，不重试，HTTP拒绝与嵌套工具错误无回显，未知成功体不虚报accepted。
- [x] 实现 spec 中 GatewayResult/normalize_gateway_url/send_image；严格限制大小、目标、固定操作和响应输出。
- [x] 写明已有绑定+有效会话上下文、网关授权、目标标识取得方式、loopback或HTTPS部署与真实收件验收步骤。

## Task 3: 预览/配置/发送持久服务

Files: `apps/backend/app/approved_report_delivery.py`, `apps/backend/app/wechat_delivery_store.py`, `apps/backend/app/wechat_delivery_service.py`, `apps/backend/app/wechat_delivery_api.py`, `apps/backend/app/main.py`, `tests/backend/test_wechat_delivery.py`, `tests/backend/test_wechat_delivery_api.py`.

- [x] 测试正式报告绑定、草稿/过期文案拒绝、配置版本变化拒绝、受保护PNG读取、token不回显、重复/并发请求至多一次、部分失败停止、异常/重启unknown不重发。
- [x] 实现 spec 中 API，沿用正式 renderer 和凭据管理接口；新增schema由main lifespan初始化。
- [x] 配置/预览状态在请求开始及发送前核对；幂等恢复返回现有记录，不重新外发。
- [x] 仅已证明未投递的配置拒绝允许更高配置版本、新预览、新幂等键及再次人工确认后的独立尝试；保留全部历史及不可变序号，测试并发最多一次，旧预览/旧键仍返回旧记录。
- [x] 真实SQLite+内存secret+本机/替身transport验证状态转换与回归邮件入口。

## Task 4: 客户发送界面与集成验证

Files: `apps/frontend/src/api/wechatDelivery.js`, `apps/frontend/src/components/WechatSendPanel.vue`, `apps/frontend/src/views/AnxinBoardView.vue`, `apps/frontend/tests/` (follow existing browser layout), `README.md`.

- [x] UI测试先行：配置保存不回显令牌、未确认不能预览、预览图片/页数可见、确认发送、失败/unknown状态、项目切换和迟到响应防护、历史刷新不重发。
- [x] 按正式报告页现有 props 绑定新增微信区域；邮件按钮仍独立可用。明确每种渠道结果。
- [x] 测试前后端合成全流程、实际浏览器截图和中文长报告；完成受影响邮件回归与前端构建。
- [x] 独立代码审查；保留离线验证和真实微信未验收的区别。

## Progress

- [x] 用户批准截图发送；明确客户已有绑定、位置未知、先可配置。
- [x] 远端 main/开放PR/本地源码核对；独立clone及分支创建。
- [x] Tasks 1–4 completed and reviewed.

## Local evidence

Final scoped results: gateway 79 passed; screenshot 23 passed; delivery/API/restore 75 passed; existing mail/report/session regression 129 passed; installer lifecycle contracts 37 passed; frontend relevant browser 18 passed (plus prior unchanged board preview 18 passed); Node API 61 passed; production build passed; real formal HTML -> Chromium PNG -> guarded API -> loopback HTTP integration 1 passed. These are local synthetic checks, not actual customer WeChat receipt. Runtime packaging and installed acceptance are recorded separately with the built commit and artifact hashes.
