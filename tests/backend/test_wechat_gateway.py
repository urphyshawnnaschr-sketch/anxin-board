"""Exercise the real HTTP transport against a synthetic loopback Gateway."""

from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import struct
import threading
import time
import zlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


PNG = (b"\x89PNG\r\n\x1a\n"
       + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
       + png_chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
       + png_chunk(b"IEND", b""))
TOKEN = "synthetic-gateway-token-never-display"
TARGET = "synthetic_customer@im.wechat"
ACCOUNT = "synthetic-bot"


def gateway_module():
    assert importlib.util.find_spec("app.wechat_gateway") is not None, "PNG Gateway transport is not implemented"
    return importlib.import_module("app.wechat_gateway")


def success(*, message_id="openclaw-weixin-synthetic-001", delivery_status="sent"):
    details = {
        "channel": "openclaw-weixin", "to": TARGET, "via": "direct",
        "mediaUrl": "/synthetic/media/outbound/report.png",
        "result": {"channel": "openclaw-weixin", "messageId": message_id},
        "deliveryStatus": delivery_status,
    }
    return {"ok": True, "result": {"content": [{"type": "text", "text": json.dumps(details)}], "details": details}}


@contextmanager
def local_gateway(*, body=None, status=200, headers=None, delay=0, raw=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(data)})
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            payload = raw if raw is not None else json.dumps(body if body is not None else success()).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def config(url):
    return {"gateway_url": url, "account_id": ACCOUNT, "target": TARGET, "session_key": "main"}


def send(url, **changes):
    values = {"config": config(url), "token": TOKEN, "png": PNG,
              "filename": "anxin-report-7-page-01.png", "caption": "安心看板正式报告｜第 1/3 页"}
    values.update(changes)
    return gateway_module().send_image(**values)


def test_delivers_exact_png_using_official_tool_without_a_model_or_public_url(monkeypatch, caplog):
    monkeypatch.setenv("HTTP_PROXY", "http://invalid-proxy.invalid:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid-proxy.invalid:9")
    monkeypatch.setenv("NO_PROXY", "")
    with local_gateway() as (url, requests):
        result = send(url)
    assert result.state == "accepted"
    assert result.message_id == "openclaw-weixin-synthetic-001"
    assert len(requests) == 1
    request = requests[0]
    assert request["path"] == "/tools/invoke"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["body"] == {
        "tool": "message", "action": "send", "sessionKey": "main",
        "args": {"channel": "openclaw-weixin", "target": TARGET, "accountId": ACCOUNT,
                 "buffer": base64.b64encode(PNG).decode(), "contentType": "image/png",
                 "filename": "anxin-report-7-page-01.png", "message": "安心看板正式报告｜第 1/3 页"},
    }
    assert TOKEN not in repr(result) + caplog.text


@pytest.mark.parametrize(("value", "want"), [
    ("https://Gateway.Example:443/", "https://gateway.example"),
    ("https://gateway.example/openclaw/tools/invoke", "https://gateway.example/openclaw"),
    ("http://127.0.0.1:18789/", "http://127.0.0.1:18789"),
    ("http://[::1]:18789", "http://[::1]:18789"),
    ("http://localhost:18789", "http://localhost:18789"),
])
def test_normalizes_usable_gateway_base_urls(value, want):
    assert gateway_module().normalize_gateway_url(value) == want


@pytest.mark.parametrize("value", [
    "http://gateway.example", "http://192.168.1.20", "http://localhost.evil.example",
    "http://127.0.0.1.evil.example", "ftp://gateway.example", "https://user:secret@gateway.example",
    "https://gateway.example?token=secret", "https://gateway.example#secret",
    "https://gateway.example:0", "https://gateway.example:65536", "https://gateway.example:bad",
    "https://gateway.example/../private", "https://gateway.example/%2e%2e/private",
    "https://gateway.example\\@evil.example", "https://gateway.example\n", "", None,
])
def test_rejects_unsafe_gateway_urls_without_echo(value):
    with pytest.raises(ValueError) as error:
        gateway_module().normalize_gateway_url(value)
    assert str(error.value) == "WECHAT_GATEWAY_URL_INVALID"


@pytest.mark.parametrize(("status", "code"), [
    (400, "GATEWAY_REQUEST_REJECTED"), (401, "GATEWAY_AUTH_REJECTED"),
    (403, "GATEWAY_POLICY_REJECTED"), (404, "GATEWAY_TOOL_UNAVAILABLE"),
    (413, "GATEWAY_IMAGE_TOO_LARGE"), (429, "GATEWAY_RATE_LIMITED"),
])
def test_http_rejection_is_safe_and_never_retried(status, code, caplog):
    with local_gateway(status=status, body={"ok": False, "error": {"message": TOKEN}}) as (url, requests):
        result = send(url)
    assert (result.state, result.code, result.message_id) == ("rejected", code, None)
    assert len(requests) == 1
    assert TOKEN not in repr(result) + caplog.text


def test_upload_policy_rejection_has_a_safe_specific_code():
    body = {"ok": False, "error": {"type": "tool_call_blocked", "message": "File and image uploads are disabled by gateway.uploads.enabled"}}
    with local_gateway(status=403, body=body) as (url, requests):
        result = send(url)
    assert (result.state, result.code) == ("rejected", "GATEWAY_UPLOADS_DISABLED")
    assert len(requests) == 1


