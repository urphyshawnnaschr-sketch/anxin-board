from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json

import pytest
from fastapi import HTTPException

from app import final_context_manifest


ACCOUNTING_KEYS = (
    "schema_version",
    "manifest_core_hash",
    "budget_profile_hash",
    "model_call_id",
    "call_identity_hash",
    "snapshot_id",
    "snapshot_hash",
    "project_id",
    "candidate_set_hash",
    "counting_policy_version",
    "framing_policy_version",
    "framing_scope",
    "context_admission_state",
    "coverage_state",
    "admitted_targets",
    "denied_targets",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "conservative_input_token_upper_bound",
    "exact_tokens",
    "budget_fit_state",
    "final_request_fit_state",
    "unsupported_context_sources",
    "token_framing_accounting_hash",
)
RESULT_KEYS = (
    "schema_version",
    "manifest_stage",
    "model_call_id",
    "call_identity_hash",
    "project_id",
    "snapshot_id",
    "snapshot_hash",
    "candidate_set_hash",
    "task_type",
    "provider",
    "model_id",
    "model_version",
    "rule_version",
    "output_schema_version",
    "benchmark_sample_pack_version",
    "qualification_hash",
    "authorization_hash",
    "manifest_core_hash",
    "budget_profile_hash",
    "token_framing_accounting_hash",
    "counting_policy_version",
    "framing_policy_version",
    "framing_scope",
    "context_admission_state",
    "coverage_state",
    "admitted_target_count",
    "denied_target_count",
    "admitted_targets",
    "denied_targets",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "conservative_input_token_upper_bound",
    "exact_tokens",
    "budget_fit_state",
    "final_request_fit_state",
    "unsupported_context_sources",
    "local_request_readiness_state",
    "final_manifest_state",
    "gateway_send_state",
    "final_context_manifest_hash",
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ledger() -> dict[str, object]:
    return {
        "schema_version": "model_call_ledger_v1",
        "project_id": 7,
        "local_task_id": "daily-2026-08-21",
        "call_prepare_key": "prepare-1",
        "snapshot_id": 11,
        "snapshot_hash": _hash("snapshot"),
        "candidate_set_hash": _hash("candidate"),
        "task_type": "daily_report",
        "provider": "provider-a",
        "model_id": "model-a",
        "model_version": "2026-08",
        "rule_version": "rule-v1",
        "output_schema_version": "daily-report-v1",
        "benchmark_sample_pack_version": "benchmark-v1",
        "qualification_status": "qualified",
        "qualification_hash": _hash("qualification"),
        "authorization_provider": "provider-a",
        "authorization_authorized": 1,
        "authorization_valid": 1,
        "authorization_hash": _hash("authorization"),
        "created_at": "2026-08-21T00:00:00+00:00",
        "call_identity_hash": _hash("call"),
        "model_call_id": 13,
        "preparation_state": "prepared",
    }


def _admitted_target(*, ordinal: int = 1, target: str = "profile") -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "target": target,
        "target_type": "profile" if target == "profile" else "evidence",
        "model_send_admission_hash": _hash(f"admission-{target}"),
        "redaction_result_hash": _hash(f"redaction-{target}"),
        "frame_hash": _hash(f"frame-{target}"),
        "frame_utf8_bytes": 120,
    }


def _denied_target(*, ordinal: int = 2, target: str = "git:secret") -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "target": target,
        "target_type": "git",
        "admission_reason": "sensitive_path_hard_deny",
        "matched_rule_id": "SP01",
        "model_send_admission_hash": _hash(f"admission-{target}"),
    }


def _accounting(
    *,
    admitted: list[dict[str, object]] | None = None,
    denied: list[dict[str, object]] | None = None,
    fit: bool = True,
    unsupported: list[str] | None = None,
) -> dict[str, object]:
    admitted = [_admitted_target()] if admitted is None else admitted
    denied = [] if denied is None else denied
    unsupported = [] if unsupported is None else unsupported
    ledger = _ledger()
    result: dict[str, object] = {
        "schema_version": "token_framing_accounting_v1",
        "manifest_core_hash": _hash("core"),
        "budget_profile_hash": _hash("budget"),
        "model_call_id": ledger["model_call_id"],
        "call_identity_hash": ledger["call_identity_hash"],
        "snapshot_id": ledger["snapshot_id"],
        "snapshot_hash": ledger["snapshot_hash"],
        "project_id": ledger["project_id"],
        "candidate_set_hash": ledger["candidate_set_hash"],
        "counting_policy_version": "utf8_byte_upper_bound_v1",
        "framing_policy_version": "context_payload_framing_v1",
        "framing_scope": "context_payload_only",
        "context_admission_state": "contains_denied_targets" if denied else "all_targets_admitted",
        "coverage_state": "partial_fail_visible" if unsupported else "supported_context_complete",
        "admitted_targets": deepcopy(admitted),
        "denied_targets": deepcopy(denied),
        "framed_payload_hash": _hash("payload"),
        "framed_payload_utf8_bytes": 456,
        "conservative_input_token_upper_bound": 456,
        "exact_tokens": None,
        "budget_fit_state": (
            "fit_by_conservative_upper_bound" if fit else "not_fit_by_conservative_upper_bound"
        ),
        "final_request_fit_state": "not_evaluated",
        "unsupported_context_sources": list(unsupported),
    }
    result["token_framing_accounting_hash"] = _stable_hash(result)
    assert tuple(result) == ACCOUNTING_KEYS
    return result


