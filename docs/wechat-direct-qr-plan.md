# Direct WeChat QR Implementation Plan

> **For agentic workers:** Use subagent-driven-development for independent protocol, backend binding and frontend tasks. Root reviews integration and delivery evidence.

**Goal:** Make direct WeChat QR binding the default customer path while preserving advanced OpenClaw delivery.

**Architecture:** A narrowly scoped iLink client owns Tencent protocol transport. A session-guarded binding service stores opaque credential references and projects a ready configuration into the existing report-image delivery ledger. Vue handles the QR lifecycle and existing human-confirmed preview/send flow.

**Tech Stack:** Python 3.11, FastAPI, httpx 0.28.1, cryptography 50.0.1, qrcode 8.2, Vue 3, existing Playwright runtime.

**Spec:** `docs/wechat-direct-qr-design.md`

## Global Constraints

- No real model/SMTP calls or qualification experiments; preserve existing approved reports and ledger.
- Customer alone completes real QR authorization. Use synthetic HTTP and in-memory credentials during development.
- Keep immutable preview, per-page attempt accounting and unknown-result no-retry semantics.
- Do not place raw QR, bot token, context token, cursor or arbitrary provider error text in logs, SQLite, public APIs or commits.
- Existing gateway is a functioning advanced option; no migration of external customer services.

### Task 1: Verified iLink transport

Files: `apps/backend/app/wechat_ilink.py`, `tests/backend/test_wechat_ilink.py`.

Interfaces:

```python
class IlinkError(Exception):
    code: str

class IlinkClient:
    def __init__(self, *, transport=None): ...
    def start_login(self) -> dict: ... # qrcode, qrcode_content
    def poll_login(self, qrcode, *, base_url=DEFAULT_API_BASE, verification_code=None) -> dict: ...
    def get_updates(self, *, base_url, token, cursor='') -> dict: ... # cursor, limited message metadata
    def send_image(self, config, token, png, *, context_token, filename, caption): ... # GatewayResult
```

- [ ] Write mock-transport tests for QR wait/scan/confirmed/verification/expiry, trusted redirect hosts and response limits.
- [ ] Test encrypted upload against independent decryption and assert no Authorization header reaches CDN; check base64 of hex key text, exact owner and one image message.
- [ ] Implement strict parse/size/TLS/host checks and safe errors. Send-time transport uncertainty remains unknown.
- [ ] Run `python -m pytest tests/backend/test_wechat_ilink.py -q` and inspect exact HTTP payload evidence.

### Task 2: Binding authority and delivery compatibility

Files: new `wechat_binding_store.py`, `wechat_binding_service.py`, `wechat_binding_api.py`; modify `wechat_delivery_store.py`, `wechat_delivery_service.py`, `main.py`, `product_restore_guard.py`; add binding/API tests and extend existing delivery/restore tests.

- [ ] Test each spec endpoint with session denial, body limits, stale config, two concurrent login polls, canceled/expired flow, and scanner-only context selection.
- [ ] Test that unrelated sender and group messages cannot enable delivery; bound secrets remain outside database/JSON/errors.
- [ ] Add backwards-compatible transport projection, credential rollback and local disconnect; recovery must retain immutable records.
- [ ] Reuse `send_preview` for direct dispatch, including bound owner/context and canonical bot identity. Do not reset existing attempts.
- [ ] Run binding, existing WeChat delivery/API and restore tests using isolated DB and in-memory secret store.

Behavioral contract example:

```python
assert binding['binding_state'] == 'awaiting_message'
assert settings['configured'] is False
# A matching owner single-chat context is necessary before ready becomes true.
assert sender_id == confirmed_owner_id and message_type == 1 and not group_id
assert history_before_disconnect == history_after_disconnect
```

### Task 3: Customer QR flow

Files: `WechatSendPanel.vue`, optional focused `WechatBindingPanel.vue`, `wechatDelivery.js`, WeChat browser tests.

- [ ] Test unconfigured view starts with binding action and collapsed advanced OpenClaw form.
- [ ] Test real API-shaped mock QR response is fetched as a protected PNG blob and cleared on expiry/cancel/unmount/project switch.
- [ ] Implement serial polling, stale-generation guards, verification input erasure, awaiting-message guidance and explicit disconnect/refresh.
- [ ] Keep frozen preview SHA/dimension validation and human send confirmation; changing binding invalidates old preview.
- [ ] Run focused WeChat/mail E2E plus build; inspect default unbound, QR, awaiting-message and ready screenshots.

### Task 4: Integration, packaging and handoff

Files: backend requirements, Windows package configuration only if required, customer setup instructions and delivery evidence outside repository.

- [ ] Pin approved dependencies; install only in disposable build/development environment.
- [ ] Run one synthetic complete QR-to-owner-context-to-formal-PNG-to-send integration, proving repeated click causes no duplicate send.
- [ ] Review exact changed files for credentials/private artifacts and regression risks; commit/push Draft PR and read back exact remote HEAD/checks.
- [ ] Build final committed runtime, verify packaged QR generation/AES and image rendering, then run new Setup with data recovery backup.
- [ ] Verify installed hashes, original data/report, desktop session and customer entry. Keep real QR authorization/phone receipt separate from local test evidence.

Commit each verified scope on the existing `codex/wechat-report-images` branch; no merge, Ready or Release operation is authorized by this plan.