def test_redirect_is_not_followed_and_does_not_forward_credentials():
    with local_gateway() as (other, forwarded):
        with local_gateway(status=307, headers={"Location": other + "/steal"}) as (url, requests):
            result = send(url)
    assert (result.state, result.code) == ("rejected", "GATEWAY_REDIRECT_REJECTED")
    assert len(requests) == 1
    assert forwarded == []


@pytest.mark.parametrize("body", [
    {"ok": True}, {"ok": True, "result": {"details": {"ok": True}}},
    {"ok": True, "result": {"isError": True, "content": [{"type": "text", "text": TOKEN}]}},
    {"ok": False, "error": {"message": TOKEN}},
    {"ok": True, "result": {"details": {"deliveryStatus": {"unexpected": "object"}}}},
    {"ok": True, "result": {"details": {"status": ["unexpected"]}}},
    success(message_id=""), success(message_id=TOKEN), success(delivery_status="suppressed"),
    success(delivery_status="failed"), success(delivery_status="partial_failed"),
    success(delivery_status="queued"),
])
def test_unknown_or_nested_errors_cannot_be_claimed_as_accepted(body, caplog):
    with local_gateway(body=body) as (url, requests):
        result = send(url)
    assert result.state in {"rejected", "unknown"}
    assert result.message_id is None
    assert len(requests) == 1
    assert TOKEN not in repr(result) + caplog.text


def test_dry_run_and_wrong_recipient_are_not_delivery():
    for field, value in [("dryRun", True), ("to", "someone_else@im.wechat"), ("channel", "telegram")]:
        body = success()
        body["result"]["details"][field] = value
        with local_gateway(body=body) as (url, _):
            assert send(url).state != "accepted"


@pytest.mark.parametrize(("field", "value"), [
    ("deliveryStatus", "queued"), ("deliveryStatus", "failed"),
    ("deliveryStatus", {"unexpected": "object"}), ("status", "suppressed"),
    ("status", ["unexpected"]), ("dryRun", True), ("sentBeforeError", True),
    ("channel", "telegram"), ("channel", None), ("target", "other@im.wechat"),
    ("to", "other@im.wechat"), ("error", "synthetic failure"),
])
def test_contradictory_nested_receipt_is_never_accepted(field, value):
    body = success()
    body["result"]["details"]["result"][field] = value
    with local_gateway(body=body) as (url, requests):
        result = send(url)
    assert result.state in {"unknown", "rejected"}
    assert result.message_id is None
    assert len(requests) == 1


def test_missing_nested_receipt_channel_is_unverified():
    body = success()
    del body["result"]["details"]["result"]["channel"]
    with local_gateway(body=body) as (url, _):
        assert send(url).state == "unknown"


def test_malformed_text_json_and_duplicate_keys_are_unverified():
    cases = [
        json.dumps({"ok": True, "result": {"content": [{"type": "text", "text": "\ud800"}]}}).encode(),
        json.dumps(success()).replace('"deliveryStatus": "sent"', '"deliveryStatus": "failed", "deliveryStatus": "sent"').encode(),
    ]
    for raw in cases:
        with local_gateway(raw=raw) as (url, _):
            assert send(url).state == "unknown"


def test_text_json_result_from_older_gateway_is_parsed():
    body = success()
    del body["result"]["details"]
    with local_gateway(body=body) as (url, _):
        assert send(url).state == "accepted"


@pytest.mark.parametrize(("raw", "status"), [(b"not json", 200), (b"{}" * 40000, 200), (b"error", 503)], ids=["malformed", "oversized", "server-error"])
def test_unreadable_or_server_failure_is_unknown_without_retry(raw, status):
    with local_gateway(status=status, raw=raw) as (url, requests):
        result = send(url)
    assert result.state == "unknown"
    assert result.message_id is None
    assert len(requests) == 1


def test_timeout_after_accepting_request_is_unknown_and_not_retried(monkeypatch):
    monkeypatch.setattr(gateway_module(), "_REQUEST_TIMEOUT_SECONDS", 0.1)
    with local_gateway(delay=0.3) as (url, requests):
        result = send(url)
    assert (result.state, result.code) == ("unknown", "GATEWAY_TIMEOUT")
    assert len(requests) == 1


@pytest.mark.parametrize("changes", [
    {"png": b"not a PNG"}, {"png": b"\x89PNG\r\n\x1a\n"}, {"png": PNG + b"x" * 1_300_000},
    {"png": PNG[:-1]}, {"png": PNG + b"trailing bytes"}, {"png": PNG[:42] + b"x" + PNG[43:]},
    {"filename": "../report.png"}, {"filename": "report.html"},
    {"token": ""}, {"token": "bad\nheader"}, {"caption": "x" * 1001},
])
def test_invalid_input_is_rejected_before_any_network(changes):
    with local_gateway() as (url, requests):
        result = send(url, **changes)
    assert (result.state, result.code) == ("rejected", "GATEWAY_INPUT_INVALID")
    assert requests == []


@pytest.mark.parametrize("changes", [
    {"target": "display name"}, {"target": "someone@other.example"}, {"account_id": ""},
    {"session_key": "bad\nkey"}, {"gateway_url": "http://gateway.example"},
])
def test_bad_routing_cannot_reach_gateway(changes):
    with local_gateway() as (url, requests):
        values = config(url) | changes
        result = send(url, config=values)
    assert result.state == "rejected"
    assert requests == []