def _install(monkeypatch: pytest.MonkeyPatch, ledger: dict[str, object], accounting: dict[str, object]):
    calls = {"ledger": 0, "accounting": 0}

    def fake_ledger(model_call_id: int):
        calls["ledger"] += 1
        assert model_call_id == 13
        return deepcopy(ledger)

    def fake_accounting(*, model_call_id: int, budget_record):
        calls["accounting"] += 1
        assert model_call_id == 13
        assert budget_record == {"budget": "record"}
        return deepcopy(accounting)

    monkeypatch.setattr(final_context_manifest, "get_model_call", fake_ledger)
    monkeypatch.setattr(
        final_context_manifest,
        "build_context_token_framing_accounting",
        fake_accounting,
    )
    return calls


def _build(monkeypatch: pytest.MonkeyPatch, *, ledger=None, accounting=None):
    ledger = _ledger() if ledger is None else ledger
    accounting = _accounting() if accounting is None else accounting
    calls = _install(monkeypatch, ledger, accounting)
    result = final_context_manifest.build_final_context_manifest(
        model_call_id=13,
        budget_record={"budget": "record"},
    )
    return result, calls


def _error_code(exc: HTTPException) -> str:
    assert isinstance(exc.detail, dict)
    return str(exc.detail["code"])


# T01
def test_t01_exact_keyword_only_signature():
    signature = inspect.signature(final_context_manifest.build_final_context_manifest)
    assert tuple(signature.parameters) == ("model_call_id", "budget_record")
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


# T02
def test_t02_ledger_exactly_once(monkeypatch):
    _, calls = _build(monkeypatch)
    assert calls["ledger"] == 1


# T03
def test_t03_accounting_exactly_once(monkeypatch):
    _, calls = _build(monkeypatch)
    assert calls["accounting"] == 1


# T04
def test_t04_shared_immutable_identity_rebind(monkeypatch):
    for field in (
        "model_call_id",
        "call_identity_hash",
        "project_id",
        "snapshot_id",
        "snapshot_hash",
        "candidate_set_hash",
    ):
        accounting = _accounting()
        accounting[field] = 99 if field in {"model_call_id", "project_id", "snapshot_id"} else _hash(f"drift-{field}")
        accounting["token_framing_accounting_hash"] = _stable_hash(
            {key: accounting[key] for key in ACCOUNTING_KEYS[:-1]}
        )
        _install(monkeypatch, _ledger(), accounting)
        with pytest.raises(HTTPException) as raised:
            final_context_manifest.build_final_context_manifest(
                model_call_id=13, budget_record={"budget": "record"}
            )
        assert _error_code(raised.value) == "FINAL_CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT"


# T05
def test_t05_accounting_exact_schema(monkeypatch):
    accounting = _accounting()
    accounting["unexpected"] = "nope"
    _install(monkeypatch, _ledger(), accounting)
    with pytest.raises(HTTPException) as raised:
        final_context_manifest.build_final_context_manifest(
            model_call_id=13, budget_record={"budget": "record"}
        )
    assert _error_code(raised.value) == "FINAL_CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT"


# T06
def test_t06_accounting_hash_independent_recompute(monkeypatch):
    accounting = _accounting()
    accounting["framed_payload_utf8_bytes"] += 1
    accounting["conservative_input_token_upper_bound"] += 1
    _install(monkeypatch, _ledger(), accounting)
    with pytest.raises(HTTPException) as raised:
        final_context_manifest.build_final_context_manifest(
            model_call_id=13, budget_record={"budget": "record"}
        )
    assert _error_code(raised.value) == "FINAL_CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT"


# T07
def test_t07_ledger_provider_model_output_identity_preserved(monkeypatch):
    result, _ = _build(monkeypatch)
    ledger = _ledger()
    for field in (
        "task_type",
        "provider",
        "model_id",
        "model_version",
        "rule_version",
        "output_schema_version",
        "benchmark_sample_pack_version",
        "qualification_hash",
        "authorization_hash",
    ):
        assert result[field] == ledger[field]


# T08
def test_t08_all_admitted_fit_is_ready_for_gateway_evaluation(monkeypatch):
    result, _ = _build(monkeypatch)
    assert result["local_request_readiness_state"] == "ready_for_gateway_evaluation"


# T09
def test_t09_denied_target_blocks_before_budget(monkeypatch):
    accounting = _accounting(
        admitted=[_admitted_target()],
        denied=[_denied_target()],
        fit=False,
    )
    result, _ = _build(monkeypatch, accounting=accounting)
    assert result["local_request_readiness_state"] == "blocked_context_denied"


# T10
def test_t10_over_budget_blocks_when_no_denied(monkeypatch):
    result, _ = _build(monkeypatch, accounting=_accounting(fit=False))
    assert result["local_request_readiness_state"] == "blocked_context_budget"


