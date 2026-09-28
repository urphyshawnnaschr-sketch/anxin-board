from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import HTTPException

from app import model_gateway


H = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _admitted_target() -> dict[str, object]:
    return {
        "ordinal": 1,
        "target": "profile",
        "target_type": "profile",
        "model_send_admission_hash": H("admission"),
        "redaction_result_hash": H("redaction"),
        "frame_hash": H("frame"),
        "frame_utf8_bytes": 5,
    }


def _manifest(
    *,
    readiness: str = "ready_for_gateway_evaluation",
    **overrides: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "final_context_manifest_v1",
        "manifest_stage": "local_pre_gateway_final",
        "model_call_id": 7,
        "call_identity_hash": H("call"),
        "project_id": 3,
        "snapshot_id": 11,
        "snapshot_hash": H("snapshot"),
        "candidate_set_hash": H("candidate"),
        "task_type": "daily_report_generate",
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "rule_version": "rules/1",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "bench/1",
        "qualification_hash": H("historical-qualification"),
        "authorization_hash": H("historical-authorization"),
        "manifest_core_hash": H("core"),
        "budget_profile_hash": H("budget"),
        "token_framing_accounting_hash": H("accounting"),
        "counting_policy_version": "utf8_byte_upper_bound_v1",
        "framing_policy_version": "context_payload_framing_v1",
        "framing_scope": "context_payload_only",
        "context_admission_state": "all_targets_admitted",
        "coverage_state": "supported_context_complete",
        "admitted_target_count": 1,
        "denied_target_count": 0,
        "admitted_targets": [_admitted_target()],
        "denied_targets": [],
        "framed_payload_hash": hashlib.sha256(b"hello").hexdigest(),
        "framed_payload_utf8_bytes": 5,
        "conservative_input_token_upper_bound": 5,
        "exact_tokens": None,
        "budget_fit_state": "fit_by_conservative_upper_bound",
        "final_request_fit_state": "not_evaluated",
        "unsupported_context_sources": [],
        "local_request_readiness_state": readiness,
        "final_manifest_state": "finalized_local_metadata",
        "gateway_send_state": "not_evaluated",
    }
    if readiness == "blocked_context_budget":
        value["budget_fit_state"] = "not_fit_by_conservative_upper_bound"
    elif readiness == "blocked_context_denied":
        value["context_admission_state"] = "contains_denied_targets"
        value["admitted_target_count"] = 0
        value["denied_target_count"] = 1
        value["admitted_targets"] = []
        value["denied_targets"] = [
            {
                "ordinal": 1,
                "target": "git:secret",
                "target_type": "git_file_fact",
                "admission_reason": "sensitive_path_hard_deny",
                "matched_rule_id": "SP01",
                "model_send_admission_hash": H("denied-admission"),
            }
        ]
    value.update(overrides)
    value["final_context_manifest_hash"] = _stable_hash(value)
    return value


def _budget(*, reserved: int = 4000, safety: int = 1000) -> dict[str, object]:
    return {
        "reserved_output_tokens": reserved,
        "safety_margin_tokens": safety,
        "caller_extra": {"keep": True},
    }


def _capability(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "deepseek_transport_capability_v1",
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "endpoint_origin": "https://api.deepseek.com",
        "endpoint_path": "/chat/completions",
        "task_type": "daily_report_generate",
        "output_schema_version": "daily-report/1.0",
        "response_format": "json_object",
        "stream": False,
        "thinking": "enabled",
        "reasoning_effort": "high",
        "max_output_tokens": 384000,
    }
    value.update(overrides)
    return value


def _authority(manifest: dict[str, object], **overrides: object) -> dict[str, object]:
    data_scope_hash = _stable_hash(
        {
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "model_version": "DeepSeek-V4.1-Flash",
            "model_call_id": manifest["model_call_id"],
            "call_identity_hash": manifest["call_identity_hash"],
            "final_context_manifest_hash": manifest["final_context_manifest_hash"],
            "framed_payload_hash": manifest["framed_payload_hash"],
            "task_type": manifest["task_type"],
            "output_schema_version": manifest["output_schema_version"],
            "purpose_id": "anxin_board_daily_report_v1",
        }
    )
    qualification: dict[str, object] = {
        "schema_version": "deepseek_current_model_qualification_v1",
        "authority_source_id": "deepseek_official_models_and_metadata",
        "authority_source_version": "v1",
        "authority_ref": {
            "model_list": "deepseek_api_models",
            "model_metadata": "deepseek_api_docs_models_pricing",
        },
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "rule_version": manifest["rule_version"],
        "output_schema_version": manifest["output_schema_version"],
        "benchmark_sample_pack_version": manifest["benchmark_sample_pack_version"],
        "qualification_status": "qualified",
        "context_window_tokens": 1000000,
        "max_output_tokens": 384000,
        "checked_at": "2026-08-30T00:00:00+00:00",
        "models_source_hash": H("models"),
        "metadata_source_hash": H("metadata"),
    }
    qualification["evidence_hash"] = _stable_hash(qualification)
    authorization: dict[str, object] = {
        "schema_version": "deepseek_current_data_authorization_v1",
        "authority_source_id": "windows_process_env_human_permit",
        "authority_source_version": "v1",
        "authority_ref": "process-env/exact-scope",
        "provider": "deepseek",
        "authorized": True,
        "valid": True,
        "purpose_id": "anxin_board_daily_report_v1",
        "data_scope_hash": data_scope_hash,
    }
    authorization["evidence_hash"] = _stable_hash(authorization)
    result: dict[str, object] = {
        "schema_version": "deepseek_current_authority_v1",
        "model_call_id": manifest["model_call_id"],
        "call_identity_hash": manifest["call_identity_hash"],
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "context_window_tokens": 1000000,
        "max_output_tokens": 384000,
        "purpose_id": "anxin_board_daily_report_v1",
        "data_scope_hash": data_scope_hash,
        "qualification": qualification,
        "authorization": authorization,
    }
    result.update(overrides)
    result["authority_hash"] = _stable_hash(result)
    return result


def _authority_error(code: str) -> HTTPException:
    return HTTPException(status_code=503, detail={"code": code, "message": "blocked"})


