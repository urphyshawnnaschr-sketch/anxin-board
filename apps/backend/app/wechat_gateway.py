"""Send one frozen PNG through a customer's existing OpenClaw Gateway.

An accepted result proves provider submission only. This transport deliberately
does not retry: a timeout or lost response may follow a successful delivery.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import ipaddress
import json
import re
import struct
from urllib.parse import urlsplit
import zlib

import httpx


_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_IMAGE_BYTES = 1_300_000
_MAX_RESPONSE_BYTES = 65_536
_CHANNEL = "openclaw-weixin"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_UPLOADS_DISABLED = "File and image uploads are disabled by gateway.uploads.enabled"
_TARGET = re.compile(r"[A-Za-z0-9_.-]{1,180}@im\.wechat\Z")
_ROUTING = re.compile(r"[A-Za-z0-9_:@.-]{1,200}\Z")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.png\Z")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_:@.-]{1,256}\Z")


@dataclass(frozen=True)
class GatewayResult:
    state: str
    code: str
    message_id: str | None = None


def normalize_gateway_url(value: str) -> str:
    """Return a base URL; credentials and transport hints never belong in it."""
    try:
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError
        if parsed.username is not None or parsed.password is not None or "?" in value or "#" in value:
            raise ValueError
        host = parsed.hostname
        if not host or "%" in host:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            host = host.encode("idna").decode("ascii").lower()
            if len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError
        if parsed.scheme == "http" and host != "localhost" and not (address and address.is_loopback):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parsed.netloc.endswith(":"):
            raise ValueError
        path = parsed.path.rstrip("/")
        # A simple optional reverse-proxy prefix is enough; encoded separators
        # and dot segments could make an operator review a different endpoint.
        if path and (
            not re.fullmatch(r"/[A-Za-z0-9_./~-]*", path)
            or any(part in {".", "..", ""} for part in path[1:].split("/"))
        ):
            raise ValueError
        if path.endswith("/tools/invoke"):
            path = path[:-len("/tools/invoke")]
        if address:
            host = address.compressed
            if address.version == 6:
                host = f"[{host}]"
        port_text = f":{port}" if port is not None and port != (443 if parsed.scheme == "https" else 80) else ""
        return f"{parsed.scheme}://{host}{port_text}{path}"
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("WECHAT_GATEWAY_URL_INVALID") from None


def _valid_text(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _valid_png(png: object) -> bool:
    """Check a bounded PNG container without decompressing untrusted pixels."""
    if not isinstance(png, bytes) or not 57 <= len(png) <= _MAX_IMAGE_BYTES or not png.startswith(_PNG_SIGNATURE):
        return False
    offset = len(_PNG_SIGNATURE)
    has_pixels = False
    while offset + 12 <= len(png):
        size = int.from_bytes(png[offset:offset + 4], "big")
        end = offset + 12 + size
        if end > len(png):
            return False
        kind = png[offset + 4:offset + 8]
        data = png[offset + 8:end - 4]
        if zlib.crc32(kind + data) != int.from_bytes(png[end - 4:end], "big"):
            return False
        if offset == len(_PNG_SIGNATURE):
            if kind != b"IHDR" or size != 13:
                return False
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if not (0 < width < 2**31 and 0 < height < 2**31 and depth in depths.get(color, ())
                    and compression == filtering == 0 and interlace in (0, 1)):
                return False
        elif kind == b"IHDR":
            return False
        if kind == b"IDAT" and size:
            has_pixels = True
        if kind == b"IEND":
            return size == 0 and has_pixels and end == len(png)
        offset = end
    return False


def _request_payload(config: dict, token: str, png: bytes, filename: str, caption: str) -> tuple[str, dict]:
    if not isinstance(config, dict):
        raise ValueError
    url = normalize_gateway_url(config.get("gateway_url"))
    session_key = config.get("session_key", "main")
    if not (
        _valid_text(config.get("target"), _TARGET)
        and _valid_text(config.get("account_id"), _ROUTING)
        and _valid_text(session_key, _ROUTING)
        and _valid_text(filename, _FILENAME)
        and ".." not in filename
        and isinstance(token, str) and 1 <= len(token) <= 8192
        and all(33 <= ord(char) <= 126 for char in token)
        and _valid_png(png)
        and isinstance(caption, str) and len(caption) <= 1000
        and all(ord(char) >= 32 or char == "\n" for char in caption)
    ):
        raise ValueError
    return url, {
        "tool": "message", "action": "send", "sessionKey": session_key,
        "args": {
            "channel": _CHANNEL, "target": config["target"], "accountId": config["account_id"],
            "buffer": base64.b64encode(png).decode("ascii"), "contentType": "image/png",
            "filename": filename, "message": caption,
        },
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _load_json(raw: bytes | str) -> object:
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        return None


def _delivery_problem(value: dict) -> GatewayResult | None:
    """An outer success never overrides a contradictory nested receipt."""
    unknown = GatewayResult("unknown", "GATEWAY_RESPONSE_UNVERIFIED")
    states = [value.get("deliveryStatus"), value.get("status")]
    if any(state is not None and not isinstance(state, str) for state in states):
        return unknown
    if any(key in value and not isinstance(value[key], bool) for key in ("ok", "isError", "dryRun", "sentBeforeError")):
        return unknown
    if value.get("sentBeforeError") or any(state in ("partial_failed", "queued", "pending") for state in states):
        return GatewayResult("unknown", "GATEWAY_DELIVERY_UNCERTAIN")
    if (value.get("dryRun") or value.get("ok") is False or value.get("error") or value.get("isError")
            or any(state in ("failed", "suppressed", "error") for state in states)):
        return GatewayResult("rejected", "GATEWAY_TOOL_REJECTED")
    if any(state not in (None, "sent", "settled", "success", "ok") for state in states):
        return unknown
    return None


def _interpret_result(body: object, *, target: str, token: str) -> GatewayResult:
    unknown = GatewayResult("unknown", "GATEWAY_RESPONSE_UNVERIFIED")
    rejected = GatewayResult("rejected", "GATEWAY_TOOL_REJECTED")
    if not isinstance(body, dict):
        return unknown
    if body.get("ok") is False:
        return rejected
    if body.get("ok") is not True:
        return unknown
    result = body.get("result")
    if not isinstance(result, dict):
        return unknown
    problem = _delivery_problem(result)
    if problem:
        return problem
    if "details" in result:
        details = result["details"]
    else:
        # jsonResult historically carries both details and a JSON text block.
        content = result.get("content")
        details = None
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    candidate = _load_json(item["text"])
                    if isinstance(candidate, dict):
                        if details is not None:
                            return unknown
                        details = candidate
    if not isinstance(details, dict):
        return unknown
    problem = _delivery_problem(details)
    if problem:
        return problem
    receipt = details.get("result")
    if not (
        details.get("deliveryStatus") == "sent" and details.get("channel") == _CHANNEL and details.get("to") == target
        and isinstance(receipt, dict) and receipt.get("channel") == _CHANNEL
        and all(receipt[key] == target for key in ("target", "to") if key in receipt)
    ):
        return unknown
    problem = _delivery_problem(receipt)
    if problem:
        return problem
    message_id = receipt.get("messageId")
    if not _valid_text(message_id, _MESSAGE_ID) or message_id.lower() == "unknown" or token in message_id:
        return unknown
    return GatewayResult("accepted", "GATEWAY_ACCEPTED", message_id)


def send_image(config: dict, token: str, png: bytes, *, filename: str, caption: str) -> GatewayResult:
    """Perform one request, with no automatic retries, redirects, or model turn."""
    try:
        url, payload = _request_payload(config, token, png, filename, caption)
    except (TypeError, ValueError):
        return GatewayResult("rejected", "GATEWAY_INPUT_INVALID")

    try:
        # Do not inherit machine proxy credentials or allow a redirect to forward
        # the customer's owner-level Gateway token to a different destination.
        with httpx.Client(
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS),
            transport=httpx.HTTPTransport(retries=0, trust_env=False),
        ) as client:
            with client.stream(
                "POST", url + "/tools/invoke", json=payload,
                headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    return GatewayResult("rejected", "GATEWAY_REDIRECT_REJECTED")
                if status >= 500 or status == 408:
                    return GatewayResult("unknown", "GATEWAY_SERVER_UNCERTAIN")
                raw = bytearray()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return GatewayResult("unknown", "GATEWAY_RESPONSE_UNVERIFIED")
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > _MAX_RESPONSE_BYTES:
                        return GatewayResult("unknown", "GATEWAY_RESPONSE_UNVERIFIED")
                    raw.extend(chunk)
                body = _load_json(bytes(raw))
                if 400 <= status < 500:
                    if status == 403 and isinstance(body, dict):
                        error = body.get("error")
                        if isinstance(error, dict) and error.get("message") == _UPLOADS_DISABLED:
                            return GatewayResult("rejected", "GATEWAY_UPLOADS_DISABLED")
                    code = {
                        401: "GATEWAY_AUTH_REJECTED", 403: "GATEWAY_POLICY_REJECTED",
                        404: "GATEWAY_TOOL_UNAVAILABLE", 413: "GATEWAY_IMAGE_TOO_LARGE",
                        429: "GATEWAY_RATE_LIMITED",
                    }.get(status, "GATEWAY_REQUEST_REJECTED")
                    return GatewayResult("rejected", code)
                if status != 200:
                    return GatewayResult("unknown", "GATEWAY_RESPONSE_UNVERIFIED")
                return _interpret_result(body, target=config["target"], token=token)
    except httpx.TimeoutException:
        return GatewayResult("unknown", "GATEWAY_TIMEOUT")
    except (httpx.HTTPError, OSError, ValueError):
        # Exception and response strings may contain credentials or private URLs.
        return GatewayResult("unknown", "GATEWAY_TRANSPORT_UNCERTAIN")
