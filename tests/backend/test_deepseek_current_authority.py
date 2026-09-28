"""DeepSeek Current Authority V1：双官方源、exact scope permit 与 Fail-Closed acceptance。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import deepseek_current_authority as authority  # noqa: E402

from app import deepseek_credential
from app.secret_store import InMemorySecretStore


@pytest.fixture(autouse=True)
def fake_report_credential_store(monkeypatch):
    monkeypatch.setattr(deepseek_credential, "_secret_store_factory", InMemorySecretStore)



MODELS_BYTES = json.dumps(
    {
        "object": "list",
        "data": [{"id": "deepseek-flash", "object": "model"}],
    },
    separators=(",", ":"),
).encode()
METADATA_BYTES = b"""
<html><body><table>
<tr><th>MODEL</th><th>deepseek-flash</th></tr>
<tr><th>MODEL VERSION</th><td>DeepSeek-V4.1-Flash</td></tr>
<tr><th>CONTEXT LENGTH</th><td>1M</td></tr>
<tr><th>MAX OUTPUT</th><td>384K</td></tr>
</table></body></html>
"""


def _call(**overrides) -> dict[str, object]:
    value: dict[str, object] = {
        "model_call_id": 7,
        "call_identity_hash": "a" * 64,
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "rule_version": "rules/1.0",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "samples/1.0",
        "task_type": "daily_report_generate",
    }
    value.update(overrides)
    return value


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _set_exact_permit(
    monkeypatch,
    call: dict[str, object],
    manifest_hash: str = "b" * 64,
    payload_hash: str = "c" * 64,
) -> str:
    scope_hash = authority._stable_hash(
        authority._data_scope_payload(
            call=call,
            final_context_manifest_hash=manifest_hash,
            framed_payload_hash=payload_hash,
        )
    )
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_AUTHORIZED", "true")
    monkeypatch.setenv(
        "ANXIN_DEEPSEEK_SEND_PURPOSE_ID", authority.PURPOSE_ID
    )
    monkeypatch.setenv(
        "ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH", scope_hash
    )
    return scope_hash


def _install_valid_sources(
    monkeypatch,
    *,
    models: bytes = MODELS_BYTES,
    metadata: bytes = METADATA_BYTES,
):
    calls = {"models": 0, "metadata": 0}

    def fetch_models():
        calls["models"] += 1
        return models

    def fetch_metadata():
        calls["metadata"] += 1
        return metadata

    monkeypatch.setattr(authority, "_fetch_models_source", fetch_models)
    monkeypatch.setattr(authority, "_fetch_metadata_source", fetch_metadata)
    return calls


def _resolve(
    monkeypatch,
    *,
    call: dict[str, object] | None = None,
    manifest_hash: str = "b" * 64,
    payload_hash: str = "c" * 64,
):
    call = deepcopy(call or _call())
    monkeypatch.setattr(
        authority, "get_model_call", lambda _id: deepcopy(call)
    )
    _install_valid_sources(monkeypatch)
    _set_exact_permit(monkeypatch, call, manifest_hash, payload_hash)
    return authority.resolve_deepseek_current_authority(
        model_call_id=call["model_call_id"],
        final_context_manifest_hash=manifest_hash,
        framed_payload_hash=payload_hash,
    )


def _metadata(
    *,
    version="DeepSeek-V4.1-Flash",
    context="1M",
    output="384K",
    duplicate=False,
) -> bytes:
    table = f"""<table>