def _install(
    monkeypatch,
    *,
    manifest: dict[str, object] | None = None,
    payload: object = b"hello",
    capability: object | None = None,
    authority: object | None = None,
    authority_exc: HTTPException | None = None,
):
    manifest = deepcopy(manifest or _manifest())
    capability = deepcopy(_capability() if capability is None else capability)
    authority = deepcopy(_authority(manifest) if authority is None else authority)
    calls = {"manifest": 0, "materialize": 0, "capability": 0, "authority": 0}

    def build_manifest(*, model_call_id, budget_record):
        calls["manifest"] += 1
        assert model_call_id == 7
        return deepcopy(manifest)

    def materialize(*, model_call_id, budget_record):
        calls["materialize"] += 1
        if type(payload) is bytes:
            return {
                "payload": payload,
                "framed_payload_hash": hashlib.sha256(payload).hexdigest(),
                "framed_payload_utf8_bytes": len(payload),
            }
        return {"payload": payload}

    def get_capability():
        calls["capability"] += 1
        return deepcopy(capability)

    def resolve_authority(*, model_call_id, final_context_manifest_hash, framed_payload_hash):
        calls["authority"] += 1
        assert model_call_id == 7
        assert final_context_manifest_hash == manifest["final_context_manifest_hash"]
        assert framed_payload_hash == manifest["framed_payload_hash"]
        if authority_exc is not None:
            raise authority_exc
        return deepcopy(authority)

    monkeypatch.setattr(model_gateway, "build_final_context_manifest", build_manifest)
    monkeypatch.setattr(
        model_gateway.token_framing,
        "_materialize_context_payload_transient",
        materialize,
    )
    monkeypatch.setattr(
        model_gateway.deepseek_transport,
        "get_deepseek_transport_capability",
        get_capability,
    )
    monkeypatch.setattr(
        model_gateway.deepseek_current_authority,
        "resolve_deepseek_current_authority",
        resolve_authority,
    )
    return manifest, calls


def _run(*, budget: dict[str, object] | None = None):
    return model_gateway.build_model_gateway_preflight(
        model_call_id=7,
        budget_record=budget or _budget(),
    )


def _transient(*, budget: dict[str, object] | None = None):
    return model_gateway.materialize_model_gateway_request_transient(
        model_call_id=7,
        budget_record=budget or _budget(),
    )


def _code(caught: pytest.ExceptionInfo[HTTPException]) -> str:
    return caught.value.detail["code"]


def test_public_and_transient_entries_are_exact_keyword_only():
    for fn in (
        model_gateway.build_model_gateway_preflight,
        model_gateway.materialize_model_gateway_request_transient,
    ):
        sig = inspect.signature(fn)
        assert list(sig.parameters) == ["model_call_id", "budget_record"]
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in sig.parameters.values()
        )


def test_blocked_final_manifest_stops_before_payload_capability_and_authority(monkeypatch):
    _, calls = _install(monkeypatch, manifest=_manifest(readiness="blocked_context_denied"))
    result = _run()
    assert result["local_gateway_state"] == "blocked_final_manifest"
    assert calls == {"manifest": 1, "materialize": 0, "capability": 0, "authority": 0}


def test_malformed_payload_stops_before_capability_and_authority(monkeypatch):
    _, calls = _install(monkeypatch, payload="not-bytes")
    with pytest.raises(HTTPException) as caught:
        _run()
    assert _code(caught) == "MODEL_GATEWAY_PAYLOAD_REBIND_FAILED"
    assert calls["capability"] == 0 and calls["authority"] == 0


def test_payload_hash_or_byte_drift_stops_before_capability_and_authority(monkeypatch):
    _, calls = _install(monkeypatch)
    monkeypatch.setattr(
        model_gateway.token_framing,
        "_materialize_context_payload_transient",
        lambda **_: {
            "payload": b"hello",
            "framed_payload_hash": H("wrong"),
            "framed_payload_utf8_bytes": 6,
        },
    )
    with pytest.raises(HTTPException) as caught:
        _run()
    assert _code(caught) == "MODEL_GATEWAY_PAYLOAD_REBIND_FAILED"
    assert calls["capability"] == 0 and calls["authority"] == 0


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("provider", "other"),
        ("model_id", "other"),
        ("model_version", "other"),
        ("task_type", "daily_report_regenerate"),
        ("output_schema_version", "daily-report/9.9"),
    ],
)
def test_wrong_activation_identity_fails_before_current_authority(monkeypatch, field, wrong):
    _, calls = _install(monkeypatch, manifest=_manifest(**{field: wrong}))
    result = _run()
    assert result["local_gateway_state"] == "blocked_provider_compatibility"
    assert result["provider_compatibility_state"] == "incompatible"
    assert result["request_envelope_hash"] is None
    assert calls["capability"] == 1 and calls["authority"] == 0


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "provider",
        "model_id",
        "model_version",
        "endpoint_origin",
        "endpoint_path",
        "task_type",
        "output_schema_version",
        "response_format",
        "stream",
        "thinking",
        "reasoning_effort",
        "max_output_tokens",
    ],
)
def test_every_capability_field_drift_blocks_with_zero_authority(monkeypatch, field):
    capability = _capability()
    capability[field] = 384001 if field == "max_output_tokens" else "drift"
    if field == "stream":
        capability[field] = True
    _, calls = _install(monkeypatch, capability=capability)
    result = _run()
    assert result["local_gateway_state"] == "blocked_provider_compatibility"
    assert calls["capability"] == 1 and calls["authority"] == 0


def test_request_plan_closure_precedes_current_authority(monkeypatch):
    _, calls = _install(monkeypatch)
    monkeypatch.setattr(
        model_gateway,
        "_build_gateway_request_plan",
        lambda **_: (_ for _ in ()).throw(
            HTTPException(status_code=409, detail={"code": "PLAN_STOP"})
        ),
    )
    with pytest.raises(HTTPException) as caught:
        _run()
    assert caught.value.detail["code"] == "PLAN_STOP"
    assert calls["authority"] == 0


def test_capability_and_current_authority_are_each_called_exactly_once(monkeypatch):
    _, calls = _install(monkeypatch)
    result = _run()
    assert result["local_gateway_state"] == "ready_for_provider_transport"
    assert calls["capability"] == 1 and calls["authority"] == 1


@pytest.mark.parametrize(
    "code",
    [
        "DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE",
        "DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE",
        "DEEPSEEK_AUTHORITY_MODEL_METADATA_UNAVAILABLE",
    ],
)
def test_current_authority_unavailable_is_fail_visible(monkeypatch, code):
    _, calls = _install(monkeypatch, authority_exc=_authority_error(code))
    result = _run()
    assert result["local_gateway_state"] == "blocked_current_authority_unavailable"
    assert result["current_qualification_authority_state"] == "unavailable"
    assert result["network_send_state"] == "not_attempted"
    assert calls["authority"] == 1