# T11
def test_t11_partial_coverage_stays_fail_visible(monkeypatch):
    result, _ = _build(
        monkeypatch,
        accounting=_accounting(unsupported=["issue_comments"]),
    )
    assert result["coverage_state"] == "partial_fail_visible"
    assert result["unsupported_context_sources"] == ["issue_comments"]
    assert result["local_request_readiness_state"] == "ready_for_gateway_evaluation"


# T12
def test_t12_partial_coverage_cannot_masquerade_as_complete(monkeypatch):
    accounting = _accounting(unsupported=["issue_comments"])
    accounting["coverage_state"] = "supported_context_complete"
    accounting["token_framing_accounting_hash"] = _stable_hash(
        {key: accounting[key] for key in ACCOUNTING_KEYS[:-1]}
    )
    _install(monkeypatch, _ledger(), accounting)
    with pytest.raises(HTTPException) as raised:
        final_context_manifest.build_final_context_manifest(
            model_call_id=13, budget_record={"budget": "record"}
        )
    assert _error_code(raised.value) == "FINAL_CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT"


# T13
def test_t13_target_counts_match_arrays(monkeypatch):
    accounting = _accounting(
        admitted=[_admitted_target(), _admitted_target(ordinal=3, target="prd:1")],
        denied=[_denied_target()],
    )
    result, _ = _build(monkeypatch, accounting=accounting)
    assert result["admitted_target_count"] == 2
    assert result["denied_target_count"] == 1


# T14
def test_t14_target_metadata_preserved_without_body(monkeypatch):
    accounting = _accounting(
        admitted=[_admitted_target(), _admitted_target(ordinal=3, target="prd:1")],
        denied=[_denied_target()],
    )
    result, _ = _build(monkeypatch, accounting=accounting)
    assert result["admitted_targets"] == accounting["admitted_targets"]
    assert result["denied_targets"] == accounting["denied_targets"]
    assert "body" not in json.dumps(result, ensure_ascii=False).lower()


# T15
def test_t15_exact_tokens_must_remain_null(monkeypatch):
    accounting = _accounting()
    accounting["exact_tokens"] = 123
    accounting["token_framing_accounting_hash"] = _stable_hash(
        {key: accounting[key] for key in ACCOUNTING_KEYS[:-1]}
    )
    _install(monkeypatch, _ledger(), accounting)
    with pytest.raises(HTTPException) as raised:
        final_context_manifest.build_final_context_manifest(
            model_call_id=13, budget_record={"budget": "record"}
        )
    assert _error_code(raised.value) == "FINAL_CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT"


# T16
def test_t16_final_request_and_gateway_send_stay_not_evaluated(monkeypatch):
    result, _ = _build(monkeypatch)
    assert result["final_request_fit_state"] == "not_evaluated"
    assert result["gateway_send_state"] == "not_evaluated"
    assert result["manifest_stage"] == "local_pre_gateway_final"
    assert result["final_manifest_state"] == "finalized_local_metadata"


# T17
def test_t17_result_has_exact_schema_and_no_payload_or_secret(monkeypatch):
    result, _ = _build(monkeypatch)
    assert tuple(result) == RESULT_KEYS
    serialized = json.dumps(result, ensure_ascii=False).lower()
    for forbidden in (
        "redacted_body",
        "raw_body",
        "canonical frame",
        "api_key",
        "credential",
        "authorization_secret",
    ):
        assert forbidden not in serialized


# T18
def test_t18_deterministic_and_final_hash_is_independent(monkeypatch):
    ledger = _ledger()
    accounting = _accounting()
    result1, _ = _build(monkeypatch, ledger=ledger, accounting=accounting)
    result2, _ = _build(monkeypatch, ledger=ledger, accounting=accounting)
    assert result1 == result2
    expected = _stable_hash({key: result1[key] for key in RESULT_KEYS[:-1]})
    assert result1["final_context_manifest_hash"] == expected


# T19
def test_t19_upstream_inputs_are_not_mutated(monkeypatch):
    ledger = _ledger()
    accounting = _accounting(
        admitted=[_admitted_target(), _admitted_target(ordinal=2, target="prd:1")],
        unsupported=["issue_comments"],
    )
    ledger_before = deepcopy(ledger)
    accounting_before = deepcopy(accounting)
    _build(monkeypatch, ledger=ledger, accounting=accounting)
    assert ledger == ledger_before
    assert accounting == accounting_before


# T20
def test_t20_no_new_provider_network_tokenizer_request_or_persistence_dependency():
    source = inspect.getsource(final_context_manifest)
    forbidden_imports = (
        "import requests",
        "import httpx",
        "import socket",
        "import sqlite3",
        "import subprocess",
        "from pathlib",
        "import os",
    )
    assert all(token not in source for token in forbidden_imports)
    for forbidden_call in (
        "requests.",
        "httpx.",
        "socket.",
        "open(",
        "Path(",
        "subprocess.",
    ):
        assert forbidden_call not in source
    assert "provider request" not in source.lower()