<tr><th>MODEL</th><th>deepseek-flash</th></tr>
<tr><th>MODEL VERSION</th><td>{version}</td></tr>
<tr><th>CONTEXT LENGTH</th><td>{context}</td></tr>
<tr><th>MAX OUTPUT</th><td>{output}</td></tr>
</table>"""
    if duplicate:
        table += table
    return table.encode()


def test_t01_valid_combined_authority_exact_shape_and_hashes(monkeypatch):
    call = _call()
    scope_hash = _set_exact_permit(monkeypatch, call)
    monkeypatch.setattr(
        authority, "get_model_call", lambda _id: deepcopy(call)
    )
    calls = _install_valid_sources(monkeypatch)

    result = authority.resolve_deepseek_current_authority(
        model_call_id=7,
        final_context_manifest_hash="b" * 64,
        framed_payload_hash="c" * 64,
    )

    assert calls == {"models": 1, "metadata": 1}
    assert result["schema_version"] == "deepseek_current_authority_v1"
    assert result["model_call_id"] == 7
    assert result["provider"] == "deepseek"
    assert result["model_id"] == authority.MODEL_ID
    assert result["model_version"] == authority.MODEL_VERSION
    assert result["context_window_tokens"] == 1_000_000
    assert result["max_output_tokens"] == 384_000
    assert result["purpose_id"] == authority.PURPOSE_ID
    assert result["data_scope_hash"] == scope_hash
    expected = {k: v for k, v in result.items() if k != "authority_hash"}
    assert result["authority_hash"] == authority._stable_hash(expected)

    qualification = result["qualification"]
    assert qualification["rule_version"] == "rules/1.0"
    assert qualification["output_schema_version"] == "daily-report/1.0"
    assert qualification["benchmark_sample_pack_version"] == "samples/1.0"
    assert qualification["models_source_hash"] == hashlib.sha256(
        MODELS_BYTES
    ).hexdigest()
    assert qualification["metadata_source_hash"] == hashlib.sha256(
        METADATA_BYTES
    ).hexdigest()
    q_expected = {
        k: v for k, v in qualification.items() if k != "evidence_hash"
    }
    assert qualification["evidence_hash"] == authority._stable_hash(q_expected)

    authorization = result["authorization"]
    assert authorization["authorized"] is True
    assert authorization["valid"] is True
    assert authorization["data_scope_hash"] == scope_hash
    a_expected = {
        k: v for k, v in authorization.items() if k != "evidence_hash"
    }
    assert authorization["evidence_hash"] == authority._stable_hash(a_expected)


def test_t02_formal_model_call_is_read_exactly_once_before_sources(monkeypatch):
    call = _call()
    _set_exact_permit(monkeypatch, call)
    counts = {"call": 0}

    def get_call(model_call_id):
        counts["call"] += 1
        assert model_call_id == 7
        return deepcopy(call)

    monkeypatch.setattr(authority, "get_model_call", get_call)
    sources = _install_valid_sources(monkeypatch)
    authority.resolve_deepseek_current_authority(
        model_call_id=7,
        final_context_manifest_hash="b" * 64,
        framed_payload_hash="c" * 64,
    )
    assert counts == {"call": 1}
    assert sources == {"models": 1, "metadata": 1}


@pytest.mark.parametrize("value", [True, 0, -1])
def test_t03_direct_model_call_id_invalid_stops_before_ledger(
    monkeypatch, value
):
    monkeypatch.setattr(
        authority,
        "get_model_call",
        lambda _id: pytest.fail("ledger must not run"),
    )
    with pytest.raises(HTTPException) as caught:
        authority.resolve_deepseek_current_authority(
            model_call_id=value,
            final_context_manifest_hash="b" * 64,
            framed_payload_hash="c" * 64,
        )
    assert _code(caught) == "DEEPSEEK_AUTHORITY_INPUT_INVALID"


@pytest.mark.parametrize(
    "field", ["final_context_manifest_hash", "framed_payload_hash"]
)
@pytest.mark.parametrize("value", ["", "A" * 64, "0" * 63, "g" * 64])
def test_t04_direct_hashes_require_lowercase_sha256(
    monkeypatch, field, value
):
    kwargs = {
        "model_call_id": 7,
        "final_context_manifest_hash": "b" * 64,
        "framed_payload_hash": "c" * 64,
    }
    kwargs[field] = value
    monkeypatch.setattr(
        authority,
        "get_model_call",
        lambda _id: pytest.fail("ledger must not run"),
    )
    with pytest.raises(HTTPException) as caught:
        authority.resolve_deepseek_current_authority(**kwargs)
    assert _code(caught) == "DEEPSEEK_AUTHORITY_INPUT_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "other"),
        ("model_id", "deepseek-flash-alias"),
        ("model_version", "DeepSeek-V4.0-Flash"),
        ("call_identity_hash", "bad"),
        ("rule_version", ""),
        ("output_schema_version", ""),
        ("benchmark_sample_pack_version", ""),
        ("task_type", ""),
    ],
)
def test_t05_prepared_identity_drift_stops_before_source_network(
    monkeypatch, field, value
):
    call = _call(**{field: value})
    monkeypatch.setattr(authority, "get_model_call", lambda _id: call)
    monkeypatch.setattr(
        authority,
        "_fetch_models_source",
        lambda: pytest.fail("source must not run"),
    )
    with pytest.raises(HTTPException) as caught:
        authority.resolve_deepseek_current_authority(
            model_call_id=7,
            final_context_manifest_hash="b" * 64,
            framed_payload_hash="c" * 64,
        )
    assert _code(caught) == "DEEPSEEK_AUTHORITY_CALL_IDENTITY_INVALID"


def test_t06_ledger_http_error_is_remapped_to_call_identity_invalid(monkeypatch):
    def fail(_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "MODEL_CALL_NOT_FOUND", "message": "x"},
        )

    monkeypatch.setattr(authority, "get_model_call", fail)
    with pytest.raises(HTTPException) as caught:
        authority.resolve_deepseek_current_authority(
            model_call_id=7,
            final_context_manifest_hash="b" * 64,
            framed_payload_hash="c" * 64,
        )
    assert _code(caught) == "DEEPSEEK_AUTHORITY_CALL_IDENTITY_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"id": "DEEPSEEK-FLASH"}]},
        {"data": [{"id": "deepseek-v4"}]},
        {"data": [{"id": "deepseek-flash-extra"}]},
        {
            "data": [
                {"id": "deepseek-flash"},
                {"id": "deepseek-flash"},
            ]
        },
    ],
)
def test_t07_model_membership_is_exact_no_alias_case_prefix_or_duplicate(
    payload,
):
    with pytest.raises(HTTPException) as caught:
        authority._validate_models_source(json.dumps(payload).encode())
    assert _code(caught) == "DEEPSEEK_AUTHORITY_MODEL_NOT_CURRENTLY_LISTED"


@pytest.mark.parametrize(
    "raw",
    [b"not-json", b"[]", b'{"data":{}}', b'{"data":[{}]}'],
)
def test_t08_models_source_malformed_is_source_invalid(raw):
    with pytest.raises(HTTPException) as caught:
        authority._validate_models_source(raw)
    assert _code(caught) == "DEEPSEEK_AUTHORITY_SOURCE_INVALID"


def test_t09_metadata_version_change_is_distinct_fail_closed():
    with pytest.raises(HTTPException) as caught:
        authority._parse_metadata_source(
            _metadata(version="DeepSeek-V4.1-Flash-0801")
        )
    assert _code(caught) == "DEEPSEEK_AUTHORITY_MODEL_VERSION_CHANGED"


@pytest.mark.parametrize(
    ("context", "output"),
    [("2M", "384K"), ("1M", "128K")],
)
def test_t10_metadata_constraint_change_is_distinct_fail_closed(
    context, output
):
    with pytest.raises(HTTPException) as caught:
        authority._parse_metadata_source(
            _metadata(context=context, output=output)
        )
    assert _code(caught) == "DEEPSEEK_AUTHORITY_PROVIDER_CONSTRAINT_CHANGED"


@pytest.mark.parametrize(
    "raw",
    [
        b"<html>deepseek-flash DeepSeek-V4.1-Flash 1M 384K</html>",
        _metadata(duplicate=True),
        b"<table><tr><th>MODEL</th><th>deepseek-flash</th></tr>"
        b"<tr><th>MODEL VERSION</th><td>DeepSeek-V4.1-Flash</td></tr>"
        b"</table>",
    ],
)
def test_t11_metadata_requires_unique_structural_binding_not_substring(raw):
    with pytest.raises(HTTPException) as caught:
        authority._parse_metadata_source(raw)
    assert _code(caught) == "DEEPSEEK_AUTHORITY_SOURCE_INVALID"


def test_t12_missing_api_key_is_zero_models_network(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        authority,
        "_read_bounded_source",
        lambda **_kwargs: pytest.fail("network must be zero"),
    )
    with pytest.raises(HTTPException) as caught:
        authority._fetch_models_source()
    assert _code(caught) == "DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE"


class _FakeResponse:
    def __init__(
        self,
        *,
        status=200,
        body=b"ok",
        redirect=False,
        headers=None,
    ):
        self.status_code = status
        self._body = body
        self.is_redirect = redirect
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        for index in range(0, len(self._body), 3):
            yield self._body[index : index + 3]


class _FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, method, url, headers=None):
        self.requests.append((method, url, headers))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("response", "error", "expected"),
    [
        (
            _FakeResponse(status=302, redirect=True),
            None,
            "DEEPSEEK_AUTHORITY_SOURCE_INVALID",
        ),
        (
            _FakeResponse(status=500),
            None,
            "DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE",
        ),
        (
            _FakeResponse(
                body=b"abcdef", headers={"content-length": "6"}
            ),
            None,
            "DEEPSEEK_AUTHORITY_SOURCE_INVALID",
        ),
        (
            None,
            httpx.ReadTimeout("timeout"),
            "DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE",
        ),
        (
            None,
            httpx.ConnectError("network"),
            "DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE",
        ),
    ],
)
def test_t13_source_redirect_non200_oversize_timeout_network_fail_closed(
    monkeypatch, response, error, expected
):
    client = _FakeClient(response=response, error=error)
    monkeypatch.setattr(authority, "_new_client", lambda: client)
    with pytest.raises(HTTPException) as caught:
        authority._read_bounded_source(
            url=authority.MODELS_URL,
            headers={"Accept": "application/json"},
            limit=5,
            unavailable=authority._qualification_unavailable(),
        )
    assert _code(caught) == expected
    assert len(client.requests) == 1


def test_t14_each_source_request_is_exactly_one_and_auth_is_scoped(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-secret")
    first = _FakeClient(_FakeResponse(body=MODELS_BYTES))
    second = _FakeClient(_FakeResponse(body=METADATA_BYTES))
    pool = [first, second]
    monkeypatch.setattr(authority, "_new_client", lambda: pool.pop(0))

    authority._fetch_models_source()
    authority._fetch_metadata_source()

    assert len(first.requests) == len(second.requests) == 1
    assert first.requests[0][0:2] == ("GET", authority.MODELS_URL)
    assert first.requests[0][2]["Authorization"] == "Bearer dummy-secret"
    assert second.requests[0][0:2] == ("GET", authority.METADATA_URL)
    assert "Authorization" not in second.requests[0][2]


@pytest.mark.parametrize(
    ("authorized", "purpose", "scope"),
    [
        (None, None, None),
        ("false", authority.PURPOSE_ID, "d" * 64),
        ("true ", authority.PURPOSE_ID, "d" * 64),
        ("true", "wrong-purpose", "d" * 64),
        ("true", authority.PURPOSE_ID, "e" * 64),
        ("true", authority.PURPOSE_ID, "D" * 64),
    ],
)
def test_t15_permit_is_exact_no_false_wrong_purpose_or_hash(
    monkeypatch, authorized, purpose, scope
):
    expected_scope = "d" * 64
    keys = (
        "ANXIN_DEEPSEEK_SEND_AUTHORIZED",
        "ANXIN_DEEPSEEK_SEND_PURPOSE_ID",
        "ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    if authorized is not None:
        monkeypatch.setenv(keys[0], authorized)
    if purpose is not None:
        monkeypatch.setenv(keys[1], purpose)
    if scope is not None:
        monkeypatch.setenv(keys[2], scope)
    with pytest.raises(HTTPException) as caught:
        authority._authorization_evidence(expected_scope)
    assert _code(caught) == "DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED"


def test_t16_data_scope_contains_every_frozen_bound_field():
    call = _call()
    payload = authority._data_scope_payload(
        call=call,
        final_context_manifest_hash="b" * 64,
        framed_payload_hash="c" * 64,
    )
    assert payload == {
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "model_call_id": 7,
        "call_identity_hash": "a" * 64,
        "final_context_manifest_hash": "b" * 64,
        "framed_payload_hash": "c" * 64,
        "task_type": "daily_report_generate",
        "output_schema_version": "daily-report/1.0",
        "purpose_id": "anxin_board_daily_report_v1",
    }
    original = authority._stable_hash(payload)
    mutations = [
        ({**call, "model_call_id": 8}, "b" * 64, "c" * 64),
        ({**call, "call_identity_hash": "d" * 64}, "b" * 64, "c" * 64),
        (call, "e" * 64, "c" * 64),
        (call, "b" * 64, "f" * 64),
        ({**call, "task_type": "report_contradiction_check"}, "b" * 64, "c" * 64),
        ({**call, "output_schema_version": "other/1.0"}, "b" * 64, "c" * 64),
    ]
    for changed_call, manifest_hash, framed_hash in mutations:
        changed = authority._data_scope_payload(
            call=changed_call,
            final_context_manifest_hash=manifest_hash,
            framed_payload_hash=framed_hash,
        )
        assert authority._stable_hash(changed) != original


def test_t17_system_fingerprint_never_used_as_semantic_version(monkeypatch):
    result = _resolve(monkeypatch)
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert result["model_version"] == "DeepSeek-V4.1-Flash"
    assert "system_fingerprint" not in serialized


def test_t18_no_raw_key_env_or_provider_bodies_leak(monkeypatch):
    secret = "super-secret-never-return"
    docs_marker = "raw-doc-body-never-return"
    call = _call()
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    _set_exact_permit(monkeypatch, call)
    monkeypatch.setattr(
        authority, "get_model_call", lambda _id: deepcopy(call)
    )
    models = json.dumps(
        {
            "data": [
                {"id": "deepseek-flash", "secret_marker": secret}
            ]
        }
    ).encode()
    metadata = METADATA_BYTES.replace(
        b"</body>", docs_marker.encode() + b"</body>"
    )
    _install_valid_sources(
        monkeypatch, models=models, metadata=metadata
    )
    result = authority.resolve_deepseek_current_authority(
        model_call_id=7,
        final_context_manifest_hash="b" * 64,
        framed_payload_hash="c" * 64,
    )
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert secret not in serialized
    assert docs_marker not in serialized
    assert "Authorization" not in serialized


def test_t19_source_boundary_has_no_chat_send_persistence_or_retry():
    source = inspect.getsource(authority)
    assert "chat/completions" not in source
    assert "sqlite3" not in source
    assert "get_connection" not in source
    assert "import requests" not in source
    assert "subprocess" not in source
    assert "sleep(" not in source
    assert "follow_redirects=False" in source
    assert "trust_env=False" in source
    assert source.count("get_model_call(model_call_id)") == 1
    assert "system_fingerprint" not in source


def test_t20_network_failure_severs_credential_bearing_request_exception_chain(
    monkeypatch,
):
    secret = "credential-must-not-survive-exception-chain"
    request = httpx.Request(
        "GET",
        authority.MODELS_URL,
        headers={"Authorization": f"Bearer {secret}"},
    )
    client = _FakeClient(
        error=httpx.ConnectError("network", request=request)
    )
    monkeypatch.setattr(authority, "_new_client", lambda: client)

    with pytest.raises(HTTPException) as caught:
        authority._read_bounded_source(
            url=authority.MODELS_URL,
            headers={"Authorization": f"Bearer {secret}"},
            limit=5,
            unavailable=authority._qualification_unavailable(),
        )

    assert _code(caught) == "DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in repr(caught.value)


def test_t21_malformed_models_source_severs_raw_body_exception_chain():
    raw = b'{"raw_model_body_secret":"must-not-survive"'
    with pytest.raises(HTTPException) as caught:
        authority._validate_models_source(raw)
    assert _code(caught) == "DEEPSEEK_AUTHORITY_SOURCE_INVALID"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "raw_model_body_secret" not in repr(caught.value)


def test_t22_non_utf8_metadata_source_severs_raw_body_exception_chain():
    raw = b"\xffraw-doc-body-must-not-survive"
    with pytest.raises(HTTPException) as caught:
        authority._parse_metadata_source(raw)
    assert _code(caught) == "DEEPSEEK_AUTHORITY_SOURCE_INVALID"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "raw-doc-body-must-not-survive" not in repr(caught.value)


def _regenerate_call(**overrides) -> dict[str, object]:
    value = _call(
        task_type=authority.REGENERATE_TASK_TYPE,
        output_schema_version=authority.REGENERATE_OUTPUT_SCHEMA_VERSION,
        rule_version=authority.REGENERATE_RULE_VERSION,
        benchmark_sample_pack_version=authority.REGENERATE_SAMPLE_PACK_VERSION,
    )
    value.update(overrides)
    return value


def _regenerate_record(prompt_hash: str, sampling_hash: str, **overrides) -> dict[str, object]:
    value: dict[str, object] = {
        "provider": authority.PROVIDER,
        "model_id": authority.MODEL_ID,
        "model_version": authority.MODEL_VERSION,
        "task_type": authority.REGENERATE_TASK_TYPE,
        "ai_contract_schema_version": authority.AI_CONTRACT_SCHEMA_VERSION,
        "output_schema_version": authority.REGENERATE_OUTPUT_SCHEMA_VERSION,
        "prompt_version": authority.REGENERATE_PROMPT_VERSION,
        "prompt_contract_hash": prompt_hash,
        "rule_version": authority.REGENERATE_RULE_VERSION,
        "sample_pack_version": authority.REGENERATE_SAMPLE_PACK_VERSION,
        "sampling_parameters_hash": sampling_hash,
        "sample_manifest_hash": "1" * 64,
        "qualification_harness_commit": "qualification-harness-commit",
        "evidence_manifest_hash": "2" * 64,
        "review_ref": "review/pass/fixture",
        "qualification_status": authority.model_qualification_registry.QUALIFIED_STATUS,
        "record_hash": "3" * 64,
    }
    value.update(overrides)
    return value


def _set_regenerate_permit(
    monkeypatch,
    *,
    call: dict[str, object],
    manifest_hash: str,
    payload_hash: str,
    request_hash: str,
    prompt_hash: str,
    sampling_hash: str,
) -> str:
    scope = authority._stable_hash(
        authority._regenerate_data_scope_payload(
            call=call,
            final_context_manifest_hash=manifest_hash,
            framed_payload_hash=payload_hash,
            request_envelope_hash=request_hash,
            prompt_contract_hash=prompt_hash,
            sampling_parameters_hash=sampling_hash,
        )
    )
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_AUTHORIZED", "true")
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_PURPOSE_ID", authority.REGENERATE_PURPOSE_ID)
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH", scope)
    return scope


def test_t23_regenerate_uses_exact_registry_identity_and_zero_provider_source_io(monkeypatch):
    call = _regenerate_call()
    prompt_hash, sampling_hash = "4" * 64, "5" * 64
    manifest_hash, payload_hash, request_hash = "6" * 64, "7" * 64, "8" * 64
    expected_scope = _set_regenerate_permit(
        monkeypatch,
        call=call,
        manifest_hash=manifest_hash,
        payload_hash=payload_hash,
        request_hash=request_hash,
        prompt_hash=prompt_hash,
        sampling_hash=sampling_hash,
    )
    seen = {}
    monkeypatch.setattr(authority, "get_model_call", lambda _id: deepcopy(call))
    monkeypatch.setattr(
        authority,
        "_fetch_models_source",
        lambda: pytest.fail("regenerate must not access provider qualification source"),
    )
    monkeypatch.setattr(
        authority,
        "_fetch_metadata_source",
        lambda: pytest.fail("regenerate must not access provider metadata source"),
    )

    def lookup(identity):
        seen.update(identity)
        return _regenerate_record(prompt_hash, sampling_hash)

    monkeypatch.setattr(authority.model_qualification_registry, "lookup_qualified_record", lookup)
    result = authority.resolve_deepseek_regenerate_current_authority(
        model_call_id=7,
        final_context_manifest_hash=manifest_hash,
        framed_payload_hash=payload_hash,
        request_envelope_hash=request_hash,
        prompt_contract_hash=prompt_hash,
        sampling_parameters_hash=sampling_hash,
    )
    assert seen == {
        "provider": authority.PROVIDER,
        "model_id": authority.MODEL_ID,
        "model_version": authority.MODEL_VERSION,
        "task_type": authority.REGENERATE_TASK_TYPE,
        "ai_contract_schema_version": authority.AI_CONTRACT_SCHEMA_VERSION,
        "output_schema_version": authority.REGENERATE_OUTPUT_SCHEMA_VERSION,
        "prompt_version": authority.REGENERATE_PROMPT_VERSION,
        "prompt_contract_hash": prompt_hash,
        "rule_version": authority.REGENERATE_RULE_VERSION,
        "sample_pack_version": authority.REGENERATE_SAMPLE_PACK_VERSION,
        "sampling_parameters_hash": sampling_hash,
    }
    assert result["purpose_id"] == authority.REGENERATE_PURPOSE_ID
    assert result["data_scope_hash"] == expected_scope
    assert result["qualification"]["authority_source_id"] == "product_model_qualification_registry"


def test_t24_regenerate_missing_admission_fails_before_human_authorization(monkeypatch):
    call = _regenerate_call()
    monkeypatch.setattr(authority, "get_model_call", lambda _id: deepcopy(call))
    monkeypatch.setattr(
        authority.model_qualification_registry,
        "lookup_qualified_record",
        lambda _identity: (_ for _ in ()).throw(
            authority.model_qualification_registry.QualificationRegistryError(
                "QUALIFICATION_NOT_ADMITTED", "not admitted"
            )
        ),
    )
    monkeypatch.setattr(
        authority,
        "_authorization_evidence_for_purpose",
        lambda *_args, **_kwargs: pytest.fail("Human authorization must remain independent"),
    )
    with pytest.raises(HTTPException) as caught:
        authority.resolve_deepseek_regenerate_current_authority(
            model_call_id=7,
            final_context_manifest_hash="6" * 64,
            framed_payload_hash="7" * 64,
            request_envelope_hash="8" * 64,
            prompt_contract_hash="4" * 64,
            sampling_parameters_hash="5" * 64,
        )
    assert _code(caught) == "DEEPSEEK_AUTHORITY_QUALIFICATION_NOT_ADMITTED"


def test_t25_regenerate_registry_return_drift_is_rejected(monkeypatch):
    call = _regenerate_call()
    monkeypatch.setattr(authority, "get_model_call", lambda _id: deepcopy(call))
    monkeypatch.setattr(
        authority.model_qualification_registry,
        "lookup_qualified_record",
        lambda _identity: _regenerate_record("4" * 64, "5" * 64, rule_version="wrong-rule"),
    )
    with pytest.raises(HTTPException) as caught:
        authority.resolve_deepseek_regenerate_current_authority(
            model_call_id=7,
            final_context_manifest_hash="6" * 64,
            framed_payload_hash="7" * 64,
            request_envelope_hash="8" * 64,
            prompt_contract_hash="4" * 64,
            sampling_parameters_hash="5" * 64,
        )
    assert _code(caught) == "DEEPSEEK_AUTHORITY_QUALIFICATION_REGISTRY_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_type", "daily_report_generate"),
        ("output_schema_version", "daily-report/1.0"),
        ("rule_version", "wrong-rule"),
        ("benchmark_sample_pack_version", "wrong-pack"),
    ],
)
def test_t26_regenerate_prepared_identity_is_closed_world_before_registry(monkeypatch, field, value):
    call = _regenerate_call(**{field: value})
    monkeypatch.setattr(authority, "get_model_call", lambda _id: deepcopy(call))
    monkeypatch.setattr(
        authority.model_qualification_registry,
        "lookup_qualified_record",
        lambda _identity: pytest.fail("registry must not run for mismatched prepared identity"),
    )
    with pytest.raises(HTTPException) as caught:
        authority.resolve_deepseek_regenerate_current_authority(
            model_call_id=7,
            final_context_manifest_hash="6" * 64,
            framed_payload_hash="7" * 64,
            request_envelope_hash="8" * 64,
            prompt_contract_hash="4" * 64,
            sampling_parameters_hash="5" * 64,
        )
    assert _code(caught) == "DEEPSEEK_AUTHORITY_CALL_IDENTITY_INVALID"


def test_t27_regenerate_human_scope_binds_request_prompt_sampling_and_purpose(monkeypatch):
    call = _regenerate_call()
    prompt_hash, sampling_hash = "4" * 64, "5" * 64
    manifest_hash, payload_hash, request_hash = "6" * 64, "7" * 64, "8" * 64
    _set_regenerate_permit(
        monkeypatch,
        call=call,
        manifest_hash=manifest_hash,
        payload_hash=payload_hash,
        request_hash=request_hash,
        prompt_hash=prompt_hash,
        sampling_hash=sampling_hash,
    )
    monkeypatch.setattr(authority, "get_model_call", lambda _id: deepcopy(call))
    monkeypatch.setattr(
        authority.model_qualification_registry,
        "lookup_qualified_record",
        lambda _identity: _regenerate_record(prompt_hash, sampling_hash),
    )
    for changed in (
        {"request_envelope_hash": "9" * 64},
        {"prompt_contract_hash": "a" * 64},
        {"sampling_parameters_hash": "b" * 64},
    ):
        kwargs = {
            "model_call_id": 7,
            "final_context_manifest_hash": manifest_hash,
            "framed_payload_hash": payload_hash,
            "request_envelope_hash": request_hash,
            "prompt_contract_hash": prompt_hash,
            "sampling_parameters_hash": sampling_hash,
        }
        kwargs.update(changed)
        if "prompt_contract_hash" in changed or "sampling_parameters_hash" in changed:
            monkeypatch.setattr(
                authority.model_qualification_registry,
                "lookup_qualified_record",
                lambda identity: _regenerate_record(
                    str(identity["prompt_contract_hash"]), str(identity["sampling_parameters_hash"])
                ),
            )
        with pytest.raises(HTTPException) as caught:
            authority.resolve_deepseek_regenerate_current_authority(**kwargs)
        assert _code(caught) == "DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED"


def test_t28_regenerate_api_has_no_caller_purpose_or_qualification_truth_and_no_send_surface():
    parameters = set(inspect.signature(authority.resolve_deepseek_regenerate_current_authority).parameters)
    assert parameters == {
        "model_call_id",
        "final_context_manifest_hash",
        "framed_payload_hash",
        "request_envelope_hash",
        "prompt_contract_hash",
        "sampling_parameters_hash",
    }
    source = inspect.getsource(authority.resolve_deepseek_regenerate_current_authority)
    assert "send_deepseek" not in source
    assert "chat/completions" not in source
    assert "purpose_id=" not in inspect.signature(authority.resolve_deepseek_regenerate_current_authority).parameters