@pytest.mark.parametrize(
    "code",
    [
        "DEEPSEEK_AUTHORITY_MODEL_NOT_CURRENTLY_LISTED",
        "DEEPSEEK_AUTHORITY_MODEL_VERSION_CHANGED",
        "DEEPSEEK_AUTHORITY_PROVIDER_CONSTRAINT_CHANGED",
    ],
)
def test_current_qualification_failure_is_fail_visible(monkeypatch, code):
    _install(monkeypatch, authority_exc=_authority_error(code))
    result = _run()
    assert result["local_gateway_state"] == "blocked_current_qualification"
    assert result["current_qualification_authority_state"] == "not_verified"


def test_current_authorization_failure_is_fail_visible(monkeypatch):
    _install(
        monkeypatch,
        authority_exc=HTTPException(
            status_code=403,
            detail={"code": "DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED"},
        ),
    )
    result = _run()
    assert result["local_gateway_state"] == "blocked_current_authorization"
    assert result["current_authorization_authority_state"] == "not_authorized"


def test_unrecognized_current_authority_error_is_stable_gateway_error(monkeypatch):
    _install(monkeypatch, authority_exc=_authority_error("DEEPSEEK_AUTHORITY_SOURCE_INVALID"))
    with pytest.raises(HTTPException) as caught:
        _run()
    assert _code(caught) == "MODEL_GATEWAY_CURRENT_AUTHORITY_INCONSISTENT"


def test_wrong_current_authority_purpose_fails_closed_as_malformed_authority(monkeypatch):
    manifest = _manifest()
    authority = _authority(manifest, purpose_id="other-purpose")
    _install(monkeypatch, manifest=manifest, authority=authority)
    with pytest.raises(HTTPException) as caught:
        _run()
    assert _code(caught) == "MODEL_GATEWAY_CURRENT_AUTHORITY_INCONSISTENT"


def test_current_authority_top_level_hash_drift_fails_closed(monkeypatch):
    manifest = _manifest()
    authority = _authority(manifest)
    authority["authority_hash"] = H("wrong")
    _install(monkeypatch, manifest=manifest, authority=authority)
    with pytest.raises(HTTPException) as caught:
        _run()
    assert _code(caught) == "MODEL_GATEWAY_CURRENT_AUTHORITY_INCONSISTENT"


def test_nested_qualification_evidence_hash_drift_fails_closed(monkeypatch):
    manifest = _manifest()
    authority = _authority(manifest)
    authority["qualification"]["evidence_hash"] = H("wrong")
    authority["authority_hash"] = _stable_hash(
        {key: value for key, value in authority.items() if key != "authority_hash"}
    )
    _install(monkeypatch, manifest=manifest, authority=authority)
    with pytest.raises(HTTPException) as caught:
        _run()
    assert _code(caught) == "MODEL_GATEWAY_CURRENT_AUTHORITY_INCONSISTENT"


def test_nested_authorization_scope_drift_fails_closed(monkeypatch):
    manifest = _manifest()
    authority = _authority(manifest)
    authorization = authority["authorization"]
    authorization["data_scope_hash"] = H("wrong-scope")
    authorization["evidence_hash"] = _stable_hash(
        {key: value for key, value in authorization.items() if key != "evidence_hash"}
    )
    authority["authority_hash"] = _stable_hash(
        {key: value for key, value in authority.items() if key != "authority_hash"}
    )
    _install(monkeypatch, manifest=manifest, authority=authority)
    with pytest.raises(HTTPException) as caught:
        _run()
    assert _code(caught) == "MODEL_GATEWAY_CURRENT_AUTHORITY_INCONSISTENT"


def test_output_budget_overflow_blocks_after_authority(monkeypatch):
    _, calls = _install(monkeypatch)
    result = _run(budget=_budget(reserved=384001))
    assert calls["authority"] == 1
    assert result["local_gateway_state"] == "blocked_request_budget"
    assert result["final_request_fit_state"] == "not_fit_by_conservative_upper_bound"


def test_context_budget_overflow_blocks_after_authority(monkeypatch):
    _, calls = _install(monkeypatch)
    result = _run(budget=_budget(reserved=1, safety=1000000))
    assert calls["authority"] == 1
    assert result["local_gateway_state"] == "blocked_request_budget"


def test_ready_means_only_ready_for_provider_transport_and_network_not_attempted(monkeypatch):
    manifest, _ = _install(monkeypatch)
    result = _run()
    assert result["provider"] == "deepseek"
    assert result["model_id"] == "deepseek-flash"
    assert result["model_version"] == "DeepSeek-V4.1-Flash"
    assert result["task_type"] == "daily_report_generate"
    assert result["output_schema_version"] == "daily-report/1.0"
    assert result["local_gateway_state"] == "ready_for_provider_transport"
    assert result["current_qualification_authority_state"] == "verified"
    assert result["current_authorization_authority_state"] == "verified"
    assert result["final_request_fit_state"] == "fit_by_conservative_upper_bound"
    assert result["provider_compatibility_state"] == "compatible"
    assert result["network_send_state"] == "not_attempted"
    assert result["current_authorization_purpose_id"] == "anxin_board_daily_report_v1"
    assert result["current_authorization_data_scope_hash"] == model_gateway._expected_data_scope_hash(manifest)


def test_preflight_and_transient_consume_identical_request_plan_material(monkeypatch):
    _install(monkeypatch)
    preflight = _run()
    transient = _transient()
    assert transient["request_envelope_hash"] == preflight["request_envelope_hash"]
    assert transient["max_tokens"] == 4000
    assert transient["provider"] == preflight["provider"]
    assert transient["model_id"] == preflight["model_id"]
    assert transient["model_version"] == preflight["model_version"]
    assert transient["framed_payload_hash"] == preflight["framed_payload_hash"]
    assert transient["final_context_manifest_hash"] == preflight["final_context_manifest_hash"]


def test_transient_requires_prior_ready_preflight(monkeypatch):
    _, calls = _install(monkeypatch)
    model_gateway._TRANSIENT_MATERIAL_SLOT.set(None)
    with pytest.raises(HTTPException) as caught:
        _transient()
    assert _code(caught) == "MODEL_GATEWAY_TRANSIENT_NOT_READY"
    assert calls == {"manifest": 0, "materialize": 0, "capability": 0, "authority": 0}


def test_transient_consumes_ready_material_with_zero_upstream_calls(monkeypatch):
    _, calls = _install(monkeypatch)
    assert _run()["local_gateway_state"] == "ready_for_provider_transport"
    before = dict(calls)

    def forbidden(**_kwargs):
        raise AssertionError("transient helper must not call external/stateful upstream")

    monkeypatch.setattr(model_gateway, "build_final_context_manifest", forbidden)
    monkeypatch.setattr(
        model_gateway.token_framing,
        "_materialize_context_payload_transient",
        forbidden,
    )
    monkeypatch.setattr(
        model_gateway.deepseek_transport,
        "get_deepseek_transport_capability",
        lambda: (_ for _ in ()).throw(AssertionError("capability call forbidden")),
    )
    monkeypatch.setattr(
        model_gateway.deepseek_current_authority,
        "resolve_deepseek_current_authority",
        forbidden,
    )
    transient = _transient()
    assert transient["schema_version"] == "model_gateway_transient_request_v1"
    assert calls == before

    with pytest.raises(HTTPException) as caught:
        _transient()
    assert _code(caught) == "MODEL_GATEWAY_TRANSIENT_NOT_READY"


def test_transient_copy_context_allows_exactly_one_global_consumer(monkeypatch):
    _, calls = _install(monkeypatch)
    assert _run()["local_gateway_state"] == "ready_for_provider_transport"
    before = dict(calls)
    copied = copy_context()

    transient = copied.run(_transient)
    assert transient["schema_version"] == "model_gateway_transient_request_v1"
    with pytest.raises(HTTPException) as caught:
        _transient()
    assert _code(caught) == "MODEL_GATEWAY_TRANSIENT_NOT_READY"
    assert calls == before


def test_transient_concurrent_copied_contexts_allow_exactly_one_consumer(monkeypatch):
    _, calls = _install(monkeypatch)
    assert _run()["local_gateway_state"] == "ready_for_provider_transport"
    before = dict(calls)
    contexts = [copy_context(), copy_context()]
    barrier = Barrier(2)

    def consume(context):
        def inside_context():
            barrier.wait()
            try:
                return "ok", _transient()
            except HTTPException as exc:
                return "error", exc.detail["code"]

        return context.run(inside_context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, contexts))

    assert [kind for kind, _ in results].count("ok") == 1
    assert [kind for kind, _ in results].count("error") == 1
    error_codes = [value for kind, value in results if kind == "error"]
    assert error_codes == ["MODEL_GATEWAY_TRANSIENT_NOT_READY"]
    assert calls == before


def test_stale_copied_context_cannot_consume_after_later_preflight(monkeypatch):
    _, calls = _install(monkeypatch)
    first = _run()
    assert first["local_gateway_state"] == "ready_for_provider_transport"
    stale = copy_context()

    second = _run()
    assert second["local_gateway_state"] == "ready_for_provider_transport"
    before_consume = dict(calls)
    with pytest.raises(HTTPException) as caught:
        stale.run(_transient)
    assert _code(caught) == "MODEL_GATEWAY_TRANSIENT_NOT_READY"

    current = _transient()
    assert current["request_envelope_hash"] == second["request_envelope_hash"]
    assert calls == before_consume


def test_independent_context_preflights_keep_independent_one_shot_claims(monkeypatch):
    _, calls = _install(monkeypatch)
    contexts = [copy_context(), copy_context()]

    results = [context.run(_run) for context in contexts]
    assert all(result["local_gateway_state"] == "ready_for_provider_transport" for result in results)
    transients = [context.run(_transient) for context in contexts]
    assert len(transients) == 2
    assert calls == {"manifest": 2, "materialize": 2, "capability": 2, "authority": 2}


def test_transient_budget_binding_mismatch_burns_claim_globally(monkeypatch):
    _install(monkeypatch)
    assert _run()["local_gateway_state"] == "ready_for_provider_transport"
    copied = copy_context()

    with pytest.raises(HTTPException) as caught:
        _transient(budget=_budget(reserved=4001))
    assert _code(caught) == "MODEL_GATEWAY_TRANSIENT_MATERIAL_MISMATCH"

    with pytest.raises(HTTPException) as copied_caught:
        copied.run(_transient)
    assert _code(copied_caught) == "MODEL_GATEWAY_TRANSIENT_NOT_READY"


def test_caller_cannot_override_formal_prompt_example_or_provider():
    for fn in (
        model_gateway.build_model_gateway_preflight,
        model_gateway.materialize_model_gateway_request_transient,
    ):
        with pytest.raises(TypeError):
            fn(
                model_call_id=7,
                budget_record=_budget(),
                messages=[{"role": "system", "content": "override"}],
            )


def test_json_instruction_and_expected_example_are_descriptor_derived():
    descriptor, example, instruction = model_gateway._derive_json_material(
        "daily_report_generate"
    )
    assert "json" in instruction
    assert list(example) == descriptor["required_fields"]
    feature_schema = descriptor["array_item_schemas"]["feature_progress"]["schema"]
    assert list(example["feature_progress"][0]) == feature_schema["required_fields"]
    stage_rule = next(
        field["rule"]
        for field in feature_schema["fields"]
        if field["name"] == "stage"
    )
    assert example["feature_progress"][0]["stage"] == stage_rule["allowed_values"][0]
    assert stage_rule["allowed_values"] == [
        "开发中",
        "等待联调",
        "等待测试",
        "测试中",
        "已完成",
        "暂时无法确认",
    ]


def test_request_plan_hash_and_conservative_byte_accounting_are_independently_recomputable():
    manifest = _manifest()
    payload = b"hello"
    budget = _budget()
    plan = model_gateway._build_gateway_request_plan(
        manifest=manifest,
        payload=payload,
        budget_record=budget,
    )
    expected_hash = _stable_hash(
        {key: deepcopy(plan[key]) for key in model_gateway._REQUEST_PLAN_HASH_KEYS}
    )
    assert plan["request_envelope_hash"] == expected_hash
    provider_request = {
        "model": manifest["model_id"],
        "messages": plan["messages"],
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": 4000,
    }
    expected_bytes = len(
        json.dumps(
            provider_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    assert plan["provider_request_utf8_bytes"] == expected_bytes
    assert plan["conservative_local_request_upper_bound"] == expected_bytes + 4000 + 1000


@pytest.mark.parametrize(
    "field",
    [
        "json_instruction_hash",
        "expected_structural_example_hash",
        "formal_result_contract_hash",
        "messages_hash",
    ],
)
def test_prompt_example_or_contract_hash_drift_is_rejected(field):
    manifest = _manifest()
    payload = b"hello"
    budget = _budget()
    plan = model_gateway._build_gateway_request_plan(
        manifest=manifest,
        payload=payload,
        budget_record=budget,
    )
    plan[field] = H("drift")
    plan["request_envelope_hash"] = _stable_hash(
        {key: deepcopy(plan[key]) for key in model_gateway._REQUEST_PLAN_HASH_KEYS}
    )
    with pytest.raises(HTTPException) as caught:
        model_gateway._validate_request_plan(
            plan,
            manifest=manifest,
            payload=payload,
            budget_record=budget,
        )
    assert _code(caught) == "MODEL_GATEWAY_REQUEST_PLAN_INCONSISTENT"


def test_transient_material_drift_burns_claim_globally(monkeypatch):
    _install(monkeypatch)
    assert _run()["local_gateway_state"] == "ready_for_provider_transport"
    cached = model_gateway._TRANSIENT_MATERIAL_SLOT.get()
    assert isinstance(cached, dict)
    copied = copy_context()
    cached["payload"] = b"changed"

    with pytest.raises(HTTPException) as caught:
        _transient()
    assert _code(caught) == "MODEL_GATEWAY_REQUEST_PLAN_INCONSISTENT"

    with pytest.raises(HTTPException) as copied_caught:
        copied.run(_transient)
    assert _code(copied_caught) == "MODEL_GATEWAY_TRANSIENT_NOT_READY"


def test_gateway_preflight_hash_is_independently_recomputable(monkeypatch):
    _install(monkeypatch)
    result = _run()
    expected = _stable_hash(
        {key: deepcopy(result[key]) for key in model_gateway._RESULT_HASH_KEYS}
    )
    assert result["gateway_preflight_hash"] == expected
    assert list(result) == list(model_gateway._RESULT_KEYS)


def test_inputs_are_not_mutated(monkeypatch):
    _install(monkeypatch)
    budget = _budget()
    before = deepcopy(budget)
    _run(budget=budget)
    assert budget == before


def test_gateway_source_has_no_sender_credential_db_persistence_or_logging_surface():
    source = Path("apps/backend/app/model_gateway.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in (
        "send_deepseek_v4_flash",
        "model_execution_results",
        "record_model_execution_result",
        "import httpx",
        "import requests",
        "import socket",
        "os.getenv",
        "os.environ",
        "environ[",
        "api_key",
        "sqlite3",
        "sqlalchemy",
        "logging.",
        "open(",
    ):
        assert forbidden not in lowered
    assert source.count("get_deepseek_transport_capability()") == 1
    assert source.count("resolve_deepseek_current_authority(") == 1

    transient_source = inspect.getsource(
        model_gateway.materialize_model_gateway_request_transient
    )
    for forbidden in (
        "build_final_context_manifest",
        "_materialize_context_payload_transient",
        "get_deepseek_transport_capability",
        "resolve_deepseek_current_authority",
    ):
        assert forbidden not in transient_source


def test_no_retry_or_fallback_mechanism_is_present():
    source = Path("apps/backend/app/model_gateway.py").read_text(encoding="utf-8").lower()
    assert "retry" not in source
    assert "fallback" not in source

# Page07 3B4 P regenerate coverage: selector-only server rebind and hash closure.
def _regenerate_manifest(**overrides: object) -> dict[str, object]:
    return _manifest(
        task_type="daily_report_regenerate",
        output_schema_version="daily-report-regenerate/1.0",
        rule_version="page07-regenerate-rules/2.0",
        benchmark_sample_pack_version="page07-regenerate-qualification-pack/2.0",
        **overrides,
    )


def _regenerate_model_call(manifest: dict[str, object], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "model_call_id": manifest["model_call_id"],
        "project_id": manifest["project_id"],
        "local_task_id": "report-reanalysis-21-fixture",
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_hash": manifest["snapshot_hash"],
        "candidate_set_hash": manifest["candidate_set_hash"],
        "task_type": manifest["task_type"],
        "provider": manifest["provider"],
        "model_id": manifest["model_id"],
        "model_version": manifest["model_version"],
        "rule_version": manifest["rule_version"],
        "output_schema_version": manifest["output_schema_version"],
        "benchmark_sample_pack_version": manifest["benchmark_sample_pack_version"],
        "qualification_hash": manifest["qualification_hash"],
        "authorization_hash": manifest["authorization_hash"],
        "call_identity_hash": manifest["call_identity_hash"],
    }
    value.update(overrides)
    return value


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _regenerate_subject(
    manifest: dict[str, object],
    call: dict[str, object],
    *,
    report_version_id: int = 21,
    corrected_truth: str = "PM corrected truth",
    replacement_local_task_id: str | None = None,
    snapshot_id: int | None = None,
    snapshot_hash: str | None = None,
) -> dict[str, object]:
    from app import context_redaction

    subject: dict[str, object] = {
        "schema_version": "page07_daily_report_regenerate_subject_v1",
        "task_type": "daily_report_regenerate",
        "project_id": call["project_id"],
        "source_report": {
            "report_version_id": report_version_id,
            "report_content_hash": H("old-report"),
            "model_execution_result_id": 31,
            "execution_result_hash": H("old-result"),
            "model_call_id": 5,
            "call_identity_hash": H("old-call"),
        },
        "reanalysis_request": {
            "reanalysis_request_id": 41,
            "request_hash": H("reanalysis-request"),
            "error_location": "section A",
            "error_location_hash": _text_hash("section A"),
            "corrected_truth": corrected_truth,
            "corrected_truth_hash": _text_hash(corrected_truth),
            "correction_basis": "PM review",
            "correction_basis_hash": _text_hash("PM review"),
            "correction_source": "project_manager",
            "correction_source_hash": _text_hash("project_manager"),
            "requested_by": "pm-fixture",
            "requested_at": "2026-09-02T12:00:00+00:00",
            "requested_timezone": "UTC",
        },
        "replacement_task": {
            "replacement_task_id": 51,
            "replacement_local_task_id": replacement_local_task_id or str(call["local_task_id"]),
            "replacement_task_identity_hash": H("replacement-task"),
        },
        "evidence_snapshot": {
            "evidence_snapshot_id": snapshot_id if snapshot_id is not None else manifest["snapshot_id"],
            "evidence_snapshot_hash": snapshot_hash or str(manifest["snapshot_hash"]),
        },
        "redaction_policy_id": context_redaction.POLICY_ID,
        "redaction_policy_hash": context_redaction.REDACTION_POLICY_HASH,
    }
    redaction = context_redaction.redact_credential_safe_structured_value(value=subject)
    redacted_subject = redaction["redacted_value"]
    assert isinstance(redacted_subject, dict)
    return {
        "schema_version": "page07_task_subject_result_v1",
        "subject_schema_version": "page07_daily_report_regenerate_subject_v1",
        "task_type": "daily_report_regenerate",
        "subject": redacted_subject,
        "task_subject_hash": redaction["redacted_value_hash"],
        "redaction_policy_id": redaction["redaction_policy_id"],
        "redaction_policy_hash": redaction["redaction_policy_hash"],
        "redaction_stats": redaction["redaction_stats"],
        "redaction_match_count": redaction["redaction_match_count"],
    }


def _regenerate_authority(
    manifest: dict[str, object],
    *,
    request_envelope_hash: str,
    prompt_contract_hash: str,
    sampling_parameters_hash: str,
) -> dict[str, object]:
    data_scope_hash = _stable_hash(
        {
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "model_version": "DeepSeek-V4.1-Flash",
            "model_call_id": manifest["model_call_id"],
            "call_identity_hash": manifest["call_identity_hash"],
            "final_context_manifest_hash": manifest["final_context_manifest_hash"],
            "framed_payload_hash": manifest["framed_payload_hash"],
            "request_envelope_hash": request_envelope_hash,
            "prompt_contract_hash": prompt_contract_hash,
            "sampling_parameters_hash": sampling_parameters_hash,
            "task_type": "daily_report_regenerate",
            "output_schema_version": "daily-report-regenerate/1.0",
            "purpose_id": "anxin_board_daily_report_regenerate_v1",
        }
    )
    qualification: dict[str, object] = {
        "schema_version": "deepseek_current_registry_qualification_v1",
        "authority_source_id": "product_model_qualification_registry",
        "authority_source_version": "v1",
        "authority_ref": {"record_hash": H("registry-record"), "review_ref": "review/pass/fixture"},
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "task_type": "daily_report_regenerate",
        "ai_contract_schema_version": "ai-agent-contract/1.0",
        "output_schema_version": "daily-report-regenerate/1.0",
        "prompt_version": "page07-regenerate-prompt/1.0",
        "prompt_contract_hash": prompt_contract_hash,
        "rule_version": "page07-regenerate-rules/2.0",
        "sample_pack_version": "page07-regenerate-qualification-pack/2.0",
        "sampling_parameters_hash": sampling_parameters_hash,
        "qualification_status": "qualified",
        "sample_manifest_hash": H("sample-manifest"),
        "qualification_harness_commit": "qualification-harness-commit",
        "evidence_manifest_hash": H("evidence-manifest"),
        "review_ref": "review/pass/fixture",
        "record_hash": H("registry-record"),
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
    }
    qualification["evidence_hash"] = _stable_hash(qualification)
    authorization: dict[str, object] = {
        "schema_version": "deepseek_current_data_authorization_v1",
        "authority_source_id": "windows_process_env_human_permit",
        "authority_source_version": "v1",
        "authority_ref": "process-env/exact-scope",
        "provider": "deepseek",
        "authorized": True,
        "valid": True,
        "purpose_id": "anxin_board_daily_report_regenerate_v1",
        "data_scope_hash": data_scope_hash,
    }
    authorization["evidence_hash"] = _stable_hash(authorization)
    result: dict[str, object] = {
        "schema_version": "deepseek_current_authority_v1",
        "model_call_id": manifest["model_call_id"],
        "call_identity_hash": manifest["call_identity_hash"],
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "purpose_id": "anxin_board_daily_report_regenerate_v1",
        "data_scope_hash": data_scope_hash,
        "qualification": qualification,
        "authorization": authorization,
    }
    result["authority_hash"] = _stable_hash(result)
    return result


def _install_regenerate(
    monkeypatch,
    *,
    corrected_truth: str = "PM corrected truth",
    replacement_local_task_id: str | None = None,
    snapshot_id: int | None = None,
    snapshot_hash: str | None = None,
):
    from app import model_call_ledger, report_task_subjects

    manifest = _regenerate_manifest()
    call = _regenerate_model_call(manifest)
    subject = _regenerate_subject(
        manifest,
        call,
        corrected_truth=corrected_truth,
        replacement_local_task_id=replacement_local_task_id,
        snapshot_id=snapshot_id,
        snapshot_hash=snapshot_hash,
    )
    calls: dict[str, object] = {"manifest": 0, "materialize": 0, "capability": [], "call": 0, "subject": [], "authority": []}

    def build_manifest(*, model_call_id, budget_record):
        calls["manifest"] += 1
        assert model_call_id == 7
        return deepcopy(manifest)

    def materialize(*, model_call_id, budget_record):
        calls["materialize"] += 1
        return {"payload": b"hello", "framed_payload_hash": hashlib.sha256(b"hello").hexdigest(), "framed_payload_utf8_bytes": 5}

    def capability(**kwargs):
        calls["capability"].append(deepcopy(kwargs))
        return _capability(task_type="daily_report_regenerate", output_schema_version="daily-report-regenerate/1.0")

    def get_call(model_call_id):
        calls["call"] += 1
        assert model_call_id == 7
        return deepcopy(call)

    def build_subject(*, project_id, report_version_id):
        calls["subject"].append((project_id, report_version_id))
        return deepcopy(subject)

    def resolve_authority(**kwargs):
        calls["authority"].append(deepcopy(kwargs))
        return _regenerate_authority(
            manifest,
            request_envelope_hash=kwargs["request_envelope_hash"],
            prompt_contract_hash=kwargs["prompt_contract_hash"],
            sampling_parameters_hash=kwargs["sampling_parameters_hash"],
        )

    monkeypatch.setattr(model_gateway, "build_final_context_manifest", build_manifest)
    monkeypatch.setattr(model_gateway.token_framing, "_materialize_context_payload_transient", materialize)
    monkeypatch.setattr(model_gateway.deepseek_transport, "get_deepseek_transport_capability", capability)
    monkeypatch.setattr(model_call_ledger, "get_model_call", get_call)
    monkeypatch.setattr(report_task_subjects, "build_daily_report_regenerate_subject", build_subject)
    monkeypatch.setattr(model_gateway.deepseek_current_authority, "resolve_deepseek_regenerate_current_authority", resolve_authority)
    return manifest, call, subject, calls


def _run_regenerate(*, report_version_id: int = 21, budget: dict[str, object] | None = None):
    return model_gateway.build_daily_report_regenerate_gateway_preflight(
        model_call_id=7,
        budget_record=budget or _budget(),
        report_version_id=report_version_id,
    )


def _transient_regenerate(*, report_version_id: int = 21, budget: dict[str, object] | None = None):
    return model_gateway.materialize_daily_report_regenerate_gateway_request_transient(
        model_call_id=7,
        budget_record=budget or _budget(),
        report_version_id=report_version_id,
    )


def test_regenerate_public_entries_are_selector_only_keyword_contracts():
    for fn in (
        model_gateway.build_daily_report_regenerate_gateway_preflight,
        model_gateway.materialize_daily_report_regenerate_gateway_request_transient,
    ):
        sig = inspect.signature(fn)
        assert list(sig.parameters) == ["model_call_id", "budget_record", "report_version_id"]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())
        with pytest.raises(TypeError):
            fn(model_call_id=7, budget_record=_budget(), report_version_id=21, corrected_truth="caller injection")


@pytest.mark.parametrize("selector", [True, 0, -1, 2**63])
def test_regenerate_selector_invalid_stops_before_manifest(monkeypatch, selector):
    monkeypatch.setattr(model_gateway, "build_final_context_manifest", lambda **_: pytest.fail("manifest must not run"))
    with pytest.raises(HTTPException) as caught:
        model_gateway.build_daily_report_regenerate_gateway_preflight(
            model_call_id=7, budget_record=_budget(), report_version_id=selector
        )
    assert _code(caught) == "MODEL_GATEWAY_REGENERATE_SELECTOR_INVALID"


def test_regenerate_exact_capability_server_subject_and_authority_hash_binding(monkeypatch):
    _, call, subject, calls = _install_regenerate(monkeypatch)
    result = _run_regenerate()
    assert result["local_gateway_state"] == "ready_for_provider_transport"
    assert calls["capability"] == [{"task_type": "daily_report_regenerate", "output_schema_version": "daily-report-regenerate/1.0"}]
    assert calls["subject"] == [(call["project_id"], 21)]
    assert len(calls["authority"]) == 1
    authority_args = calls["authority"][0]
    assert set(authority_args) == {
        "model_call_id", "final_context_manifest_hash", "framed_payload_hash",
        "request_envelope_hash", "prompt_contract_hash", "sampling_parameters_hash",
    }
    cached = model_gateway._TRANSIENT_MATERIAL_SLOT.get()
    assert cached["task_subject"]["task_subject_hash"] == subject["task_subject_hash"]
    assert "PM corrected truth" in cached["plan"]["messages"][1]["content"]
    assert cached["plan"]["request_envelope_hash"] == result["request_envelope_hash"]


def test_regenerate_correction_subject_changes_effective_input_and_envelope(monkeypatch):
    _install_regenerate(monkeypatch, corrected_truth="truth A")
    first = _run_regenerate()
    first_cached = model_gateway._TRANSIENT_MATERIAL_SLOT.get()
    assert isinstance(first_cached, dict)
    first_task_subject_hash = first_cached["plan"]["task_subject_hash"]
    first_effective_input_hash = first_cached["plan"]["effective_input_hash"]
    _install_regenerate(monkeypatch, corrected_truth="truth B")
    second = _run_regenerate()
    second_cached = model_gateway._TRANSIENT_MATERIAL_SLOT.get()
    assert isinstance(second_cached, dict)
    assert first_task_subject_hash != second_cached["plan"]["task_subject_hash"]
    assert first_effective_input_hash != second_cached["plan"]["effective_input_hash"]
    assert first["request_envelope_hash"] != second["request_envelope_hash"]


def test_regenerate_accepts_credential_redacted_subject_and_tamper_fails_closed(monkeypatch):
    from app import context_redaction

    secret = "SYNTHETIC_CREDENTIAL_123456"
    original_truth = f"PM corrected truth\npassword={secret}"
    _, _, subject, calls = _install_regenerate(
        monkeypatch,
        corrected_truth=original_truth,
    )
    request = subject["subject"]["reanalysis_request"]
    visible_truth = request["corrected_truth"]
    assert secret not in json.dumps(subject, ensure_ascii=False)
    assert visible_truth != original_truth
    assert request["corrected_truth_hash"] == _text_hash(original_truth)
    assert _text_hash(visible_truth) != request["corrected_truth_hash"]
    assert subject["redaction_policy_id"] == context_redaction.POLICY_ID
    assert subject["redaction_policy_hash"] == context_redaction.REDACTION_POLICY_HASH
    assert subject["redaction_match_count"] > 0
    assert sum(subject["redaction_stats"].values()) == subject["redaction_match_count"]
    result = _run_regenerate()
    assert result["local_gateway_state"] == "ready_for_provider_transport"
    assert len(calls["authority"]) == 1

    _, _, tampered_subject, tamper_calls = _install_regenerate(
        monkeypatch,
        corrected_truth=original_truth,
    )
    tampered_subject["subject"]["reanalysis_request"]["corrected_truth"] += " tampered"
    with pytest.raises(HTTPException) as caught:
        _run_regenerate()
    assert _code(caught) == "MODEL_GATEWAY_UPSTREAM_INCONSISTENT"
    assert tamper_calls["authority"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"replacement_local_task_id": "wrong-local-task"},
        {"snapshot_id": 99},
        {"snapshot_hash": H("wrong-snapshot")},
    ],
)
def test_regenerate_replacement_task_or_snapshot_drift_fails_before_authority(monkeypatch, kwargs):
    _, _, _, calls = _install_regenerate(monkeypatch, **kwargs)
    with pytest.raises(HTTPException) as caught:
        _run_regenerate()
    assert _code(caught) == "MODEL_GATEWAY_UPSTREAM_INCONSISTENT"
    assert calls["authority"] == []


def test_regenerate_transient_consumes_cached_plan_with_zero_upstream_reread(monkeypatch):
    _install_regenerate(monkeypatch)
    ready = _run_regenerate()
    assert ready["local_gateway_state"] == "ready_for_provider_transport"

    monkeypatch.setattr(model_gateway, "build_final_context_manifest", lambda **_: pytest.fail("manifest reread forbidden"))
    monkeypatch.setattr(model_gateway.token_framing, "_materialize_context_payload_transient", lambda **_: pytest.fail("payload reread forbidden"))
    monkeypatch.setattr(model_gateway.deepseek_transport, "get_deepseek_transport_capability", lambda **_: pytest.fail("capability reread forbidden"))
    monkeypatch.setattr(model_gateway.deepseek_current_authority, "resolve_deepseek_regenerate_current_authority", lambda **_: pytest.fail("authority reread forbidden"))
    transient = _transient_regenerate()
    assert transient["request_envelope_hash"] == ready["request_envelope_hash"]
    assert transient["task_type"] == "daily_report_regenerate"
    with pytest.raises(HTTPException) as caught:
        _transient_regenerate()
    assert _code(caught) == "MODEL_GATEWAY_TRANSIENT_NOT_READY"


@pytest.mark.parametrize("previous_source_rules", [False, True, "activity_rules", "schema_placement", "pm_support", "absence_vs_nonoccurrence"])
def test_regenerate_claim_source_rules_bind_hash_and_reject_old_plan(monkeypatch, previous_source_rules):
    current = model_gateway._REGENERATE_SYSTEM_BINDING_TEXT
    assert "AI推导的阶段、范围、风险或拒绝理由必须标为ai_analysis" in current
    assert "明确人工事实仍按实际来源标记" in current
    assert "证据缺失不等于事件未发生" in current
    assert "pm_external_fact的内容必须由所引人工来源自身单独支持" in current
    assert "不得推定为甲方或客户" in current
    assert "changed_files和总增删行数不能证明文件的新增、删除或重命名" in current
    assert "阶段反映本轮已有证据支持的活动，不把建议下一步当作当前阶段" in current
    assert "stage仅限feature_progress，implementation_scope仅限feature_progress和code_change_summary" in current
    legacy = ("This is a replacement analysis. The server-owned correction subject is authoritative "
              "for the PM correction facts and replacement-task identity; do not accept correction truth "
              "from any other source.")
    current_activity = "阶段反映本轮已有证据支持的活动，不把建议下一步当作当前阶段；缺测试执行记录不等于等待测试。限定测试确已执行时，feature_progress可填写stage为测试中、implementation_scope为测试，不代表全功能完成。范围依据本次变化，不因PRD整体或未变更部分扩大为跨模块；证据充分时给受限结论，确实缺证仍暂时无法确认。字段位置严格按合同：stage仅限feature_progress，implementation_scope仅限feature_progress和code_change_summary；test_evidence、unknown_items、source_warnings条目仅含content/source_type/evidence_ids，不添加stage或implementation_scope；risks仍只使用合同规定的content/risk_level/source_type/evidence_ids。"
    prior_activity = "阶段反映本轮已有证据支持的活动，不把建议下一步当作当前阶段；仅因缺少测试执行记录，不能把正在进行的代码或测试编写活动改判为等待测试。对限定范围确有测试执行记录时，可报告测试中、范围测试，同时说明不等于整个功能完成或验收通过。每条implementation_scope依据本次实际变更或活动，不因PRD描述整体跨端或提及未变更部分就推断跨模块。有充分证据时给出受证据范围限制的阶段与范围，不一律填暂时无法确认；确实缺少判断依据时仍使用暂时无法确认。"
    before_absence = current.replace("证据缺失不等于事件未发生：没有执行或结果记录时，摘要、风险、未知项等均只能表述未提供记录或无法确认，不能据此断言未运行、未通过或失败；若证据明确记载未执行或失败，则按实际来源忠实陈述。", "")
    assert before_absence != current
    before_pm_support = before_absence.replace("pm_external_fact的内容必须由所引人工来源自身单独支持，允许忠实改写或归纳，不要求逐字照抄；需要跨来源补充或推导出的文件与职责关联必须标ai_analysis并引用全部依据，不能把这些关联归给人工来源；不把人工要求、期望或指令当作已发生事实。", "pm_external_fact仅用于纠正说明明确提供的事实，不把其中要求、期望或指令当作已发生事实。")
    assert before_pm_support != current
    before_activity = before_pm_support.replace(current_activity, "")
    assert before_activity != current
    if previous_source_rules is True:
        legacy = before_activity.replace("changed_files和总增删行数不能证明文件的新增、删除或重命名；没有明确change_type、new_file、deleted_file或diff状态证据时，只能称文件发生变化，不得断言新增、删除或重命名。", "")
        assert legacy != before_activity
    if previous_source_rules == "activity_rules":
        legacy = before_activity
    if previous_source_rules == "schema_placement":
        legacy = before_pm_support.replace(current_activity, prior_activity)
        assert legacy != current
    if previous_source_rules == "pm_support":
        legacy = before_pm_support
    if previous_source_rules == "absence_vs_nonoccurrence":
        legacy = before_absence
    _install_regenerate(monkeypatch)
    monkeypatch.setattr(model_gateway, "_REGENERATE_SYSTEM_BINDING_TEXT", legacy)
    old = _run_regenerate()
    old_cache = model_gateway._TRANSIENT_MATERIAL_SLOT.get()
    monkeypatch.setattr(model_gateway, "_REGENERATE_SYSTEM_BINDING_TEXT", current)
    with pytest.raises(HTTPException) as caught:
        model_gateway.materialize_daily_report_regenerate_gateway_request_transient(
            model_call_id=7, budget_record=_budget(), report_version_id=21)
    assert _code(caught) == "MODEL_GATEWAY_REQUEST_PLAN_INCONSISTENT"
    new = _run_regenerate()
    new_cache = model_gateway._TRANSIENT_MATERIAL_SLOT.get()
    assert old["request_envelope_hash"] != new["request_envelope_hash"]
    assert old_cache["plan"]["prompt_contract_hash"] != new_cache["plan"]["prompt_contract_hash"]
    assert old_cache["plan"]["sampling_parameters_hash"] == new_cache["plan"]["sampling_parameters_hash"]
    assert current in new_cache["plan"]["messages"][0]["content"]
    result = model_gateway.materialize_daily_report_regenerate_gateway_request_transient(
        model_call_id=7, budget_record=_budget(), report_version_id=21)
    assert result["request_envelope_hash"] == new["request_envelope_hash"]
